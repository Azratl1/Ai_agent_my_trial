# -*- coding: utf-8 -*-
"""Celery-приложение и задачи пайплайна (очередь через локальный Redis)."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from celery import Celery  # type: ignore

from app.agent.settings import settings

log = logging.getLogger("agent.tasks")

celery_app = Celery(
    "doc_audit",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Moscow",
    task_routes={
        "agent.parse_etalon": {"queue": "index"},
        "agent.delete_etalon": {"queue": "index"},
        "agent.audit_incoming": {"queue": "audit"},
        "agent.reindex_all": {"queue": "index"},
    },
    task_acks_late=True,
    worker_max_tasks_per_child=50,          # периодический перезапуск — очистка памяти ML-моделей
    broker_transport_options={"visibility_timeout": 3600},
)


@celery_app.task(name="agent.parse_etalon", bind=True, max_retries=2)
def parse_etalon(self, path_str: str) -> dict:
    """Шаг индексации эталона: Docling -> чанки -> Qdrant."""
    from app.agent.auditor import reindex_etalon
    p = Path(path_str)
    if not p.exists():
        return {"skipped": "gone"}
    t0 = time.time()
    n = reindex_etalon(p)
    return {"file": p.name, "chunks": n, "seconds": round(time.time() - t0, 1)}


@celery_app.task(name="agent.delete_etalon")
def delete_etalon(filename: str) -> dict:
    from app.agent.auditor import unindex_etalon
    unindex_etalon(filename)
    return {"deleted_vectors_for": filename}


@celery_app.task(name="agent.audit_incoming", bind=True, max_retries=1)
def audit_incoming(self, path_str: str) -> dict:
    """Полный пайплайн проверки файла из /data/02_na_proverku."""
    from app.agent.auditor import run_audit
    p = Path(path_str)
    if not p.exists():
        return {"skipped": "file moved/removed before processing"}
    try:
        return run_audit(p)
    except Exception as exc:  # noqa: BLE001
        log.exception("Аудит %s упал", p.name)
        raise self.retry(exc=exc, countdown=30)


@celery_app.task(name="agent.reindex_all")
def reindex_all() -> dict:
    """Полная переиндексация /data/01_etalony (запускается при старте watcher'а)."""
    from app.agent.auditor import reindex_etalon
    from app.agent.indexer import get_indexer
    get_indexer().ensure_collections()
    total = 0
    for f in sorted(settings.dir_etalony.rglob("*")):
        if f.is_file() and f.suffix.lower() in settings.supported_exts:
            total += reindex_etalon(f) or 0
    return {"indexed_chunks_total": total}

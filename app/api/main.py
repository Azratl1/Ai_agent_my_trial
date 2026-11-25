#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI-оркестратор: статусы, ручной запуск проверок, RAG-поиск, webhooks."""
from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from app.agent import storage
from app.agent.auditor import run_audit
from app.agent.checklists import load_checklists
from app.agent.indexer import get_indexer
from app.agent.settings import settings
from app.agent.tasks import audit_incoming, parse_etalon, reindex_all

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent.api")

app = FastAPI(title="Doc Audit Agent (offline)", version="1.0.0",
              docs_url="/internal/docs", openapi_url="/internal/openapi.json")


def require_token(x_internal_token: str = Header(default="")) -> None:
    """Общий секрет из .env — доступ только для внутренних сервисов/admindash."""
    if not hmac.compare_digest(x_internal_token, settings.api_internal_token):
        raise HTTPException(401, "Bad internal token")


class AuditRequest(BaseModel):
    path: Optional[str] = None      # конкретный файл; иначе — все в 02_na_proverku
    sync: bool = False              # true: выполнить прямо сейчас (для отладки)


@app.get("/internal/health")
def health() -> dict:
    return {"status": "ok", "offline_mode": settings.offline_mode,
            "hf_offline": True}


@app.post("/internal/audit/run", dependencies=[Depends(require_token)])
def trigger_audit(req: AuditRequest) -> dict:
    """Ручной запуск проверки (или постановка всех входящих в очередь)."""
    if req.path:
        p = Path(req.path)
        if not p.exists():
            raise HTTPException(404, f"Файл не найден: {p}")
        if req.sync:
            return run_audit(p)
        audit_incoming.delay(str(p))
        return {"queued": str(p)}
    queued = []
    for f in sorted(settings.dir_incoming.glob("*")):
        if f.is_file() and f.suffix.lower() in settings.supported_exts:
            audit_incoming.delay(str(f))
            queued.append(f.name)
    return {"queued": queued, "count": len(queued)}


@app.post("/internal/index/rebuild", dependencies=[Depends(require_token)])
def rebuild_index(sync: bool = Query(False)) -> dict:
    """Полная переиндексация /data/01_etalony."""
    get_indexer().ensure_collections()
    if sync:
        total = 0
        for f in sorted(settings.dir_etalony.rglob("*")):
            if f.is_file() and f.suffix.lower() in settings.supported_exts:
                from app.agent.auditor import reindex_etalon
                total += reindex_etalon(f) or 0
        return {"indexed_chunks_total": total}
    reindex_all.delay()
    return {"queued": "reindex_all"}


@app.post("/internal/index/etalon", dependencies=[Depends(require_token)])
def index_one(path: str = Query(...)) -> dict:
    p = Path(path)
    if not p.exists():
        raise HTTPException(404, "no such file")
    parse_etalon.delay(str(p))
    return {"queued": p.name}


@app.get("/internal/search", dependencies=[Depends(require_token)])
def hybrid_search(q: str = Query(min_length=2), kind: str = "etalon",
                  limit: int = 10) -> list[dict]:
    """Гибридный dense+sparse поиск (используется Open WebUI/RAG-источниками)."""
    return get_indexer().hybrid_search(q, source_kind=kind, limit=limit)


@app.get("/internal/history", dependencies=[Depends(require_token)])
def history(limit: int = 50, status: Optional[str] = None) -> list[dict]:
    return storage.audit_history(limit=limit, status=status)


@app.get("/internal/audit/{filename}", dependencies=[Depends(require_token)])
def last(filename: str) -> dict:
    r = storage.last_audit(filename)
    if not r:
        raise HTTPException(404, "нет истории проверок для файла")
    return r


@app.get("/internal/checklists")
def checklists() -> dict:
    cfg = load_checklists()
    return {"version": cfg.version, "updated": cfg.updated,
            "checklists": list(cfg.checklists.keys())}


class WebhookBody(BaseModel):
    file: str


@app.post("/internal/webhooks/paperless")
def paperless_webhook(body: WebhookBody,
                      x_webhook_secret: str = Header(default="")) -> dict:
    """Webhook из Paperless-ngx (внутренний): expected fields: {'file': '/data/...'}."""
    if not settings.paperless_webhook_enabled:
        raise HTTPException(404, "webhook выключен")
    if not hmac.compare_digest(x_webhook_secret, settings.api_internal_token):
        raise HTTPException(401)
    p = Path(body.get("file", ""))
    if p.exists():
        audit_incoming.delay(str(p))
        return {"queued": p.name}
    raise HTTPException(404, "файл не найден")


if __name__ == "__main__":
    import uvicorn
    storage.init_db()
    get_indexer().ensure_collections()
    uvicorn.run(app, host="0.0.0.0", port=8000)

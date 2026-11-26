#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Watcher за рабочими директориями.

Логика:
  - watchdog наблюдает /data/01_etalony и /data/02_na_proverku;
  - событие «файл создан/изменён» НЕ обрабатывается сразу: ждём FILE_SETTLE_SECONDS,
    пока размер/mtime стабилизируются (сетевые папки SMB/NFS пишут медленно);
  - затем задача ставится в Celery (очереди index / audit);
  - при удалении файла из эталонов — векторы удаляются из Qdrant;
  - раз в час — reconciliation-скан (выловить события, потерянные в сети).

Запуск standalone:  python -m app.agent.watcher
(в compose это сервис `watcher`).
"""
from __future__ import annotations

import fnmatch
import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler  # type: ignore
from watchdog.observers.polling import PollingObserver  # type: ignore — надёжен для SMB/NFS

from app.agent.settings import settings
from app.agent.tasks import audit_incoming, delete_etalon, parse_etalon, reindex_all

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [watcher] %(message)s")
log = logging.getLogger("agent.watcher")


def _supported(path: Path) -> bool:
    if path.suffix.lower() not in settings.supported_exts:
        return False
    for pat in settings.exclude_patterns:
        if fnmatch.fnmatch(path.name, pat):
            return False
    return True


class Pending:
    """Буфер «созрел ли файл»: путь -> (размер, mtime, время последней активности)."""
    def __init__(self) -> None:
        self.items: dict[Path, tuple[int, float, float]] = {}

    def touch(self, p: Path) -> None:
        try:
            st = p.stat()
            self.items[p] = (st.st_size, st.st_mtime, time.time())
        except OSError:
            self.items.pop(p, None)

    def ready(self) -> list[Path]:
        out = []
        now = time.time()
        for p, (size, mtime, last) in list(self.items.items()):
            try:
                st = p.stat()
            except OSError:
                self.items.pop(p, None)      # файл исчез (перемещён воркером)
                continue
            stable = (st.st_size == size and abs(st.st_mtime - mtime) < 1e-6)
            if stable and now - last >= settings.file_settle_seconds:
                out.append(p)
                self.items.pop(p)
            else:
                self.items[p] = (st.st_size, st.st_mtime, now)  # обновляем точку отсчёта
        return out


class Handler(FileSystemEventHandler):
    def __init__(self, kind: str, pending: Pending) -> None:
        self.kind = kind          # 'etalon' | 'incoming'
        self.pending = pending

    def on_created(self, event: FileSystemEvent) -> None:
        self._add(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._add(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        p = Path(event.src_path)
        if self.kind == "etalon" and _supported(p):
            log.info("Удалён эталон %s -> чистим векторы", p.name)
            delete_etalon.delay(p.name)

    def _add(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if _supported(p) and p.exists():
            self.pending.touch(p)


def enqueue_ready(kind: str, ready: list[Path]) -> None:
    for p in ready:
        if kind == "etalon":
            log.info("Эталон готов к индексации: %s", p)
            parse_etalon.delay(str(p))
        else:
            log.info("Входящий готов к проверке: %s", p)
            audit_incoming.delay(str(p))


def initial_scan(dir_et: Path, dir_in: Path, pend_e: Pending, pend_i: Pending) -> None:
    """При старте: всё, что уже лежит в папках, ставится в очередь."""
    for d, pend in ((dir_et, pend_e), (dir_in, pend_i)):
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.is_file() and _supported(f):
                pend.touch(f)


def main() -> None:
    dir_et, dir_in = settings.dir_etalony, settings.dir_incoming
    for d in (dir_et, dir_in, settings.dir_passed, settings.dir_rework, settings.dir_reports):
        Path(d).mkdir(parents=True, exist_ok=True)

    pend_e, pend_i = Pending(), Pending()
    obs = PollingObserver(timeout=settings.watch_poll_interval_s)
    obs.schedule(Handler("etalon", pend_e), str(dir_et), recursive=True)
    obs.schedule(Handler("incoming", pend_i), str(dir_in), recursive=True)
    obs.start()
    log.info("Watcher запущен: etalony=%s incoming=%s (offline mode)", dir_et, dir_in)

    # полная переиндексация эталонов при первом старте
    reindex_all.delay()

    last_reconcile = time.time()
    try:
        while True:
            time.sleep(settings.watch_poll_interval_s)
            enqueue_ready("etalon", pend_e.ready())
            enqueue_ready("incoming", pend_i.ready())
            # reconciliation каждые 3600с — ловим пропущенные события на SMB/NFS
            if time.time() - last_reconcile > 3600:
                last_reconcile = time.time()
                initial_scan(dir_et, dir_in, pend_e, pend_i)
    except KeyboardInterrupt:
        pass
    finally:
        obs.stop()
        obs.join()


if __name__ == "__main__":
    main()

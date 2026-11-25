# -*- coding: utf-8 -*-
"""
Модуль проверки (аудита) входящего документа.

Пайплайн (бизнес-логика из ТЗ):
  Шаг 1  Извлечение   -> app.agent.parser.parse_document
  Шаг 2  Контекст     -> гибрид-поиск по /data/01_etalony в Qdrant
  Шаг 3  Аудит        -> Ollama qwen2.5 + строгий системный промпт + JSON-схема
  Шаг 4  Фиксация     -> перемещение оригинала (без изменения!) + отчёт audit.md + SQLite
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from app.agent import storage
from app.agent.checklists import detect_doc_type, get_checklist, quick_regex_facts
from app.agent.indexer import get_indexer
from app.agent.llm import get_ollama
from app.agent.parser import parse_document
from app.agent.schemas import AuditResult, ParsedDocument
from app.agent.settings import settings
from app.config.prompts import AUDIT_SYSTEM_PROMPT, AUDIT_USER_TEMPLATE, RAG_QUERY_PROMPT, REPORT_TEMPLATE

log = logging.getLogger("agent.audit")


# ---------------------------------------------------------------- шаг 2: контекст
def gather_regulations(doc: ParsedDocument) -> list[dict]:
    """Релевантные выдержки регламентов: запросы из чек-листа + автозапросы LLM."""
    idx = get_indexer()
    ollama = get_ollama()
    queries: list[str] = []

    cl = None
    try:
        _, cl_name = detect_doc_type(doc.filename, doc.plain_text[:2000])
        cl = get_checklist(cl_name)
        queries += [r.query for r in cl.regulation_refs]
    except Exception as e:  # noqa: BLE001
        log.warning("Чек-лист не загружен: %s", e)

    # автодополнение запросами самой модели (дёшево, локально)
    try:
        auto = ollama.complete_lines(RAG_QUERY_PROMPT.format(snippet=doc.plain_text[:1500]), n=3)
        queries += [q for q in auto if 8 < len(q) < 200]
    except Exception as e:  # noqa: BLE001
        log.warning("Автозапросы RAG недоступны: %s", e)

    if not queries:
        queries = [doc.plain_text[:300]]

    seen: set[tuple[str, int]] = set()
    hits: list[dict] = []
    for q in queries[:6]:
        for h in idx.hybrid_search(q, source_kind="etalon", limit=settings.hybrid_limit):
            key = (h["file"] or "", h["page"] or 0)
            if key in seen:
                continue
            seen.add(key)
            hits.append(h)
    hits.sort(key=lambda x: -x["score"])
    return hits[:20]


# ---------------------------------------------------------------- шаг 3: сборка промпта
def _fmt_block(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) or "—"


def build_user_prompt(doc: ParsedDocument, regulations: list[dict],
                      checklist_name: str, doc_type: str) -> str:
    cl = get_checklist(checklist_name)
    req_lines = _fmt_block([f"{r.id} [{r.severity}] {r.text.strip()}"
                            for r in (cl.requirements + cl.extra_checks)])
    calc_lines = _fmt_block([f"{c.id}: {c.rule} (допуск ±{c.tolerance_rub} руб.)"
                             for c in cl.calculations])
    reg_lines = "\n\n".join(
        f"[{h['file']} | раздел: {h.get('section') or '—'} | стр. {h.get('page') or '?'}]\n{h['text']}"
        for h in regulations) or "(нет релевантных регламентов — проверять только по чек-листу)"

    facts = quick_regex_facts(doc.plain_text)
    hints = ("ДЕТЕРМИНИРОВАННЫЕ НАБЛЮДЕНИЯ (regex, используй как подсказку, проверяй сам):\n"
             + "\n".join(f"- {k}: {v}" for k, v in facts.items()))

    md = doc.markdown
    if len(md) > 24000:  # защита контекста: режем середину, оставляем начало/конец и таблицы
        tables = "\n\n".join(t.markdown for t in doc.tables)
        md = md[:14000] + "\n[...сокращено...]\n" + md[-6000:] + "\n\nТАБЛИЦЫ ПОЛНОСТЬЮ:\n" + tables[:4000]

    return AUDIT_USER_TEMPLATE.format(
        filename=doc.filename, doc_type=doc_type, checklist_title=cl.title,
        requisites_block=_fmt_block(cl.required_requisites),
        calculations_block=calc_lines, requirements_block=req_lines,
        regulations_block=reg_lines, document_md=md,
    ) + "\n\n" + hints


# ---------------------------------------------------------------- шаг 4: отчёт
def render_report(doc: ParsedDocument, res: AuditResult, final_path: Path,
                  report_path: Path, duration: float, model: str) -> str:
    badge = {"pass": "✅", "warning": "⚠️", "fail": "❌"}[res.status]
    fb = []
    for i, f in enumerate(res.findings, 1):
        reg = (f"Регламент: **{f.regulation.name}, {f.regulation.clause}** (стр. {f.regulation.page})"
               if f.regulation else "Регламент: —")
        fb.append(f"""### {i}. [{f.severity.upper()}] {f.title} (`{f.item_id}`)
- Цитата: «{f.quote}»
- Ожидание: {f.expected or '—'}
- Факт: {f.actual or '—'}
- {reg}
- Исправление: {f.suggestion or '—'}
""")
    cb = []
    for c in res.calculation_check:
        mark = "OK" if c.ok else "РАСХОЖДЕНИЕ"
        cb.append(f"| {c.rule_id} | {mark} | {c.expected if c.expected is not None else '—'} | "
                  f"{c.actual if c.actual is not None else '—'} | {c.note or ''} |")
    calc_block = ("| Правило | Результат | Ожидаемое | Факт | Комментарий |\n|---|---|---|---|---|\n"
                  + "\n".join(cb)) if cb else "Расчёты не выполнялись."

    rf = res.requisites_found
    req_block = _fmt_block([
        f"Дата: {rf.date or 'не найдена'}",
        f"Номер: {rf.number or 'не найден'}",
        f"Стороны: {', '.join(rf.parties) if rf.parties else 'не найдены'}",
        f"Подписи: {rf.signatures or 'не найдены'}",
    ])

    text = REPORT_TEMPLATE.format(
        filename=doc.filename, checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        status_badge=badge, status_upper=res.status.upper(), doc_type=res.doc_type,
        checklist_title=get_checklist(detect_doc_type(doc.filename, doc.plain_text[:2000])[1]).title,
        final_path=str(final_path), llm_model=model,
        summary=res.summary or "—", findings_count=len(res.findings),
        findings_block="\n".join(fb) or "Замечаний нет 🎉",
        calc_block=calc_block, req_block=req_block,
    )
    report_path.write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------- основной вход
def run_audit(path: Path, *, index_incoming: bool = True) -> dict:
    """Проверяет один файл из /data/02_na_proverku. Возвращает итоговый dict."""
    path = Path(path)
    t0 = time.time()
    result: dict = {"file": path.name, "status": "error"}

    # ШАГ 1 — извлечение
    doc: ParsedDocument = parse_document(path)
    if not doc.plain_text.strip():
        # пустой/неразбираемый документ => на доработку с понятной причиной
        dst = settings.dir_rework / path.name
        shutil.move(str(path), str(dst))
        rep = settings.dir_reports / f"{path.stem}_audit.md"
        rep.write_text(f"# Отчёт: {path.name}\n\n❌ FAIL — не удалось извлечь текст "
                       f"(файл повреждён или это изображение без recognisable текста).\n",
                       encoding="utf-8")
        storage.record_audit(filename=path.name, doc_hash=doc.doc_hash, doc_type="unknown",
                             checklist="-", status="fail", findings=[], summary="Пустой текст",
                             final_path=str(dst), report_path=str(rep), model="-",
                             duration_s=time.time() - t0)
        return {"file": path.name, "status": "fail", "reason": "empty_text", "final": str(dst)}

    # индексация копии во «входящую» базу знаний (для ручных вопросов через WebUI)
    if index_incoming:
        try:
            get_indexer().index_document(doc, source_kind="incoming")
        except Exception as e:  # noqa: BLE001
            log.warning("Индексация входящего '%s' не удалась: %s", path.name, e)

    # ШАГ 2 — контекст регламентов
    doc_type, checklist_name = detect_doc_type(path.name, doc.plain_text[:2000])
    regulations = gather_regulations(doc)

    # ШАГ 3 — LLM-аудит (structured output)
    user_prompt = build_user_prompt(doc, regulations, checklist_name, doc_type)
    res: AuditResult = get_ollama().audit_document(AUDIT_SYSTEM_PROMPT, user_prompt)

    # ШАГ 4 — фиксация результата БЕЗ изменения оригинала
    ok, bucket = res.derive_final_dir_status()
    dst_dir = settings.dir_passed if ok else settings.dir_rework
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = _unique_dst(dst_dir, path.name)
    shutil.move(str(path), str(dst))          # move, не overwrite

    rep_main = settings.dir_reports / f"{path.stem}_audit.md"
    rep_side = dst.with_name(f"{dst.stem}_audit.md")   # дубликат рядом с файлом
    text = render_report(doc, res, dst, rep_main, time.time() - t0, settings.llm_model)
    rep_side.write_text(text, encoding="utf-8")

    storage.record_audit(filename=path.name, doc_hash=doc.doc_hash, doc_type=doc_type,
                         checklist=checklist_name, status=res.status, findings=res.findings,
                         summary=res.summary, final_path=str(dst), report_path=str(rep_main),
                         model=settings.llm_model, duration_s=time.time() - t0)

    result.update(status=res.status, findings=len(res.findings), critical=res.critical_count,
                  final=str(dst), report=str(rep_main))
    log.info("Аудит завершён: %s -> %s (%.1fs)", path.name, res.status, time.time() - t0)
    return result


def _unique_dst(d: Path, name: str) -> Path:
    p = d / name
    if not p.exists():
        return p
    stem, suf = Path(name).stem, Path(name).suffix
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return d / f"{stem}_{ts}{suf}"


def reindex_etalon(path: Path) -> int:
    """Индексация/переиндексация файла эталонов. Возвращает число чанков."""
    doc = parse_document(path)
    if storage.is_known_hash(doc.filename, doc.doc_hash):
        log.info("Эталон '%s' не изменился — пропускаю", path.name)
        return 0
    n = get_indexer().index_document(doc, source_kind="etalon", source_path=str(path))
    storage.upsert_indexed(doc.filename, "etalon", doc.doc_hash, n)
    return n


def unindex_etalon(filename: str) -> None:
    get_indexer().delete_document(filename, "etalon")
    storage.remove_indexed(filename)

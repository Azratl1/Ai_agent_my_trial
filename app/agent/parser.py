# -*- coding: utf-8 -*-
"""
Парсер документов: Docling (структура/таблицы -> Markdown) + локальный OCR для сканов.

Важно: все импорты внешних ML-библиотек — ленивые и с try/except, чтобы модуль
оставался импортируемым и тестируемым на машине без тяжёлых зависимостей.
В контейнере worker они установлены (см. services/worker/Dockerfile).
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from app.agent.schemas import Chunk, ParsedDocument, ParsedTable
from app.agent.settings import settings

log = logging.getLogger("agent.parser")

_MD_TABLE_RE = re.compile(r"(^\|.*\|\s*$\n(?:\|[\s:\-\|]+\|\s*$\n(?:\|.*\|\s*$\n?)*)", re.M)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------- docling
_doc_converter = None


def _get_converter():
    """Ленивая инициализация DoclingConverter в полностью офлайн-режиме."""
    global _doc_converter
    if _doc_converter is not None:
        return _doc_converter
    from docling.document_converter import DocumentConverter  # type: ignore
    from docling.datamodel.base_models import InputFormat  # type: ignore
    from docling.datamodel.pipeline_options import PdfPipelineOptions  # type: ignore

    opts = PdfPipelineOptions()
    opts.do_ocr = False          # OCR подключаем сами ниже — детерминированно и офлайн
    opts.do_table_structure = True
    opts.table_structure_options.do_cell_matching = True
    _doc_converter = DocumentConverter(
        format_options={InputFormat.PDF: opts}
    )
    return _doc_converter


def _docling_parse(path: Path):
    conv = _get_converter()
    result = conv.convert(str(path))
    doc = result.document
    md = doc.export_to_markdown()
    # Таблицы уже представлены в Markdown-выгрузке Docling — извлечём их regex-парсером
    # (_extract_tables_from_md), поэтому здесь возвращаем пустой список.
    try:
        pages = max(len(list(doc.iter_pages())), 1)
    except Exception:
        pages = 1
    return md, [], pages


# --------------------------------------------------------------------- ocr
def _ocr_image_or_scan(path: Path) -> tuple[str, str]:
    """Локальный OCR: RapidOCR (приоритет) -> Tesseract (fallback). Возвращает (text, engine)."""
    img_path = path
    if path.suffix.lower() == ".pdf":
        # растрируем страницы PDF в PNG через pypdfium2 (без внешних сервисов)
        import pypdfium2 as pdfium  # type: ignore
        tmpdir = path.with_suffix(".pages")
        tmpdir.mkdir(exist_ok=True)
        pdf = pdfium.PdfDocument(str(path))
        imgs = []
        for i in range(len(pdf)):
            bmp = pdf[i].render(scale=2.5).to_pil()
            out = tmpdir / f"page_{i+1}.png"
            bmp.save(out)
            imgs.append(out)
        img_path = imgs[0]
        extra = imgs[1:]
    else:
        extra = []

    text, engine = "", ""
    langs = settings.ocr_languages.replace("+", "|").split("|")
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        eng = RapidOCR(det_model_path=None, rec_model_path=None)  # веса из офлайн-кэша образа
        res, _ = eng(str(img_path))
        text = "\n".join(line[1] for line in (res or []))
        for p in extra:
            r2, _ = eng(str(p))
            text += "\n" + "\n".join(l[1] for l in (r2 or []))
        engine = "rapidocr"
    except Exception as e:  # noqa: BLE001
        log.warning("RapidOCR недоступен (%s), пробую Tesseract", e)
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
            chunks = [pytesseract.image_to_string(Image.open(str(img_path)), lang=settings.ocr_languages)]
            chunks += [pytesseract.image_to_string(Image.open(str(p)), lang=settings.ocr_languages) for p in extra]
            text, engine = "\n".join(chunks), "tesseract"
        except Exception as e2:  # noqa: BLE001
            log.error("OCR полностью недоступен: %s", e2)
            text, engine = "", "none"
    return text, engine


# --------------------------------------------------------------------- chunking
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$", re.M)


def chunk_markdown(md: str, size: int, overlap: int) -> list[Chunk]:
    """Смысловые чанки по заголовкам Markdown с метаданными раздела."""
    headings = [(m.start(), m.group(2).strip()) for m in _HEADING_RE.finditer(md)]
    chunks: list[Chunk] = []
    order = 0
    sections = []
    pos_bounds = []
    for i, (start, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(md)
        sections.append((title, md[start:end]))
        pos_bounds.append(start)
    if not sections:
        sections = [("", md)]
    for title, body in sections:
        body = body.strip()
        if not body:
            continue
        step = max(size - overlap, 1)
        for j in range(0, len(body), step):
            part = body[j:j + size]
            if len(part.strip()) < 30 and j > 0:
                continue
            order += 1
            page_guess = min(1 + order // 6, 999)
            chunks.append(Chunk(text=part, section=title, page=page_guess, order=order))
            if j + size >= len(body):
                break
    return chunks


def _extract_tables_from_md(md: str) -> list[ParsedTable]:
    tables = []
    for m in _MD_TABLE_RE.finditer(md):
        block = m.group(1).strip().splitlines()
        if len(block) < 2:
            continue
        headers = [c.strip() for c in block[0].strip("|").split("|")]
        rows = [[c.strip() for c in ln.strip("|").split("|")] for ln in block[2:]]
        tables.append(ParsedTable(markdown="\n".join(block), headers=headers, rows=rows))
    return tables


# --------------------------------------------------------------------- public API
def parse_document(path: Path) -> ParsedDocument:
    """Единая точка входа: PDF/DOCX/XLSX/изображения -> ParsedDocument (Markdown+чанки)."""
    path = Path(path)
    ext = path.suffix.lower()
    md, tables_md, pages, ocr_used, is_scan = "", [], 1, "", False

    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        text, ocr_used = _ocr_image_or_scan(path)
        md, is_scan = text, True
    elif ext in {".docx", ".xlsx", ".pptx"}:
        md, tables_md, pages = _docling_parse(path)   # docling умеет office-форматы
    elif ext == ".pdf":
        md, tables_md, pages = _docling_parse(path)
        chars_per_page = len(re.sub(r"\s", "", md)) / max(pages, 1)
        if settings.force_ocr_for_scans and chars_per_page < settings.scan_detect_min_text_chars:
            log.info("PDF '%s' похож на скан (%.0f симв./стр) — включаю OCR", path.name, chars_per_page)
            text, ocr_used = _ocr_image_or_scan(path)
            if len(text) > len(md):
                md, is_scan = text, True
    else:
        raise ValueError(f"Неподдерживаемый формат: {ext}")

    tables = _extract_tables_from_md(md)
    plain = re.sub(r"[|#*_>`\-]{1,}", " ", md)
    plain = re.sub(r"\s+", " ", plain).strip()
    chunks = chunk_markdown(md, settings.chunk_size, settings.chunk_overlap)

    return ParsedDocument(
        filename=path.name, path=str(path), doc_hash=sha256_file(path),
        is_scan=is_scan, ocr_used=ocr_used, markdown=md, plain_text=plain,
        tables=tables,
        chunks=chunks, pages=pages,
    )

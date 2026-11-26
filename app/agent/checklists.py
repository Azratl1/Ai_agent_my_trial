# -*- coding: utf-8 -*-
"""Загрузка чек-листов из YAML (hot-reload по mtime)."""
from __future__ import annotations

import fnmatch
import re
import threading
from pathlib import Path

import yaml

from app.agent.schemas import ChecklistsConfig, Checklist
from app.agent.settings import settings

_lock = threading.Lock()
_cache: tuple[float, ChecklistsConfig] | None = None


def load_checklists(path: Path | None = None) -> ChecklistsConfig:
    global _cache
    p = path or settings.checklists_path
    mtime = p.stat().st_mtime if p.exists() else 0.0
    with _lock:
        if _cache and _cache[0] == mtime:
            return _cache[1]
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        cfg = ChecklistsConfig.model_validate(raw or {"checklists": {}})
        _cache = (mtime, cfg)
        return cfg


def detect_doc_type(filename: str, text_head: str = "") -> tuple[str, str]:
    """Возвращает (type_name, checklist_name) по правилам doc_type_rules."""
    cfg = load_checklists()
    name_l = filename.lower()
    head_l = text_head.lower()[:2000]
    for rule in cfg.doc_type_rules:
        if rule.type == "default":
            continue
        for pat in rule.match_name:
            if pat.lower() in name_l or pat.lower() in head_l:
                return rule.type, rule.checklist
    return "default", cfg.default_checklist


def get_checklist(checklist_name: str) -> Checklist:
    return load_checklists().resolve(checklist_name)


# ---------- regex-хелперы для детерминированных проверок (до/после LLM) -------
RE_DATE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")
RE_NUMBER = re.compile(r"(?:№|No\.?|#)\s*[\wА-Яа-я\-/]+")
RE_SUM_DIGIT = re.compile(r"\b\d{1,3}(?:[\s\xa0]\d{3})*,\d{2}\b(?:\s*(?:руб|₽))?", re.I)
RUS_NUM_WORDS = (
    r"(нечетырехпятишести)?(двухтрехчетырехпятишести)?(один|два|три|четыре|пять|шесть|семь|восемь|девять|"
    r"десять|одиннадцать|двенадцать|тринадцать|четырнадцать|пятнадцать|шестнадцать|семнадцать|восемнадцать|"
    r"девятнадцать|двадцать|тридцать|сорок|пятьдесят|шестьдесят|семьдесят|восемьдесят|девяносто|"
    r"сто|двести|триста|четыреста|пятьсот|шестьсот|семьсот|восемьсот|девятьсот)[a-яё]*"
)
RE_SUM_WORDS = re.compile(rf"\b({RUS_NUM_WORDS}(?:\s+{RUS_NUM_WORDS})*)\s+(рублей|рубль|рубля)", re.I)


def quick_regex_facts(text: str) -> dict:
    """Дешёвые детерминированные наблюдения — передаются LLM как подсказки."""
    dates = RE_DATE.findall(text)[:5]
    numbers = RE_NUMBER.findall(text)[:5]
    sums = RE_SUM_DIGIT.findall(text)[:10]
    words_sums = [m.group(1) for m in RE_SUM_WORDS.finditer(text)][:5]
    return {
        "dates_found": dates,
        "numbers_found": numbers,
        "sums_digits": sums,
        "sums_words": words_sums,
        "has_signature_marker": bool(re.search(r"(Подпись|подпис|М\.?П\.?|печать)", text, re.I)),
    }

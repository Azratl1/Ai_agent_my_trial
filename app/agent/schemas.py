# -*- coding: utf-8 -*-
"""Pydantic-схемы: результат парсинга, чек-листы, ответ LLM-аудита."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


Severity = Literal["critical", "warning", "info"]
Status = Literal["pass", "warning", "fail"]


# ------------------------------------------------------------------ parsing
class ParsedTable(BaseModel):
    page: int = 1
    caption: str = ""
    markdown: str = ""          # таблица в Markdown
    headers: list[str] = []
    rows: list[list[str]] = []


class Chunk(BaseModel):
    text: str
    section: str = ""           # ближайший заголовок раздела
    page: int = 1
    order: int = 0


class ParsedDocument(BaseModel):
    filename: str
    path: str
    doc_hash: str               # sha256 — дедупликация и детект изменений
    is_scan: bool = False
    ocr_used: str = ""          # rapidocr | tesseract | '' 
    markdown: str = ""
    plain_text: str = ""
    tables: list[ParsedTable] = []
    chunks: list[Chunk] = []
    pages: int = 1
    parsed_at: datetime = Field(default_factory=datetime.utcnow)


# ------------------------------------------------------------------ checklists
class CalcRule(BaseModel):
    id: str
    rule: str
    tolerance_rub: float = 0.01


class Requirement(BaseModel):
    id: str
    severity: Severity = "warning"
    text: str


class RegulationRef(BaseModel):
    collection: str = "etalony"
    query: str
    top_k: int = 8


class Checklist(BaseModel):
    title: str
    extends: list[str] = []
    required_requisites: list[str] = []
    calculations: list[CalcRule] = []
    regulation_refs: list[RegulationRef] = []
    extra_checks: list[Requirement] = []
    requirements: list[Requirement] = []   # для базового universal


class DocTypeRule(BaseModel):
    type: str
    match_name: list[str] = []
    checklist: str


class ChecklistsConfig(BaseModel):
    version: int = 1
    updated: str = ""
    default_checklist: str = "universal"
    doc_type_rules: list[DocTypeRule] = []
    checklists: dict[str, Checklist]

    def resolve(self, name: str, _seen: set[str] | None = None) -> Checklist:
        """Сливает чек-лист с родителями из extends (рекурсивно, без циклов)."""
        _seen = _seen or set()
        if name in _seen or name not in self.checklists:
            return self.checklists.get(name, Checklist(title=name))
        _seen.add(name)
        cl = self.checklists[name].model_copy(deep=True)
        for parent in cl.extends:
            p = self.resolve(parent, _seen)
            cl.required_requisites = p.required_requisites + cl.required_requisites
            cl.calculations = p.calculations + cl.calculations
            cl.requirements = p.requirements + cl.requirements
            cl.extra_checks = p.extra_checks + cl.extra_checks
            cl.regulation_refs = p.regulation_refs + cl.regulation_refs
        return cl


# ------------------------------------------------------------------ audit result
class RegulationHit(BaseModel):
    name: Optional[str] = None
    clause: Optional[str] = None
    page: Optional[int] = None


class Finding(BaseModel):
    item_id: str
    severity: Severity
    title: str
    quote: str = ""
    expected: str = ""
    actual: str = ""
    regulation: Optional[RegulationHit] = None
    suggestion: str = ""


class CalcCheck(BaseModel):
    rule_id: str
    ok: bool
    expected: Optional[float] = None
    actual: Optional[float] = None
    note: str = ""


class RequisitesFound(BaseModel):
    date: Optional[str] = None
    number: Optional[str] = None
    parties: list[str] = []
    signatures: Optional[str] = None


class AuditResult(BaseModel):
    """Строгая JSON-схема ответа LLM (используется как response_format/json_schema)."""
    model_config = {"json_schema_extra": {"additionalProperties": False}}

    status: Status
    doc_type: str = "default"
    requisites_found: RequisitesFound = Field(default_factory=RequisitesFound)
    findings: list[Finding] = []
    calculation_check: list[CalcCheck] = []
    summary: str = ""

    @field_validator("status")
    @classmethod
    def consistency(cls, v, info):
        return v

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    def derive_final_dir_status(self) -> tuple[bool, str]:
        """(ok?, 'passed'|'rework') — критичных нет => passed (warning тоже пропускаем, но помечаем)."""
        return (self.status != "fail", "passed" if self.status != "fail" else "rework")


# JSON Schema для structured output Ollama (format=json_schema)
AUDIT_JSON_SCHEMA: dict = AuditResult.model_json_schema()

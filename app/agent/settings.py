# -*- coding: utf-8 -*-
"""Настройки приложения (pydantic-settings). Все значения — из .env / ENV."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # offline
    offline_mode: bool = True
    hf_hub_offline: str = "1"

    # dirs
    dir_etalony: Path = Path("/data/01_etalony")
    dir_incoming: Path = Path("/data/02_na_proverku")
    dir_passed: Path = Path("/data/03_provereno")
    dir_rework: Path = Path("/data/04_na_dorabotku")
    dir_reports: Path = Path("/data/05_otchety")

    # services (только внутренние DNS-имена compose!)
    ollama_base_url: str = "http://ollama:11434"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""
    redis_password: str = ""
    celery_broker_url: str = "redis://:changeme-redis@redis:6379/0"
    celery_result_backend: str = "redis://:changeme-redis@redis:6379/1"

    # models
    llm_model: str = "qwen2.5:14b"
    llm_model_fallback: str = "qwen2.5:7b"
    embed_model: str = "bge-m3"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    llm_timeout_s: int = 300

    # collections / chunking
    qdrant_collection_etalony: str = "etalony"
    qdrant_collection_docs: str = "incoming_docs"
    hybrid_top_dense: int = 24
    hybrid_top_sparse: int = 24
    hybrid_limit: int = 12
    chunk_size: int = 900
    chunk_overlap: int = 150

    # watcher
    file_settle_seconds: int = 5
    watch_poll_interval_s: int = 3
    watch_exclude_globs: str = "*.tmp,*.part,~$*,.~lock.*"

    # parser/ocr
    ocr_engine: str = "auto"          # auto|rapidocr|tesseract|none
    ocr_languages: str = "rus+eng"
    scan_detect_min_text_chars: int = 120
    force_ocr_for_scans: bool = True

    # storage / config
    database_url: str = "sqlite:///var/lib/agent/audit.db"
    checklists_path: Path = Path("/app/config/checklists.yaml")
    api_internal_token: str = "changeme"
    paperless_webhook_enabled: bool = False

    supported_exts: set[str] = Field(default={".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff"})

    @property
    def exclude_patterns(self) -> list[str]:
        return [p.strip() for p in self.watch_exclude_globs.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # жёсткая гарантия офлайн-режима при старте процесса
    import os
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    return s


settings = get_settings()

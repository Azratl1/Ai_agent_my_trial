# -*- coding: utf-8 -*-
"""
Клиент Ollama (строго http://ollama:11434 — внутренний DNS compose).

Гарантии offline:
  - никаких внешних хостов (проверка URL при инициализации);
  - structured output через format=json_schema;
  - эмбеддинги bge-m3 + локальный BM25-sparse для гибрида в Qdrant.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

import httpx

from app.agent.schemas import AuditResult
from app.agent.settings import settings

log = logging.getLogger("agent.llm")

_TOKEN_RE = re.compile(r"[а-яёa-z0-9№\-/]+", re.I)


def _assert_internal(url: str) -> None:
    host = httpx.URL(url).host
    allowed = {"ollama", "qdrant", "api", "redis", "postgres", "localhost", "127.0.0.1"}
    if host not in allowed:
        raise RuntimeError(f"OFFLINE GUARD: запрещённый внешний хост {url}")


class OllamaClient:
    def __init__(self, base_url: str | None = None):
        self.base = base_url or settings.ollama_base_url
        _assert_internal(self.base)
        self.http = httpx.Client(base_url=self.base, timeout=settings.llm_timeout_s)

    # ------------------------------------------------------------- chat / audit
    def chat_json(self, system: str, user: str, schema: dict,
                  model: str | None = None) -> dict[str, Any]:
        """Structured output: Ollama >=0.9 поддерживает format={"type":"json_schema",...}."""
        payload = {
            "model": model or settings.llm_model,
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": settings.llm_temperature,
                "num_predict": settings.llm_max_tokens,
            },
            "format": {"type": "json_schema", "schema": schema},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        r = self.http.post("/api/chat", json=payload)
        r.raise_for_status()
        content = r.json()["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise
            return json.loads(m.group(0))

    def audit_document(self, system: str, user: str) -> AuditResult:
        """LLM-аудит с fallback на модель меньшего размера при нехватке VRAM/таймауте."""
        models = [settings.llm_model, settings.llm_model_fallback]
        last_err: Exception | None = None
        from app.agent.schemas import AUDIT_JSON_SCHEMA
        for mdl in models:
            try:
                data = self.chat_json(system, user, AUDIT_JSON_SCHEMA, model=mdl)
                res = AuditResult.model_validate(data)
                log.info("Аудит выполнен моделью %s, статус=%s", mdl, res.status)
                return res
            except (httpx.HTTPError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
                log.warning("Модель %s не справилась (%s)", mdl, e)
            except ValueError as e:  # схема не сошлась — ретраимся один раз на fallback
                last_err = e
                log.warning("Схема ответа невалидна (%s), пробую %s", e, settings.llm_model_fallback)
        raise RuntimeError(f"LLM-аудит не удался: {last_err}")

    # ------------------------------------------------------------- embeddings
    def embed(self, texts: list[str]) -> list[list[float]]:
        r = self.http.post("/api/embed", json={"model": settings.embed_model, "input": texts})
        r.raise_for_status()
        return r.json()["embeddings"]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    # ------------------------------------------------------------- simple completion
    def complete_lines(self, prompt: str, n: int = 3) -> list[str]:
        r = self.http.post("/api/generate", json={
            "model": settings.llm_model_fallback, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.1, "num_predict": 200},
        })
        r.raise_for_status()
        lines = [l.strip(" -•.\t") for l in r.json()["response"].splitlines() if l.strip()]
        return lines[:n]


# ------------------------------------------------------------------ sparse/BM25
_RU_STOP = {"и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
            "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
            "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще", "нет",
            "о", "из", "ему", "теперь", "when", "the", "of", "and", "to", "in"}


def token_index(token: str) -> int:
    """Стабильный кросс-процессный индекс токена (hashlib, не встроенный hash())."""
    import hashlib
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big") >> 2


def bm25_sparse(text: str) -> tuple[list[int], list[float]]:
    """Локальный BM25-подобный sparse-вектор (без внешних библиотек и без сети).

    Возвращает (indices, values) для qdrant_models.SparseVector.
    Токенизация нижний регистр + стоп-слова; вес = нормированный TF
    (пересечение с dense даёт RRF-Fusion в Qdrant Hybrid Fusion Query).
    """
    toks = [t.lower() for t in _TOKEN_RE.findall(text)
            if t.lower() not in _RU_STOP and len(t) > 1]
    tf = Counter(toks)
    total = max(len(toks), 1)
    pairs = sorted(((token_index(tok), 1.0 + cnt / total) for tok, cnt in tf.items()))
    indices = [i for i, _ in pairs]
    values = [v for _, v in pairs]
    return indices, values


_client: OllamaClient | None = None


def get_ollama() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client

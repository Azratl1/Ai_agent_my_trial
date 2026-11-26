# -*- coding: utf-8 -*-
"""
Индексатор Qdrant: гибрид dense (bge-m3) + sparse (BM25-хэш), payload с метаданными,
удаление векторов при удалении файла из эталонов.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient  # type: ignore
from qdrant_client.models import (  # type: ignore
    Distance, FieldCondition, Filter, HNSWConfig, MatchValue, PointStruct,
    SparseVector, SparseVectorParams, VectorParams, Fusion,
)

from app.agent.llm import bm25_sparse, get_ollama
from app.agent.schemas import ParsedDocument
from app.agent.settings import settings

log = logging.getLogger("agent.indexer")


class Indexer:
    def __init__(self) -> None:
        assert "qdrant" in settings.qdrant_url or "localhost" in settings.qdrant_url, \
            "OFFLINE GUARD: Qdrant доступен только по внутреннему DNS"
        self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None,
                                   check_compatibility=False)
        self.ollama = get_ollama()

    # ------------------------------------------------------------- collections
    def ensure_collections(self) -> None:
        for name in (settings.qdrant_collection_etalony, settings.qdrant_collection_docs):
            if not self.client.collection_exists(name):
                self.client.create_collection(
                    collection_name=name,
                    vectors_config={
                        "dense": VectorParams(size=1024, distance=Distance.COSINE,
                                              hnsw_config=HNSWConfig(m=16, ef_construct=200)),
                    },
                    sparse_vectors_config={
                        "sparse": SparseVectorParams(modifier=None),
                    },
                )
                log.info("Создана коллекция %s (dense 1024 + sparse BM25)", name)

    # ------------------------------------------------------------- indexing
    def index_document(self, doc: ParsedDocument, source_kind: str = "etalon",
                       source_path: str | None = None) -> int:
        """Чанки -> dense(bge-m3)+sparse(BM25) -> upsert. Возвращает число точек."""
        coll = settings.qdrant_collection_etalony if source_kind == "etalon" \
            else settings.qdrant_collection_docs
        fname = Path(doc.filename).name
        # идемпотентность: сначала удаляем старые точки этого файла
        self.delete_document(fname, source_kind)

        texts = [f"{c.section}\n{c.text}" if c.section else c.text for c in doc.chunks]
        if not texts:
            return 0
        dense_vecs = self.ollama.embed(texts)
        points: list[PointStruct] = []
        for i, (chunk, dv) in enumerate(zip(doc.chunks, dense_vecs)):
            idx, val = bm25_sparse(chunk.text)
            points.append(PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{fname}:{i}")),
                vector={"dense": dv, "sparse": SparseVector(indices=idx, values=val)},
                payload={
                    "file": fname,
                    "path": source_path or doc.path,
                    "kind": source_kind,
                    "section": chunk.section,
                    "page": chunk.page,
                    "order": chunk.order,
                    "text": chunk.text[:8000],
                    "doc_hash": doc.doc_hash,
                    "is_scan": doc.is_scan,
                },
            ))
        for off in range(0, len(points), 64):
            self.client.upsert(collection_name=coll, points=points[off:off + 64], wait=True)
        log.info("Проиндексировано '%s': %d чанков (%s)", fname, len(points), source_kind)
        return len(points)

    def delete_document(self, filename: str, source_kind: str = "etalon") -> None:
        """Удаление всех векторов файла (при удалении/обновлении документа в /data/01_etalony)."""
        coll = settings.qdrant_collection_etalony if source_kind == "etalon" \
            else settings.qdrant_collection_docs
        try:
            flt = Filter(must=[FieldCondition(key="file", match=MatchValue(value=filename))])
            ids = self._scroll_ids(coll, flt)
            if ids:
                self.client.delete(collection_name=coll, points_selector=ids, wait=True)
            log.info("Удалено %d векторов файла %s из %s", len(ids), filename, coll)
        except Exception as e:  # noqa: BLE001 — коллекции могут ещё не существовать
            log.warning("delete_document(%s): %s", filename, e)

    def _scroll_ids(self, coll: str, flt: Filter) -> list:
        out, off = [], None
        while True:
            pts, off = self.client.scroll(collection_name=coll, scroll_filter=flt,
                                          limit=256, offset=off, with_payload=False)
            out.extend(p.id for p in pts)
            if not off or not pts:
                break
        return out

    # ------------------------------------------------------------- hybrid search
    def hybrid_search(self, query: str, source_kind: str = "etalon", limit: int | None = None,
                      file_filter: str | None = None) -> list[dict[str, Any]]:
        """Плотный+разреженный поиск через Server-side RRF fusion (Qdrant >=1.0)."""
        coll = settings.qdrant_collection_etalony if source_kind == "etalon" \
            else settings.qdrant_collection_docs
        limit = limit or settings.hybrid_limit
        dense_q = self.ollama.embed_one(query)
        idx, val = bm25_sparse(query)
        must = [FieldCondition(key="file", match=MatchValue(value=file_filter))] if file_filter else None
        res = self.client.query_points(
            collection_name=coll,
            prefetch=[
                {"vector": {"name": "dense", "vector": dense_q},
                 "limit": settings.hybrid_top_dense},
                {"sparse_vector": SparseVector(indices=idx, values=val),
                 "limit": settings.hybrid_top_sparse},
            ],
            using=Fusion.RRF,
            query_filter=Filter(must=must) if must else None,
            limit=limit,
            with_payload=True,
        )
        out = []
        for p in res.points:
            pl = p.payload or {}
            out.append({
                "score": round(p.score, 4),
                "file": pl.get("file"),
                "section": pl.get("section"),
                "page": pl.get("page"),
                "text": pl.get("text", ""),
            })
        return out


_indexer: Indexer | None = None


def get_indexer() -> Indexer:
    global _indexer
    if _indexer is None:
        _indexer = Indexer()
    return _indexer

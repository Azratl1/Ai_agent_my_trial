# syntax=docker/dockerfile:1.7
# Celery-worker: Docling + OCR + Qdrant-индексация + LLM-аудит (тяжёлый образ)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    DOCLING_CACHE_DIR=/models/docling SENTENCE_TRANSFORMERS_HOME=/models/hf \
    TESSDATA_PREFIX=/models/ocr/tessdata

RUN apt-get update && apt-get install -y --no-install-recommends \
      libglu1 tesseract-ocr tesseract-ocr-rus fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY services/requirements-base.txt /tmp/req-base.txt
COPY services/requirements-worker.txt /tmp/req-worker.txt
ARG PIP_INDEX_URL=http://pypi.internal.local/simple
RUN pip install --index-url ${PIP_INDEX_URL:-https://pypi.org/simple} -r /tmp/req-base.txt -r /tmp/req-worker.txt || \
    pip install -r /tmp/req-base.txt -r /tmp/req-worker.txt

COPY app /app/app
COPY services/worker/run_worker.py /app/run_worker.py
WORKDIR /app
CMD ["python", "run_worker.py"]

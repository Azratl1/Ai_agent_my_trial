# syntax=docker/dockerfile:1.7
# Watcher-образ: лёгкий (без ML), только watchdog + celery client
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
WORKDIR /app
COPY services/requirements-base.txt /tmp/req-base.txt
ARG PIP_INDEX_URL=http://pypi.internal.local/simple
RUN pip install --index-url ${PIP_INDEX_URL:-https://pypi.org/simple} -r /tmp/req-base.txt || \
    pip install -r /tmp/req-base.txt
COPY app /app/app
CMD ["python", "-m", "app.agent.watcher"]

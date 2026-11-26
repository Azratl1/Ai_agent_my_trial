# syntax=docker/dockerfile:1.7
# Dockerfile для FastAPI-оркестратора (офлайн-образ, без внешних репозиториев в рантайме)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

WORKDIR /app
COPY services/requirements-base.txt /tmp/req-base.txt
# В закрытом контуре --index-url должен указывать на внутренний зеркальный PyPI
ARG PIP_INDEX_URL=http://pypi.internal.local/simple
RUN pip install --index-url ${PIP_INDEX_URL:-https://pypi.org/simple} -r /tmp/req-base.txt || \
    pip install -r /tmp/req-base.txt

COPY app /app/app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8000/internal/health',timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

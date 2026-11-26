#!/usr/bin/env bash
# Обёртка для подготовки офлайн-окружения (см. download_models.py).
# Запускать на машине С доступом в интернет:
#   bash scripts/download_models.sh --all
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1. Проверка docker =="
docker info >/dev/null || { echo "docker недоступен"; exit 1; }

echo "== 2. Скачивание образов, моделей Ollama, Docling, OCR =="
python3 scripts/download_models.py "$@"

echo "== 3. Контрольные суммы bundle =="
( cd offline_bundle && find . -type f ! -name SHA256SUMS -print0 | xargs -0 sha256sum > SHA256SUMS )

echo "Готово. Перенесите ./offline_bundle в закрытый контур и выполните там:"
echo "  bash scripts/import_models.sh"

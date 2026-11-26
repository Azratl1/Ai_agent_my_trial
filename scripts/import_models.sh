#!/usr/bin/env bash
# Импорт офлайн-бандла ВНУТРИ закрытого контура (без интернета).
#   bash scripts/import_models.sh
set -euo pipefail
cd "$(dirname "$0")/.."

BUNDLE=${1:-offline_bundle}
[[ -d "$BUNDLE" ]] || { echo "Нет $BUNDLE — сначала перенесите его из download_models.sh"; exit 1; }

echo "== Проверка контрольных сумм =="
( cd "$BUNDLE" && sha256sum -c SHA256SUMS )

echo "== Импорт docker-образов =="
for tar in "$BUNDLE"/docker/*.tar; do
  [[ -e "$tar" ]] || continue
  docker load -i "$tar"
done

echo "== Импорт моделей Ollama =="
mkdir -p data/ollama/models
if command -v ollama >/dev/null 2>&1; then
  for d in "$BUNDLE"/ollama/*/; do
    [[ -e "$d/model.tar" ]] || continue
    img=$(tar -xOf "$d/model.tar" manifest.json 2>/dev/null | python3 -c \
      'import json,sys; m=json.load(sys.stdin); print(m[0].get("RepoTags",[m[0]["Config"]])[0])' || true)
    [[ -n "${img:-}" ]] && docker load -i "$d/model.tar"
  done
fi
# Основной путь: просто копируем blobs в volume ./data/ollama
cp -rn "$BUNDLE"/ollama/blobs/* data/ollama/models/ 2>/dev/null || true

echo "== Импорт весов Docling / HF =="
mkdir -p data/etalony-cache/hf data/etalony-cache/docling
cp -rn "$BUNDLE"/hf/* data/etalony-cache/hf/ 2>/dev/null || true
export HF_HOME="$PWD/data/etalony-cache/hf"

echo "== Импорт OCR (RapidOCR + Tesseract rus/eng) =="
mkdir -p data/etalony-cache/ocr/rapidocr /usr/share/tesseract-ocr/5/tessdata 2>/dev/null || true
cp -n "$BUNDLE"/ocr/rapidocr/*.onnx data/etalony-cache/ocr/rapidocr/ 2>/dev/null || true
cp -n "$BUNDLE"/ocr/tessdata/*.traineddata data/etalony-cache/ocr/tessdata/ 2>/dev/null || true
export TESSDATA_PREFIX="$PWD/data/etalony-cache/ocr/tessdata"

echo "== Создание структуры рабочих папок =="
mkdir -p data/documents/{01_etalony,02_na_proverku,03_provereno,04_na_dorabotku,05_otchety}
chmod -R a+rwX data/documents || true

echo "== Конвертация compose-переменных в offline-режим =="
[[ -f .env ]] || cp .env.example .env
grep -q '^HF_HUB_OFFLINE=1' .env || cat >> .env <<'EOF'
OFFLINE_MODE=1
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
EOF

echo "Импорт завершён. Запуск:  docker compose up -d"
echo "Система теперь полностью автономна (никакого egress)."

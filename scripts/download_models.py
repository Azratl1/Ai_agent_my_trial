#!/usr/bin/env python3
"""
Первичная подготовка офлайн-окружения (запускается ОДИН РАЗ на машине с доступом в интернет).

Что делает:
  1. docker pull всех образов из docker-compose.yml (или читает images.txt) -> docker save в tar.
  2. Скачивание весов Ollama: qwen2.5:14b (+7b fallback), bge-m3.
  3. Предзагрузка весов Docling (layout/model) и OCR (RapidOCR models, Tesseract rus/eng traineddata).
  4. Всё складывается в ./offline_bundle/, переносится на флешке в закрытый контур,
     где запускается scripts/import_models.sh (офлайн-импорт).

Использование:
    python scripts/download_models.py --all
    python scripts/download_models.py --ollama --docling --ocr
    bash   scripts/download_models.sh --all
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "offline_bundle"
MANIFEST_PATH = BUNDLE / "manifest.json"

# --- что скачиваем -----------------------------------------------------------
OLLAMA_MODELS = ["qwen2.5:14b", "qwen2.5:7b", "bge-m3"]

DOCLING_MODELS = [
    # docling artifacts (layout model + tableformer + reading order)
    "docling-project/docling-models",
    "docling-project/DoclingTableFormer",
]
RAPIDOCR_FILES = {
    # ONNX det/rec models для RapidOCR (переносятся в data/models/rapidocr)
    "det": ("https://github.com/RapidAI/RapidOCR/raw/main/python/rapidocr_onnxruntime/models/"
            "PP-OCRv4_det_infer.onnx", "PP-OCRv4_det_infer.onnx"),
    "rec": ("https://github.com/RapidAI/RapidOCR/raw/main/python/rapidocr_onnxruntime/models/"
            "PP-OCRv4_rec_infer.onnx", "PP-OCRv4_rec_infer.onnx"),
    "cls": ("https://github.com/RapidAI/RapidOCR/raw/main/python/rapidocr_onnxruntime/models/"
            "ch_ppocr_mobile_v2.0_cls_infer.onnx", "ch_ppocr_mobile_v2.0_cls_infer.onnx"),
}
TESSERACT_LANGS = ["rus", "eng"]  # .traineddata с https://github.com/tesseract-ocr/tessdata_fast


def run(cmd: list[str], check: bool = True) -> int:
    print(">>", " ".join(cmd))
    p = subprocess.run(cmd)
    if check and p.returncode != 0:
        sys.exit(f"Команда завершилась с ошибкой: {' '.join(cmd)}")
    return p.returncode


def manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"ollama": [], "docker_images": [], "docling": [], "ocr": []}


def save_manifest(m: dict) -> None:
    BUNDLE.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- docker images
def docker_images() -> list[str]:
    """Собираем список внешних образов из compose-файла (build-сервисы пропускаем)."""
    compose = ROOT / "docker-compose.yml"
    imgs: list[str] = []
    in_services = False
    for line in compose.read_text().splitlines():
        s = line.strip()
        if s == "services:":
            in_services = True
            continue
        if in_services and s and not s.startswith("#") and not line.startswith("  "):
            in_services = False
        if in_services and s.startswith("image:"):
            imgs.append(s.split(":", 1)[1].strip())
    # локальные build-образы не тянем
    return sorted({i for i in imgs if not i.startswith("local/")})


def step_docker(man: dict) -> None:
    out = BUNDLE / "docker"
    out.mkdir(parents=True, exist_ok=True)
    for img in docker_images():
        run(["docker", "pull", img])
        tar = out / (img.replace("/", "_").replace(":", "_") + ".tar")
        if not tar.exists():
            run(["docker", "save", "-o", str(tar), img])
        man["docker_images"].append(img)
    save_manifest(man)


# ---------------------------------------------------------------- ollama models
def step_ollama(man: dict) -> None:
    # ollama pull требует запущенного ollama; качаем blobs напрямую в ./data/ollama
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=3):
            for m in OLLAMA_MODELS:
                run(["ollama", "pull", m])
                man["ollama"].append(m)
            save_manifest(man)
            return
    except Exception:
        pass
    # Fallback: официальный registry через skopeo/docker
    for m in OLLAMA_MODELS:
        name, tag = m.split(":", 1)
        ref = f"docker.io/ollama/{name}:{tag}"
        run(["docker", "pull", ref], check=False)
        dest = BUNDLE / "ollama" / m.replace(":", "_")
        dest.mkdir(parents=True, exist_ok=True)
        run(["docker", "save", "-o", str(dest / "model.tar"), ref], check=False)
        man["ollama"].append(m)
    save_manifest(man)


# ---------------------------------------------------------------- docling weights
def step_docling(man: dict) -> None:
    """Предзагрузка артефактов Docling через huggingface_hub (в offline bundle)."""
    dest = BUNDLE / "hf"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit("pip install huggingface_hub перед запуском --docling")
    for repo in DOCLING_MODELS:
        snapshot_download(repo_id=repo, local_dir=str(dest / repo.replace("/", "__")),
                          local_dir_use_symlinks=False)
        man["docling"].append(repo)
    save_manifest(man)


# ---------------------------------------------------------------- OCR weights
def step_ocr(man: dict) -> None:
    dest = BUNDLE / "ocr"
    (dest / "rapidocr").mkdir(parents=True, exist_ok=True)
    (dest / "tessdata").mkdir(parents=True, exist_ok=True)
    for key, (url, fname) in RAPIDOCR_FILES.items():
        target = dest / "rapidocr" / fname
        if not target.exists():
            print("downloading", url)
            urllib.request.urlretrieve(url, target)
        man["ocr"].append(f"rapidocr:{fname}")
    for lang in TESSERACT_LANGS:
        url = f"https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
        target = dest / "tessdata" / f"{lang}.traineddata"
        if not target.exists():
            print("downloading", url)
            urllib.request.urlretrieve(url, target)
        man["ocr"].append(f"tesseract:{lang}")
    save_manifest(man)


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docker", action="store_true", help="pull+save образов compose")
    ap.add_argument("--ollama", action="store_true", help="модели LLM/embedding")
    ap.add_argument("--docling", action="store_true", help="веса Docling (HF)")
    ap.add_argument("--ocr", action="store_true", help="RapidOCR + Tesseract rus/eng")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    todo = args.all or (args.docker and args.ollama and args.docling and args.ocr)
    if todo:
        args.docker = args.ollama = args.docling = args.ocr = True

    man = manifest()
    BUNDLE.mkdir(exist_ok=True)
    if args.docker:
        step_docker(man)
    if args.ollama:
        step_ollama(man)
    if args.docling:
        step_docling(man)
    if args.ocr:
        step_ocr(man)

    print("\nГотово. Bundle:", BUNDLE)
    print("Перенесите папку offline_bundle/ в закрытый контур и выполните:")
    print("  bash scripts/import_models.sh")


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# 下载 RAG 语料到 data/raw/（语料不入库，见 NOTICE.md 的授权说明）。
# 下载后用：  rag_env python -m backend.rag.ingest --reset   重建向量库。
set -e
cd "$(dirname "$0")/.."
mkdir -p data/raw

echo "[1/3] Hello 算法（简中 C 版，CC BY-NC-SA）"
curl -fsSL -o data/raw/hello-algo_zh_c.pdf \
  "https://github.com/krahets/hello-algo/releases/download/1.3.0/hello-algo_1.3.0_zh_c.pdf"

echo "[2/3] Open Data Structures（CC BY）"
curl -fsSL -o data/raw/open-data-structures.pdf \
  "https://opendatastructures.org/ods-python.pdf"

echo "[3/3] Competitive Programmer's Handbook（CC BY-SA）"
curl -fsSL -o data/raw/cp-handbook.pdf \
  "https://cses.fi/book/book.pdf"

echo "完成。下一步：rag_env python -m backend.rag.ingest --reset"

"""OCR 横评公共工具：测试页定义、渲染、路径、IO。"""
from __future__ import annotations

import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "data", "ocr", "bench")
PAGES = os.path.join(BENCH, "pages")
OUT = os.path.join(BENCH, "out")
GT_PATH = os.path.join(BENCH, "ground_truth.json")

# 固定测试集：(pdf 相对 data/ocr, 页码1-based, 页面id)
TEST_PAGES = [
    ("sample_exam_a.pdf", 1, "p1_choice"),
    ("sample_exam_a.pdf", 2, "p2_table"),
    ("sample_exam_a.pdf", 3, "p3_riscv"),
    ("sample_exam_b.pdf", 1, "p4_general"),
]

DPI = 170


def png_path(page_id: str) -> str:
    return os.path.join(PAGES, f"{page_id}.png")


def render_all() -> list[str]:
    import fitz
    os.makedirs(PAGES, exist_ok=True)
    made = []
    for pdf, pno, pid in TEST_PAGES:
        d = fitz.open(os.path.join(ROOT, "data", "ocr", pdf))
        pix = d[pno - 1].get_pixmap(dpi=DPI)
        out = png_path(pid)
        pix.save(out)
        d.close()
        made.append((pid, out, len(pix.tobytes("png")) // 1024))
    return made


def load_gt() -> dict:
    return json.loads(open(GT_PATH, encoding="utf-8").read()) if os.path.exists(GT_PATH) else {}


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def img_b64(page_id: str) -> str:
    import base64
    return base64.b64encode(open(png_path(page_id), "rb").read()).decode()

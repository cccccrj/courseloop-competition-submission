"""横评·本地方案：PP-StructureV3 / MinerU / GOT-OCR。用 ocr_env(数据盘)跑，CPU 优先，不碰线上。

用法(ocr_env python)：
    /opt/projects/ocr_env/bin/python scripts/ocr_bench/run_local.py --method pp-structurev3
产出：data/ocr/bench/out/<method>/<page_id>.md + _meta.json(延迟)
"""
from __future__ import annotations

import argparse
import os
import time

# 模型缓存指数据盘（/ 仅 6G）
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", "/opt/projects/.paddlex")
os.environ.setdefault("HF_HOME", "/opt/projects/.hf")
os.environ.setdefault("FLAGS_use_mkldnn", "0")  # paddle3.3 PIR+OneDNN 有 bug，关 MKLDNN 规避

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "data", "ocr", "bench")
PAGES = os.path.join(BENCH, "pages")
OUT = os.path.join(BENCH, "out")
TEST_IDS = ["p1_2021_choice", "p2_2021_table", "p3_2021_riscv", "p1_2023_general"]


def run_pp_structurev3():
    from paddleocr import PPStructureV3
    import json
    # mobile 轻量配置：实际本地部署会用的档位。关公式/图表/印章（最慢且手写页边际低），
    # 保留表格（测试集有一张表页）。server 大模型走 CPU 慢到不实用（实测 926%CPU/16min）。
    cfg = dict(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        layout_detection_model_name="PP-DocLayout-S",
        use_formula_recognition=False,
        use_chart_recognition=False,
        use_seal_recognition=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,  # 关 MKLDNN 规避 paddle 3.x PIR+OneDNN bug
    )
    try:
        pipe = PPStructureV3(**cfg)
    except TypeError:  # 版本参数差异时退回去掉 enable_mkldnn
        cfg.pop("enable_mkldnn", None)
        pipe = PPStructureV3(**cfg)
    odir = os.path.join(OUT, "pp-structurev3"); os.makedirs(odir, exist_ok=True)
    meta = {}
    for pid in TEST_IDS:
        img = os.path.join(PAGES, f"{pid}.png")
        t = time.time()
        try:
            results = pipe.predict(input=img)
            md_parts = []
            for res in results:
                md = getattr(res, "markdown", None)
                if isinstance(md, dict):
                    md = md.get("markdown_texts", "") or md.get("text", "")
                md_parts.append(md if isinstance(md, str) else str(md))
            open(os.path.join(odir, f"{pid}.md"), "w", encoding="utf-8").write("\n".join(md_parts))
            meta[pid] = {"latency_s": round(time.time() - t, 1)}
            print(f"[pp-structurev3/{pid}] {meta[pid]['latency_s']}s", flush=True)
        except Exception as e:  # noqa: BLE001
            meta[pid] = {"error": str(e)[:150]}
            print(f"[pp-structurev3/{pid}] ERR {str(e)[:120]}", flush=True)
    json.dump(meta, open(os.path.join(odir, "_meta.json"), "w"), ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", required=True, choices=["pp-structurev3", "mineru", "got-ocr2"])
    args = ap.parse_args()
    if args.method == "pp-structurev3":
        run_pp_structurev3()
    else:
        print(f"{args.method}: 见 run_local 扩展(MinerU/GOT 单独装)")


if __name__ == "__main__":
    main()

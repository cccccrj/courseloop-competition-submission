"""VLM 版 OCR 测试 + 与 PaddleOCR(PP-OCR) 结果对比。

针对扫描真题(双栏/公式/表格/手写作答)——多模态大模型逐页转 Markdown+LaTeX，
理论上比纯 OCR 更能跟栏、还原公式表格、跳过手写。本脚本对同一页做 VLM 转写并与
data/ocr/output.zip 里的 PP-OCR 文本并排对比。

前置：智增增(或任意 OpenAI 兼容)key 有视觉模型额度。
用法：
    python -m scripts.vlm_ocr_test --pdf data/ocr/sample_exam.pdf --pages 1-3 \
        --model qwen-vl-max --key sk-xxx --out data/ocr/vlm_out
    # 不传 --key 则读环境变量 CL_LLM_API_KEY / .env
可选视觉模型(视 key 开通)：qwen-vl-max / qwen2.5-vl-72b-instruct / gpt-4o / gemini-2.5-flash …
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os

import httpx
from openai import OpenAI

VLM_PROMPT = (
    "这是一张扫描的《计算机组成原理》考试卷图片，特点：双栏排版、含公式/表格/电路图、"
    "可能有学生手写作答。请提取【印刷体的题目内容】，要求：\n"
    "1) 按正确阅读顺序输出：先读完左栏、再读右栏，不要左右串行；\n"
    "2) 公式用 LaTeX（$...$），表格用 Markdown 表格；\n"
    "3) 忽略手写笔迹与批改痕迹，只要印刷原题；\n"
    "4) 电路图/数据通路图无法文字化的，用一句话описание占位（如 [图：运算器数据通路]）；\n"
    "5) 直接输出 Markdown，不要额外解释。"
)


def render_page(pdf: str, page_no: int, dpi: int = 150) -> bytes:
    import fitz
    d = fitz.open(pdf)
    pix = d[page_no - 1].get_pixmap(dpi=dpi)
    b = pix.tobytes("png")
    d.close()
    return b


def vlm_transcribe(cli: OpenAI, model: str, img_png: bytes) -> str:
    b64 = base64.b64encode(img_png).decode()
    r = cli.chat.completions.create(
        model=model, max_tokens=4000, temperature=0.1,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": VLM_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}])
    return r.choices[0].message.content or ""


def ppocr_text(page_no: int, probe_dir: str) -> str:
    """从 PP-OCR 的 res.json 取该页文本(按 y 粗排)。"""
    jf = os.path.join(probe_dir, f"page_{page_no:03d}_res.json")
    if not os.path.exists(jf):
        return "(无 PP-OCR 对照)"
    d = json.load(open(jf))
    texts, boxes = d.get("rec_texts", []), d.get("rec_boxes", [])
    order = sorted(range(len(texts)), key=lambda i: (boxes[i][1] // 40, boxes[i][0]) if i < len(boxes) else (0, 0))
    return "\n".join(texts[i] for i in order)


def parse_pages(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-"); return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",")]


def main():
    ap = argparse.ArgumentParser(description="VLM OCR 测试 + 与 PP-OCR 对比")
    ap.add_argument("--pdf", default="data/ocr/sample_exam.pdf")
    ap.add_argument("--pages", default="1-3")
    ap.add_argument("--model", default="qwen-vl-max")
    ap.add_argument("--key", default="")
    ap.add_argument("--base-url", default="https://api.zhizengzeng.com/v1")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--out", default="data/ocr/vlm_out")
    ap.add_argument("--ppocr-dir", default="data/ocr/_ocr_probe", help="PP-OCR res.json 目录(对照)")
    args = ap.parse_args()

    key = args.key or os.environ.get("CL_LLM_API_KEY", "")
    if not key:
        try:
            from backend.core.config import settings
            key = settings.llm_api_key
        except Exception:
            pass
    if not key:
        print("缺少 API key：--key 或 CL_LLM_API_KEY"); return

    cli = OpenAI(base_url=args.base_url, api_key=key,
                 http_client=httpx.Client(trust_env=False, timeout=120))
    os.makedirs(args.out, exist_ok=True)

    for pg in parse_pages(args.pages):
        print(f"\n{'='*70}\n第 {pg} 页  (model={args.model})")
        try:
            img = render_page(args.pdf, pg, args.dpi)
            md = vlm_transcribe(cli, args.model, img)
        except Exception as e:  # noqa: BLE001
            print(f"  VLM 调用失败：{e}"); return
        open(os.path.join(args.out, f"page_{pg:03d}_vlm.md"), "w", encoding="utf-8").write(md)
        pp = ppocr_text(pg, args.ppocr_dir)
        print(f"--- VLM 输出({len(md)}字) ---\n{md[:1200]}")
        print(f"\n--- PP-OCR 对照(前600字) ---\n{pp[:600]}")
    print(f"\n完成，VLM markdown 存于 {args.out}/")


if __name__ == "__main__":
    main()

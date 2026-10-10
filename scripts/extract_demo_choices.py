"""从带解析的扫描试卷中提取少量已给答案的选择题，补齐演示模考题型。

输出先经结构校验；只有显式 ``--load`` 才写入业务库。原 PDF 与草稿均放在 data/，不进 Git。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

import fitz
import httpx
from openai import OpenAI

from backend.core import db
from backend.core.config import settings


PROMPT = """这些图片来自一份 408 模拟卷的官方解析。请只提取其中属于“数据结构”的单项选择题，
保留完整题干和 A/B/C/D 选项，并使用解析中明确给出的官方答案。不要猜测看不清的内容。
只输出 JSON 数组，每项严格为：
{"stem":"题干及四个选项","knowledge":"408大纲知识点","difficulty":1到4,
 "canonical":{"kind":"choice","value":"A|B|C|D"},"answer":"一句话解析"}
最多输出 20 题；答案不明确、选项不完整或不属于数据结构的题直接跳过。"""


def _image_url(page, dpi: int = 120) -> str:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    image = pix.tobytes("jpeg", jpg_quality=78)
    return "data:image/jpeg;base64," + base64.b64encode(image).decode()


def _parse(raw: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("视觉 API 未返回 JSON 数组")
    data = json.loads(text[start:end + 1])
    out, seen = [], set()
    for item in data:
        stem = str(item.get("stem") or "").strip()
        canonical = item.get("canonical") or {}
        value = str(canonical.get("value") or "").strip().upper()
        if len(stem) < 30 or value not in {"A", "B", "C", "D"} or stem in seen:
            continue
        if not all(mark in stem for mark in ("A", "B", "C", "D")):
            continue
        seen.add(stem)
        out.append({
            "course": "ds-algo", "type": "选择题", "stem": stem,
            "knowledge": str(item.get("knowledge") or "数据结构基本概念").strip(),
            "difficulty": max(1, min(4, int(item.get("difficulty") or 2))),
            "verification_type": "numeric",
            "canonical": {"kind": "choice", "value": value},
            "answer": str(item.get("answer") or "").strip(),
        })
    return out


def extract(pdf: Path, pages: int, model: str) -> list[dict]:
    doc = fitz.open(pdf)
    content: list[dict] = [{"type": "text", "text": PROMPT}]
    for page in list(doc)[:max(1, min(pages, len(doc)))]:
        content.append({"type": "image_url", "image_url": {"url": _image_url(page)}})
    doc.close()
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                    http_client=httpx.Client(trust_env=False, timeout=240))
    response = client.chat.completions.create(
        model=model, temperature=0, max_tokens=10000,
        messages=[{"role": "user", "content": content}],
    )
    return _parse(response.choices[0].message.content or "")


def load(items: list[dict], source: str) -> dict:
    stat = {"inserted": 0, "duplicate": 0}
    for item in items:
        if db.problem_id_by_stem("ds-algo", item["stem"]):
            stat["duplicate"] += 1
            continue
        pid = db.insert_problem(
            "ds-algo", "选择题", item["stem"], item["difficulty"],
            json.dumps(item["canonical"], ensure_ascii=False), "numeric",
            knowledge=item["knowledge"], source=source,
            hint="先判断题目考查的定义、性质或算法不变量，再逐项排除。",
        )
        kid = db.get_or_create_knowledge_id(item["knowledge"], "ds-algo")
        db.link_problem_knowledge(pid, kid)
        stat["inserted"] += 1
    return stat


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描解析卷 → 数据结构演示选择题")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--pages", type=int, default=6)
    parser.add_argument("--model", default="")
    parser.add_argument("--min-count", type=int, default=12)
    parser.add_argument("--source", default="local-demo-408-mock")
    parser.add_argument("--load", action="store_true")
    args = parser.parse_args()
    model = args.model or settings.vision_model
    if not settings.vision_ready or not model:
        raise SystemExit("未配置可用的视觉 API")
    items = extract(args.pdf, args.pages, model)
    if len(items) < args.min_count:
        raise SystemExit(f"只得到 {len(items)} 道合格选择题，未达到 {args.min_count} 道门禁，不写入")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print({"validated": len(items), "out": str(args.out)})
    if args.load:
        print(load(items, args.source))


if __name__ == "__main__":
    main()

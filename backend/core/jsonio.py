"""从 LLM 回复中稳健提取 JSON，容忍代码块和前后缀文字。"""
from __future__ import annotations

import json
import re


def extract_json(s: str) -> dict:
    if not s:
        return {}
    s = s.strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return {}

"""CourseLoop 教师模型横评：固定 50 题，智增增四模型同题生成并自动评测。

该脚本刻意绕过 ``CL_LLM_TEXT_BASE_URL``（部署时指向本地 vLLM），只使用
``CL_LLM_BASE_URL`` / ``CL_LLM_API_KEY`` 访问远端智增增。密钥从环境读取，
不会写入 prompt、输出或报告。

用法（rag_env）：

    python -m scripts.teacher_bakeoff build
    python -m scripts.teacher_bakeoff run --preflight
    python -m scripts.teacher_bakeoff run
    python -m scripts.teacher_bakeoff report
    python -m scripts.teacher_bakeoff all

``run`` 逐条追加写盘并按 (model, case_id) 断点续跑。首轮结果永远保留；失败项的
一次修复调用写入单独的 repair 文件，不会覆盖原生成功率。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backend.agents.prompts import (  # noqa: E402
    build_diagnose_system,
    build_practice_system,
    build_solve_system,
)
from backend.core.config import settings  # noqa: E402
from backend.core.courses import get_course  # noqa: E402
from backend.learning.error_taxonomy import normalize as normalize_error  # noqa: E402
from backend.verification.citation_checker import check_citation  # noqa: E402
from backend.verification.numeric_checker import check_numeric  # noqa: E402
from backend.verification.unit_test_runner import run_python_tests  # noqa: E402
from scripts.test_lora import _extract_json  # noqa: E402


OUT_DIR = HERE / "data" / "teacher_bakeoff"
PROMPTS_FILE = OUT_DIR / "prompts.jsonl"
MANIFEST_FILE = OUT_DIR / "manifest.json"
REPORT_JSON = OUT_DIR / "report.json"
REPORT_MD = OUT_DIR / "report.md"
BLIND_REVIEW = OUT_DIR / "blind_review.jsonl"

MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "qwen3-max-2025-09-23",
    "kimi-k2.5",
)

# 智增增公开价，单位：元 / 1M tokens。DeepSeek V4 有峰谷区间。
# https://doc.zhizengzeng.com/doc-3979947 （盘点日期 2026-08-22）
PRICE_CNY_PER_M = {
    "deepseek-v4-flash": {"input": (1.5, 3.0), "output": (4.5, 9.0)},
    "deepseek-v4-pro": {"input": (4.5, 9.0), "output": (13.5, 27.0)},
    "qwen3-max-2025-09-23": {"input": (6.0, 6.0), "output": (24.0, 24.0)},
    "kimi-k2.5": {"input": (4.0, 4.0), "output": (21.0, 21.0)},
}

REQUIRED_KEYS = {
    "solve": {"problem_type", "knowledge_points", "givens", "target", "strategy",
              "steps", "final_answer", "common_errors"},
    "diagnose": {"correct", "score", "first_wrong_step", "error_tags", "diagnosis",
                 "remediation", "reexplain"},
    "practice": {"stem", "type", "difficulty", "knowledge_points", "answer", "hint"},
    "grounded_qa": {"answer", "abstain", "citations"},
    "review": {"summary", "priorities", "daily_plan"},
    "hint": {"hint", "level", "leaks_final_answer"},
}

_WRITE_LOCK = threading.Lock()


def _json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(obj: Any) -> str:
    return hashlib.sha256(_json_dump(obj).encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return re.sub(r"\W+", "", (text or "").lower(), flags=re.UNICODE)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _stable_pick(rows: list[dict], n: int) -> list[dict]:
    """按难度分散抽取，避免只拿最前面的同质题。"""
    rows = sorted(rows, key=lambda r: (int(r.get("difficulty") or 0), int(r["id"])))
    if len(rows) < n:
        raise RuntimeError(f"候选仅 {len(rows)} 条，无法抽取 {n} 条")
    if n == 1:
        return [rows[len(rows) // 2]]
    idxs = [round(i * (len(rows) - 1) / (n - 1)) for i in range(n)]
    return [rows[i] for i in idxs]


def _canonical_token(c: dict) -> str:
    kind = c.get("kind")
    if kind == "ipv4net":
        return f"{c.get('network')}/{c.get('prefix')}"
    if kind == "set":
        return ",".join(str(x) for x in c.get("values", []))
    value = c.get("value")
    if isinstance(value, list):
        return ",".join(str(x) for x in value)
    if kind == "percent" and isinstance(value, (int, float)):
        return f"{100 * value:g}%"
    return str(value)


def _wrong_token(canonical: dict) -> tuple[str, str]:
    """给诊断基准构造一个确定错误且类型清晰的答案。"""
    kind = canonical.get("kind")
    if kind == "choice":
        gold = canonical.get("value")
        gold = (gold[0] if isinstance(gold, list) and gold else gold) or "A"
        wrong = chr((ord(str(gold).upper()[0]) - 65 + 1) % 4 + 65)
        return wrong, "knowledge_gap"
    if kind == "bits":
        gold = str(canonical.get("value") or "0")
        return gold[:-1] + ("1" if gold[-1:] != "1" else "0"), "calculation"
    if kind == "ipv4net":
        prefix = int(canonical.get("prefix", 24))
        return f"{canonical.get('network')}/{max(0, prefix - 1)}", "calculation"
    if kind == "set":
        vals = list(canonical.get("values") or [])
        return ",".join(str(x) for x in (vals[:-1] or ["错误元素"])), "incomplete"
    value = canonical.get("value", 0)
    try:
        wrong = float(value) + (1 if kind != "percent" else 0.1)
        if kind == "percent":
            return f"{100 * wrong:g}%", "calculation"
        return f"{wrong:g}", "calculation"
    except (TypeError, ValueError):
        return "无法确定", "reasoning"


def _old_sft_texts() -> list[str]:
    texts: list[str] = []
    for path in (HERE / "data" / "sft").glob("*/*.jsonl"):
        if path.name.endswith(".clean.jsonl"):
            continue
        for row in _read_jsonl(path):
            user = "\n".join(m.get("content", "") for m in row.get("messages", [])
                             if m.get("role") == "user")
            if user:
                texts.append(_norm(user))
    return texts


def _load_bank() -> dict[str, list[dict]]:
    db_path = HERE / "data" / "processed" / "courseloop.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    old = _old_sft_texts()
    out: dict[str, list[dict]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM problems ORDER BY id"):
        r = dict(row)
        stem_n = _norm(r.get("stem", ""))
        if not stem_n or any(stem_n in text for text in old):
            continue
        if r.get("verification_type") == "unit_test":
            tests = []
            for t in conn.execute("SELECT input,expected_output,hidden FROM test_cases WHERE problem_id=?",
                                  (r["id"],)):
                try:
                    tests.append({"args": json.loads(t[0]), "expected": json.loads(t[1]),
                                  "hidden": bool(t[2])})
                except json.JSONDecodeError:
                    pass
            if not (r.get("ref_code") and r.get("func_name") and tests):
                continue
            r["tests"] = tests
        elif r.get("verification_type") == "numeric":
            try:
                canonical = json.loads(r.get("answer") or "")
            except json.JSONDecodeError:
                continue
            if not isinstance(canonical, dict):
                continue
            if check_numeric(_canonical_token(canonical), canonical).passed is not True:
                continue
            r["canonical"] = canonical
        else:
            continue
        out[r["course_id"]].append(r)
    conn.close()
    return out


def _case(case_id: str, task: str, course: str, messages: list[dict], expected: dict,
          source: dict, temperature: float, max_tokens: int = 2048) -> dict:
    row = {
        "case_id": case_id,
        "task": task,
        "course": course,
        "messages": messages,
        "expected": expected,
        "source": source,
        "generation": {"temperature": temperature, "max_tokens": max_tokens},
    }
    row["prompt_hash"] = _sha(messages)
    return row


def _build_bank_cases() -> tuple[list[dict], dict[str, list[dict]]]:
    bank = _load_bank()
    cases: list[dict] = []
    leftovers: dict[str, list[dict]] = {}
    for course in ("ds-algo", "co", "os", "net"):
        selected = _stable_pick(bank[course], 8)
        leftovers[course] = [r for r in bank[course] if r["id"] not in {x["id"] for x in selected}]
        cobj = get_course(course)
        for i, r in enumerate(selected[:5], 1):
            msgs = [
                {"role": "system", "content": build_solve_system(cobj)},
                {"role": "user", "content": f"题目：{r['stem']}\n\n请按要求解析："},
            ]
            expected = {"verification_type": r["verification_type"]}
            if r["verification_type"] == "unit_test":
                expected.update(func_name=r["func_name"], tests=r["tests"])
            else:
                expected["canonical"] = r["canonical"]
            cases.append(_case(
                f"solve-{course}-{i:02d}", "solve", course, msgs, expected,
                {"kind": "bank", "problem_id": r["id"], "source": r.get("source", "")}, 0.2,
            ))

        for i, r in enumerate(selected[5:], 1):
            if r["verification_type"] == "unit_test":
                wrong_code = f"def {r['func_name']}(*args):\n    return None"
                student = "第1步：我认为任意输入都可以直接返回空值。\n第2步：因此提交上述实现。"
                reference = r.get("answer") or r.get("ref_code", "")
                gold_tag = "reasoning"
                extra = {"student_code": wrong_code, "func_name": r["func_name"], "tests": r["tests"]}
            else:
                wrong, gold_tag = _wrong_token(r["canonical"])
                student = f"第1步：我直接计算或判断得到答案为 {wrong}。\n第2步：因此最终答案是 {wrong}。"
                reference = _canonical_token(r["canonical"])
                extra = {}
            user = f"题目：{r['stem']}\n\n参考答案：{reference}\n\n学生作答：{student}\n\n请定位第一个错误步骤并诊断错因："
            msgs = [
                {"role": "system", "content": build_diagnose_system(cobj)},
                {"role": "user", "content": user},
            ]
            expected = {"gold_first_wrong_step": 1, "gold_error_tag": gold_tag, **extra}
            cases.append(_case(
                f"diagnose-{course}-{i:02d}", "diagnose", course, msgs, expected,
                {"kind": "bank", "problem_id": r["id"], "source": r.get("source", "")}, 0.2,
            ))
    return cases, leftovers


def _chroma_chunks() -> dict[str, list[dict]]:
    import chromadb

    collection = chromadb.PersistentClient(
        path=str(HERE / "data" / "processed" / "chroma")
    ).get_collection(settings.chroma_collection)
    out: dict[str, list[dict]] = {}
    for course in ("ds-algo", "co", "os", "net"):
        got = collection.get(where={"course": course}, limit=100,
                             include=["documents", "metadatas"])
        rows = []
        for cid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"]):
            doc = (doc or "").strip()
            meta = meta or {}
            title = meta.get("section") or meta.get("title_path") or meta.get("chapter") or meta.get("source")
            if not (500 <= len(doc) <= 1800 and title):
                continue
            if any(x in doc[:120] for x in ("目录", "作者：", "Release")):
                continue
            rows.append({"chunk_id": cid, "text": doc, "meta": meta, "title": title})
        if len(rows) < 2:
            raise RuntimeError(f"{course} 合格 RAG chunk 不足 2 条")
        out[course] = rows[:2]
    return out


_QA_SYSTEM = (
    "你是严格依据材料回答的408课程助教。只能使用用户提供的材料；材料不能支持问题时必须拒答。"
    "只输出一个JSON对象："
    '{"answer":"简洁答案","abstain":true/false,'
    '"citations":[{"source_id":"给定ID","evidence":"逐字摘取材料中的短证据"}]}。'
    "不得使用材料之外的知识补全；evidence 必须是材料原文的连续子串。"
)


def _build_qa_cases() -> list[dict]:
    chunks = _chroma_chunks()
    cases = []
    foreign_questions = {
        "co": "材料是否说明了 TCP 三次握手每一步的报文标志位？请据此回答。",
        "net": "材料是否说明了动态分区内存分配的最佳适应算法？请据此回答。",
    }
    for course in ("ds-algo", "co", "os", "net"):
        for i, chunk in enumerate(chunks[course], 1):
            unanswerable = i == 2 and course in foreign_questions
            question = foreign_questions[course] if unanswerable else (
                f"请根据材料说明“{chunk['title']}”涉及的核心机制或结论，并归纳两个要点。"
            )
            source_id = chunk["chunk_id"]
            user = (f"source_id: {source_id}\n课程: {course}\n材料：\n{chunk['text']}\n\n"
                    f"问题：{question}")
            msgs = [{"role": "system", "content": _QA_SYSTEM},
                    {"role": "user", "content": user}]
            cases.append(_case(
                f"grounded_qa-{course}-{i:02d}", "grounded_qa", course, msgs,
                {"abstain": unanswerable, "source_id": source_id, "context": chunk["text"]},
                {"kind": "chroma", "chunk_id": source_id,
                 "source": chunk["meta"].get("source", "")}, 0.2,
            ))
    return cases


def _build_practice_cases() -> list[dict]:
    cases = []
    for course in ("ds-algo", "co", "os", "net"):
        cobj = get_course(course)
        indexes = (len(cobj.topics) // 3, (2 * len(cobj.topics)) // 3)
        for i, idx in enumerate(indexes, 1):
            topic = cobj.topics[min(idx, len(cobj.topics) - 1)]
            constraint = ("必须生成可运行的Python代码题并给出至少3个测试用例。" if cobj.code_enabled
                          else "必须生成具有明确唯一答案的计算题或选择题，并给出受支持的final_answer_canonical。")
            user = (f"目标知识点：{topic}\n目标难度：{2 + i}（1易~5难）\n{constraint}\n\n"
                    "请出一道新的变式题：")
            msgs = [{"role": "system", "content": build_practice_system(cobj)},
                    {"role": "user", "content": user}]
            cases.append(_case(
                f"practice-{course}-{i:02d}", "practice", course, msgs,
                {"code_enabled": cobj.code_enabled, "knowledge": topic},
                {"kind": "course_config", "knowledge": topic}, 0.6,
            ))
    return cases


def _build_learning_cases(leftovers: dict[str, list[dict]]) -> list[dict]:
    review_system = (
        "你是408复习规划助教。只依据给定掌握度、错因、可用时间生成计划，不得编造学习记录。"
        "只输出JSON：{\"summary\":\"\",\"priorities\":[\"\"],"
        "\"daily_plan\":[{\"day\":1,\"minutes\":60,\"task\":\"\",\"reason\":\"\"}]}。"
    )
    review_user = (
        "课程：操作系统；距离考试7天；每天60分钟。\n"
        "学情：进程同步 mastery=0.35、错误3次；虚拟内存 mastery=0.52、错误2次；"
        "文件系统 mastery=0.82、错误0次。\n请给出7天复习建议，优先安排有证据的薄弱点。"
    )
    review = _case(
        "review-os-01", "review", "os",
        [{"role": "system", "content": review_system}, {"role": "user", "content": review_user}],
        {"must_include": ["进程同步", "虚拟内存"], "days": 7, "daily_minutes": 60},
        {"kind": "synthetic_profile", "profile_id": "os-profile-01"}, 0.2,
    )

    problem = _stable_pick(leftovers["ds-algo"], 1)[0]
    hint_system = (
        "你是启发式教学助教，只给一级提示，不能泄露最终答案或完整代码。"
        "只输出JSON：{\"hint\":\"\",\"level\":1,\"leaks_final_answer\":false}。"
    )
    hint_user = f"题目：{problem['stem']}\n学生说：我不知道从哪里开始。请只给一级提示。"
    hint = _case(
        "hint-ds-algo-01", "hint", "ds-algo",
        [{"role": "system", "content": hint_system}, {"role": "user", "content": hint_user}],
        {"forbidden": [problem.get("ref_code", "")], "level": 1},
        {"kind": "bank", "problem_id": problem["id"], "source": problem.get("source", "")}, 0.2,
    )
    return [review, hint]


def build_prompts(force: bool = False) -> list[dict]:
    if PROMPTS_FILE.exists() and not force:
        rows = _read_jsonl(PROMPTS_FILE)
        validate_prompts(rows)
        if MANIFEST_FILE.exists():
            manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
            if manifest.get("models") != list(MODELS):
                manifest["models"] = list(MODELS)
                MANIFEST_FILE.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        print(f"[build] 已存在并通过校验：{PROMPTS_FILE} ({len(rows)} 条)")
        return rows
    bank_cases, leftovers = _build_bank_cases()
    rows = bank_cases + _build_practice_cases() + _build_qa_cases() + _build_learning_cases(leftovers)
    rows.sort(key=lambda r: r["case_id"])
    validate_prompts(rows)
    _write_jsonl(PROMPTS_FILE, rows)
    manifest = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "distribution": dict(sorted(Counter(r["task"] for r in rows).items())),
        "course_distribution": dict(sorted(Counter(r["course"] for r in rows).items())),
        "models": list(MODELS),
        "prompts_sha256": _sha(rows),
        "policy": "teacher selection benchmark; never include in SFT",
    }
    MANIFEST_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[build] 写出 {len(rows)} 条 → {PROMPTS_FILE}")
    print(json.dumps(manifest["distribution"], ensure_ascii=False))
    return rows


def validate_prompts(rows: list[dict]) -> None:
    expected = {"solve": 20, "diagnose": 12, "practice": 8,
                "grounded_qa": 8, "review": 1, "hint": 1}
    counts = Counter(r.get("task") for r in rows)
    if counts != Counter(expected):
        raise ValueError(f"任务分布错误：{dict(counts)} != {expected}")
    if len(rows) != 50 or len({r["case_id"] for r in rows}) != 50:
        raise ValueError("基准必须恰好 50 个唯一 case_id")
    if len({r["prompt_hash"] for r in rows}) != 50:
        raise ValueError("基准存在完全相同 prompt")
    structured = [r for r in rows if r["task"] in ("solve", "diagnose", "practice", "grounded_qa")]
    for task in ("solve", "diagnose", "practice", "grounded_qa"):
        per = Counter(r["course"] for r in structured if r["task"] == task)
        if len(per) != 4:
            raise ValueError(f"{task} 未覆盖四门：{dict(per)}")
    if sum(bool(r["expected"].get("abstain")) for r in rows if r["task"] == "grounded_qa") != 2:
        raise ValueError("Grounded QA 必须含 2 个无依据拒答")


def _remote_base_url() -> str:
    raw = settings.llm_base_url.rstrip("/")
    u = urlparse(raw)
    if not u.scheme or not u.netloc:
        raise RuntimeError("CL_LLM_BASE_URL 未配置为有效远端地址")
    path = u.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunparse((u.scheme, u.netloc, path, "", "", ""))


def _client():
    import httpx
    from openai import OpenAI

    if not settings.llm_api_key:
        raise RuntimeError("CL_LLM_API_KEY 未配置")
    return OpenAI(
        base_url=_remote_base_url(), api_key=settings.llm_api_key,
        # A single stalled generation must not freeze an otherwise resumable
        # repair batch for ten minutes.  Candidate-level persistence makes a
        # four-minute ceiling and one SDK retry the safer automation tradeoff.
        max_retries=1,
        http_client=httpx.Client(trust_env=False, timeout=httpx.Timeout(240.0, connect=15.0)),
    )


def _result_path(model: str, repair: bool = False) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", model)
    return OUT_DIR / "results" / f"{safe}{'.repair' if repair else ''}.jsonl"


def _call_one(model: str, case: dict, repair_instruction: str = "") -> dict:
    client = _client()
    messages = list(case["messages"])
    if repair_instruction:
        messages = messages + [{"role": "user", "content": repair_instruction}]
    params = case["generation"]
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": params["temperature"],
        "max_tokens": params["max_tokens"],
    }
    started = time.perf_counter()
    error = ""
    raw = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    actual_model = model
    try:
        response = client.chat.completions.create(**kwargs)
        raw = response.choices[0].message.content or ""
        actual_model = getattr(response, "model", model) or model
        u = getattr(response, "usage", None)
        if u:
            usage = {k: int(getattr(u, k, 0) or 0) for k in usage}
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    latency = round(time.perf_counter() - started, 3)
    return {
        "case_id": case["case_id"], "task": case["task"], "course": case["course"],
        "prompt_hash": case["prompt_hash"], "requested_model": model, "actual_model": actual_model,
        "raw_output": raw, "error": error, "usage": usage, "latency_seconds": latency,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "is_repair": bool(repair_instruction),
    }


def _score_solve(pred: dict, expected: dict) -> tuple[bool | None, str]:
    if expected["verification_type"] == "unit_test":
        code = str(pred.get("code") or "")
        func = str(pred.get("func_name") or expected.get("func_name") or "")
        if not code or not func:
            return False, "缺 code/func_name"
        vr = run_python_tests(code, func, expected["tests"])
        return vr.passed, vr.details
    # 优先比较模型自己给出的规范答案，避免把解题散文中的中间数字/英文字母
    # 误识别成最终答案；kind 不一致时仍回退到 final_answer。
    predicted_canonical = pred.get("final_answer_canonical")
    if (isinstance(predicted_canonical, dict) and
            predicted_canonical.get("kind") == expected["canonical"].get("kind")):
        token = _canonical_token(predicted_canonical)
        vr = check_numeric(token, expected["canonical"])
        if vr.passed is not None:
            return vr.passed, vr.details
    vr = check_numeric(str(pred.get("final_answer", "")), expected["canonical"])
    return vr.passed, vr.details


def _canonical_token(canonical: dict) -> str:
    kind = canonical.get("kind")
    if kind == "ipv4net":
        return f"{canonical.get('network', '')}/{canonical.get('prefix', '')}"
    if kind == "set":
        return ",".join(str(x) for x in canonical.get("values", []))
    value = canonical.get("value", "")
    if isinstance(value, list):
        return ",".join(str(x) for x in value)
    return str(value)


def _answer_contains_canonical(answer: str, canonical: dict) -> bool:
    """自拟非代码题只能检查答案与 canonical 的内部一致性，不冒充独立真值。"""
    text = str(answer)
    kind = canonical.get("kind")
    if kind in {"int", "scalar", "percent"}:
        # 教师偶尔把十六进制位串（如 BA3F）误标成 scalar。验证器必须安全
        # 拒绝这条候选，不能让单条坏 canonical 中断整个并发批次。
        try:
            target = float(canonical.get("value"))
            tol = float(canonical.get("tol", 0.0))
        except (TypeError, ValueError):
            return False
        values = []
        for raw, pct in re.findall(r"(-?\d[\d,]*(?:\.\d+)?)\s*(%)?", text):
            try:
                value = float(raw.replace(",", ""))
                values.append(value / 100 if pct and kind == "percent" else value)
            except ValueError:
                continue
        return any(abs(value - target) <= tol + 1e-12 for value in values)
    if kind == "choice":
        gold = canonical.get("value")
        values = gold if isinstance(gold, list) else [gold]
        found = {x.upper() for x in re.findall(r"(?<![A-Za-z])([A-Za-z])(?![A-Za-z])", text)}
        return {str(x).strip().upper() for x in values}.issubset(found)
    if kind == "bits":
        target = re.sub(r"\s+", "", str(canonical.get("value", "")))
        return bool(target) and target in re.sub(r"\s+", "", text)
    if kind == "ipv4net":
        return check_numeric(text, canonical).passed is True
    if kind == "set":
        return all(str(x) in text for x in canonical.get("values", []))
    return False


def _score_practice(pred: dict, expected: dict) -> tuple[bool | None, str]:
    if expected.get("code_enabled"):
        code, func, tests = pred.get("code"), pred.get("func_name"), pred.get("test_cases") or []
        if not (code and func and len(tests) >= 3):
            return False, "代码题缺参考实现/函数名/至少3个测试"
        vr = run_python_tests(str(code), str(func), tests)
        return vr.passed, vr.details
    canonical = pred.get("final_answer_canonical")
    if not isinstance(canonical, dict):
        return False, "非代码题缺 final_answer_canonical"
    consistent = _answer_contains_canonical(str(pred.get("answer", "")), canonical)
    return consistent, "答案与 canonical 一致" if consistent else "答案与 canonical 不一致"


def _score_qa(pred: dict, expected: dict) -> tuple[bool, str, float]:
    want_abstain = bool(expected["abstain"])
    got_abstain = pred.get("abstain") is True
    if want_abstain:
        citations = pred.get("citations") or []
        ok = got_abstain and not citations
        return ok, "正确拒答" if ok else "无依据题未干净拒答", 1.0 if ok else 0.0
    if got_abstain:
        return False, "有依据题错误拒答", 0.0
    citations = pred.get("citations") or []
    context = expected["context"]
    sid = expected["source_id"]
    # PDF/OCR 入库会改变换行和空格；忽略空白后仍须是连续原文子串。
    normalized_context = re.sub(r"\s+", "", context)
    exact = []
    for citation in citations:
        if not isinstance(citation, dict) or citation.get("source_id") != sid:
            continue
        evidence = re.sub(r"\s+", "", str(citation.get("evidence") or ""))
        if evidence and evidence in normalized_context:
            exact.append(citation)
    lexical = check_citation(str(pred.get("answer", "")), [context]).passed is True
    ok = bool(exact) and lexical
    return ok, f"原文证据={bool(exact)}，答案重叠={lexical}", (float(bool(exact)) + float(lexical)) / 2


def score_result(case: dict, result: dict) -> dict:
    parsed = _extract_json(result.get("raw_output", ""))
    parsed_ok = isinstance(parsed, dict)
    required = REQUIRED_KEYS[case["task"]]
    schema_ok = parsed_ok and required.issubset(set(parsed))
    task_pass: bool | None = False if not parsed_ok else None
    details = "无法解析JSON" if not parsed_ok else ""
    diag_step = diag_tag = None
    qa_quality = None

    if parsed_ok:
        task = case["task"]
        expected = case["expected"]
        if task == "solve":
            task_pass, details = _score_solve(parsed, expected)
        elif task == "diagnose":
            try:
                diag_step = int(parsed.get("first_wrong_step")) == int(expected["gold_first_wrong_step"])
            except (TypeError, ValueError):
                diag_step = False
            tags = {normalize_error(str(x)) for x in (parsed.get("error_tags") or [])}
            diag_tag = normalize_error(expected["gold_error_tag"]) in tags
            task_pass = bool(diag_step and diag_tag)
            details = f"首错={diag_step}，标签={diag_tag}"
        elif task == "practice":
            task_pass, details = _score_practice(parsed, expected)
        elif task == "grounded_qa":
            task_pass, details, qa_quality = _score_qa(parsed, expected)
        elif task == "review":
            text = _json_dump(parsed)
            incl = all(k in text for k in expected["must_include"])
            days = parsed.get("daily_plan") or []
            task_pass = incl and isinstance(days, list) and len(days) == expected["days"]
            details = f"薄弱点覆盖={incl}，计划天数={len(days) if isinstance(days, list) else 0}"
        elif task == "hint":
            hint = str(parsed.get("hint") or "")
            forbidden = [x for x in expected.get("forbidden", []) if x]
            no_leak = parsed.get("leaks_final_answer") is False and not any(x in hint for x in forbidden)
            task_pass = no_leak and parsed.get("level") == expected["level"]
            details = f"不泄露={no_leak}，level={parsed.get('level')}"

    usage = result.get("usage") or {}
    prices = PRICE_CNY_PER_M[result["requested_model"]]
    pt, ct = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    cost_low = (pt * prices["input"][0] + ct * prices["output"][0]) / 1_000_000
    cost_high = (pt * prices["input"][1] + ct * prices["output"][1]) / 1_000_000
    return {
        "case_id": case["case_id"], "task": case["task"], "course": case["course"],
        "requested_model": result["requested_model"], "call_ok": not bool(result.get("error")),
        "parsed_ok": parsed_ok, "schema_ok": bool(schema_ok), "task_pass": task_pass,
        "details": details, "diag_step_ok": diag_step, "diag_tag_ok": diag_tag,
        "qa_quality": qa_quality, "latency_seconds": result.get("latency_seconds", 0),
        "prompt_tokens": pt, "completion_tokens": ct,
        "cost_cny_low": round(cost_low, 6), "cost_cny_high": round(cost_high, 6),
        "parsed": parsed,
    }


def _repair_instruction(score: dict) -> str:
    return (
        "你上一条输出未通过自动验证。请重新独立完成原任务，只输出符合系统schema的JSON，"
        f"修正以下问题：{score.get('details') or '格式或答案不合格'}。"
    )


def _run_model(model: str, cases: list[dict], repair: bool = False) -> dict:
    path = _result_path(model, repair=repair)
    done = {r["case_id"] for r in _read_jsonl(path)}
    case_map = {c["case_id"]: c for c in cases}
    base_results = {r["case_id"]: r for r in _read_jsonl(_result_path(model))}
    todo = []
    for case in cases:
        if case["case_id"] in done:
            continue
        if repair:
            base = base_results.get(case["case_id"])
            if not base:
                continue
            score = score_result(case, base)
            if score["call_ok"] and score["schema_ok"] and score["task_pass"] is True:
                continue
            todo.append((case, _repair_instruction(score)))
        else:
            todo.append((case, ""))
    print(f"[{model}] {'repair' if repair else 'native'} 待跑 {len(todo)} / {len(cases)}", flush=True)
    ok = fail = 0
    for idx, (case, instruction) in enumerate(todo, 1):
        row = _call_one(model, case, instruction)
        _append_jsonl(path, row)
        if row["error"]:
            fail += 1
        else:
            ok += 1
        status = "ERR " + row["error"][:70] if row["error"] else f"OK {row['latency_seconds']}s"
        print(f"[{model}] {idx}/{len(todo)} {case['case_id']} {status}", flush=True)
    return {"model": model, "ok": ok, "failed": fail, "remaining": len(todo)}


def run_bakeoff(cases: list[dict], models: tuple[str, ...], workers: int,
                preflight: bool = False, repair: bool = False) -> None:
    if len(cases) == 50:
        validate_prompts(cases)
    elif len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("定向 case 列表存在重复")
    if preflight:
        picked = []
        for task in ("solve", "grounded_qa"):
            picked.append(next(c for c in cases if c["task"] == task))
        cases = picked
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(models))) as pool:
        futures = [pool.submit(_run_model, m, cases, repair) for m in models]
        for fut in concurrent.futures.as_completed(futures):
            print("[done]", fut.result(), flush=True)


def _pct(n: float, d: int) -> float:
    return round(100 * n / d, 2) if d else 0.0


def _p95(vals: list[float]) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    return vals[max(0, math.ceil(0.95 * len(vals)) - 1)]


def _aggregate_model(model: str, cases: list[dict], repair: bool = False) -> dict:
    cmap = {c["case_id"]: c for c in cases}
    raw_rows = [r for r in _read_jsonl(_result_path(model, repair=repair)) if r["case_id"] in cmap]
    # Ctrl-C 时已经在途的请求可能写回，而断点续跑也可能同时补同一 case。
    # 质量横评按首次结果去重；实际发生的重复调用成本另行保留，避免掩盖消耗。
    unique_rows = {}
    for row in raw_rows:
        unique_rows.setdefault(row["case_id"], row)
    rows = list(unique_rows.values())
    scores = [score_result(cmap[r["case_id"]], r) for r in rows]
    raw_scores = [score_result(cmap[r["case_id"]], r) for r in raw_rows]
    n = len(cases)
    by_task = {}
    for task in ("solve", "diagnose", "practice", "grounded_qa", "review", "hint"):
        ss = [s for s in scores if s["task"] == task]
        if not ss:
            continue
        by_task[task] = {
            "n": len(ss),
            "schema_rate": _pct(sum(s["schema_ok"] for s in ss), len(ss)),
            "pass_rate": _pct(sum(s["task_pass"] is True for s in ss), len(ss)),
        }
        if task == "diagnose":
            by_task[task]["first_wrong_step_acc"] = _pct(sum(s["diag_step_ok"] is True for s in ss), len(ss))
            by_task[task]["error_tag_acc"] = _pct(sum(s["diag_tag_ok"] is True for s in ss), len(ss))
        if task == "grounded_qa":
            by_task[task]["grounding_score"] = _pct(sum(s["qa_quality"] or 0 for s in ss), len(ss))
    latencies = [float(s["latency_seconds"] or 0) for s in scores if s["call_ok"]]
    return {
        "model": model, "is_repair": repair, "expected_n": n, "completed_n": len(rows),
        "raw_request_n": len(raw_rows), "duplicate_n": len(raw_rows) - len(rows),
        "call_success_rate": _pct(sum(s["call_ok"] for s in scores), n),
        "json_parse_rate": _pct(sum(s["parsed_ok"] for s in scores), n),
        "schema_rate": _pct(sum(s["schema_ok"] for s in scores), n),
        "task_pass_rate": _pct(sum(s["task_pass"] is True for s in scores), n),
        "cost_cny_low": round(sum(s["cost_cny_low"] for s in scores), 4),
        "cost_cny_high": round(sum(s["cost_cny_high"] for s in scores), 4),
        "billed_cost_cny_low": round(sum(s["cost_cny_low"] for s in raw_scores), 4),
        "billed_cost_cny_high": round(sum(s["cost_cny_high"] for s in raw_scores), 4),
        "prompt_tokens": sum(s["prompt_tokens"] for s in scores),
        "completion_tokens": sum(s["completion_tokens"] for s in scores),
        "latency_p50": round(statistics.median(latencies), 3) if latencies else 0,
        "latency_p95": round(_p95(latencies), 3),
        "by_task": by_task,
        "failures": [{k: s[k] for k in ("case_id", "task", "course", "details")}
                     for s in scores if not (s["schema_ok"] and s["task_pass"] is True)],
    }


def _composite(rows: list[dict]) -> None:
    complete = [r for r in rows if r["completed_n"] == r["expected_n"]]
    if not complete:
        return
    min_cost = min(max(r["cost_cny_high"], 1e-9) for r in complete)
    min_latency = min(max(r["latency_p50"], 1e-9) for r in complete)
    for r in rows:
        diag = r["by_task"].get("diagnose", {})
        qa = r["by_task"].get("grounded_qa", {})
        diag_score = (diag.get("first_wrong_step_acc", 0) + diag.get("error_tag_acc", 0)) / 2
        qa_score = qa.get("grounding_score", 0)
        cost_eff = 100 * min_cost / max(r["cost_cny_high"], 1e-9)
        latency_eff = 100 * min_latency / max(r["latency_p50"], 1e-9)
        r["composite_auto"] = round(
            0.45 * r["task_pass_rate"] + 0.15 * r["schema_rate"] +
            0.15 * diag_score + 0.10 * qa_score + 0.08 * cost_eff + 0.07 * latency_eff, 2)
        sp = r["by_task"].get("solve", {}).get("pass_rate", 0)
        pp = r["by_task"].get("practice", {}).get("pass_rate", 0)
        r["automatic_gate"] = {
            "passed": bool(r["call_success_rate"] >= 98 and r["schema_rate"] >= 96 and
                           min(sp, pp) >= 90 and diag.get("first_wrong_step_acc", 0) >= 75 and
                           qa.get("grounding_score", 0) >= 90),
            "human_review": "pending",
        }


def _blind_review(cases: list[dict], sample_per_model: int = 20) -> None:
    rows = []
    rng = random.Random(20260822)
    aliases = {m: f"teacher-{chr(65 + i)}" for i, m in enumerate(rng.sample(list(MODELS), len(MODELS)))}
    for model in MODELS:
        result = {r["case_id"]: r for r in _read_jsonl(_result_path(model))}
        available = [c for c in cases if c["case_id"] in result]
        # 每任务至少覆盖，再补齐到 20。
        selected = []
        for task in ("solve", "diagnose", "practice", "grounded_qa", "review", "hint"):
            task_rows = [c for c in available if c["task"] == task]
            selected.extend(task_rows[:min(2, len(task_rows))])
        remaining = [c for c in available if c not in selected]
        rng.shuffle(remaining)
        selected = (selected + remaining)[:sample_per_model]
        for c in selected:
            r = result[c["case_id"]]
            rows.append({
                "review_id": _sha([model, c["case_id"]])[:12], "teacher_alias": aliases[model],
                "case_id": c["case_id"], "task": c["task"], "course": c["course"],
                "prompt": c["messages"], "output": r["raw_output"],
                "scores": {"correctness_1_5": None, "pedagogy_1_5": None,
                           "grounding_1_5": None, "accept_for_sft": None, "notes": ""},
            })
    rng.shuffle(rows)
    _write_jsonl(BLIND_REVIEW, rows)


def _markdown(report: dict) -> str:
    lines = ["# CourseLoop 教师模型横评", "", f"生成时间：{report['created_at']}", "",
             "## 自动评分", "",
             "| 模型 | 完成 | 重复请求 | API成功 | Schema | 任务通过 | 自动综合分 | 50题成本(元) | P50/P95(s) | 自动门禁 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in sorted(report["models"], key=lambda x: x.get("composite_auto", -1), reverse=True):
        gate = "✓" if r.get("automatic_gate", {}).get("passed") else "✗"
        lines.append(
            f"| {r['model']} | {r['completed_n']}/{r['expected_n']} | {r['duplicate_n']} | {r['call_success_rate']}% | "
            f"{r['schema_rate']}% | {r['task_pass_rate']}% | {r.get('composite_auto', '—')} | "
            f"{r['cost_cny_low']}–{r['cost_cny_high']} | {r['latency_p50']}/{r['latency_p95']} | {gate} |"
        )
    duplicates = [r for r in report["models"] if r["duplicate_n"]]
    if duplicates:
        lines += ["", "注：中断时在途请求造成重复；质量按 case_id 去重。实际含重复消耗：" +
                  "；".join(f"{r['model']} {r['billed_cost_cny_low']}–{r['billed_cost_cny_high']} 元"
                            for r in duplicates) + "。", ""]
    lines += ["", "## 分任务", ""]
    for r in sorted(report["models"], key=lambda x: x.get("composite_auto", -1), reverse=True):
        lines += [f"### {r['model']}", "",
                  "| 任务 | n | Schema | 通过率 | 补充指标 |", "|---|---:|---:|---:|---|"]
        for task, m in r["by_task"].items():
            extra = ""
            if task == "diagnose":
                extra = f"首错 {m['first_wrong_step_acc']}%，标签 {m['error_tag_acc']}%"
            elif task == "grounded_qa":
                extra = f"grounding {m['grounding_score']}%"
            lines.append(f"| {task} | {m['n']} | {m['schema_rate']}% | {m['pass_rate']}% | {extra} |")
        lines.append("")
    lines += ["## 选择规则", "",
              "- 自动门禁通过且人工盲审正确率 ≥90% 才可用于批量合成。",
              "- 前两名综合分相差 ≤3 分时，各追加 100 条复赛。",
              "- Flash 与 Pro 的质量差距 ≤2 个百分点时，采用 Flash 批量生成、Pro 复核失败项。",
              "- 盲审表见 `blind_review.jsonl`；填写前不揭示真实模型。", ""]
    return "\n".join(lines)


def make_report(cases: list[dict]) -> dict:
    rows = [_aggregate_model(model, cases) for model in MODELS]
    _composite(rows)
    repair_rows = [_aggregate_model(model, cases, repair=True) for model in MODELS
                   if _result_path(model, repair=True).exists()]
    report = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark": str(PROMPTS_FILE.relative_to(HERE)),
        "prompt_count": len(cases), "models": rows, "repair": repair_rows,
        "price_source": "https://doc.zhizengzeng.com/doc-3979947",
        "human_review": {"status": "pending", "file": str(BLIND_REVIEW.relative_to(HERE))},
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    _blind_review(cases)
    print(_markdown(report))
    print(f"报告：{REPORT_MD}\n盲审：{BLIND_REVIEW}")
    return report


def _parse_models(raw: str) -> tuple[str, ...]:
    models = tuple(x.strip() for x in raw.split(",") if x.strip())
    unknown = [x for x in models if x not in MODELS]
    if unknown:
        raise SystemExit(f"未登记价格的模型：{unknown}")
    return models


def main() -> None:
    ap = argparse.ArgumentParser(description="CourseLoop 教师 API 四模型横评")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--force", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--models", default=",".join(MODELS))
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--preflight", action="store_true")
    r.add_argument("--repair", action="store_true")
    r.add_argument("--case-ids", default="", help="逗号分隔；用于审计后的定向修复")
    sub.add_parser("report")
    a = sub.add_parser("all")
    a.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if args.cmd == "build":
        build_prompts(force=args.force)
        return
    cases = build_prompts()
    if args.cmd == "run":
        if args.case_ids:
            wanted = {x.strip() for x in args.case_ids.split(",") if x.strip()}
            unknown = wanted - {c["case_id"] for c in cases}
            if unknown:
                raise SystemExit(f"未知 case_id：{sorted(unknown)}")
            cases = [c for c in cases if c["case_id"] in wanted]
        run_bakeoff(cases, _parse_models(args.models), max(1, args.workers),
                    preflight=args.preflight, repair=args.repair)
    elif args.cmd == "report":
        make_report(cases)
    elif args.cmd == "all":
        run_bakeoff(cases, MODELS, max(1, args.workers), preflight=True)
        run_bakeoff(cases, MODELS, max(1, args.workers))
        make_report(cases)


if __name__ == "__main__":
    main()

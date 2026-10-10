"""CourseLoop LoRA 离线测试（不部署、不起服务）——出一份测试报告。

在 held-out 验证集 data/sft/val.jsonl（训练从未见过）上，用微调后的 adapter
逐条生成结构化 JSON，与每条自带的 teacher-gold 比对，得到量化指标：
  - JSON 合法率            生成是否为可解析 JSON 对象
  - 字段完整率            预测 keys 是否覆盖 gold 的全部 keys
  - 首错定位准确率(diag)   first_wrong_step 与 gold 精确一致
  - 错因标签一致率(diag)   error_tags 与 gold 标签有交集
说明：这是“蒸馏一致性”指标（相对 teacher 的 gold），衡量微调是否学到目标行为。

两种用法：
  1) 训练脚本训完自动调用 run_test(model, tokenizer, tag="best")（测内存中的最优模型）。
  2) 单独跑（sft_env）：
     /opt/sft_env/bin/python scripts/test_lora.py --adapter model/loras/courseloop-dsalgo
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

try:
    if int(os.environ.get("OMP_NUM_THREADS", "1")) < 1:
        os.environ["OMP_NUM_THREADS"] = "1"
except ValueError:
    os.environ["OMP_NUM_THREADS"] = "1"

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
VAL = os.path.join(HERE, "data", "sft", "val.jsonl")
EVAL_DIR = os.path.join(HERE, "data", "eval")
ADAPTER = os.path.join(HERE, "model", "loras", "courseloop-dsalgo")
CACHE_VERSION = 2

# 意图签名（与 build_sft_data 的 *_KEYS 对应，用子集判定）
DIAG_SIG = {"correct", "score", "first_wrong_step", "error_tags", "diagnosis"}
SOLVE_SIG = {"problem_type", "knowledge_points", "givens", "target", "strategy"}
PRAC_SIG = {"stem", "type", "difficulty", "answer"}
QA_SIG = {"answer", "abstain", "citations"}


def _production_prompt_contract_fingerprint() -> str:
    """Fingerprint every diagnosis system prompt used by standalone eval."""
    from backend.agents.prompts import build_diagnose_system
    from backend.core.courses import course_ids, get_course
    payload = {
        course_id: build_diagnose_system(get_course(course_id))
        for course_id in course_ids()
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _production_task_limits() -> dict[str, int]:
    from backend.core.generation_contract import production_task_limits
    return production_task_limits()


def _effective_max_new_tokens(intent: str, fallback: int,
                              refresh_production_prompts: bool) -> int:
    if not refresh_production_prompts:
        return fallback
    return _production_task_limits().get(intent, fallback)


def _intent(gold: dict) -> str:
    ks = set(gold)
    if DIAG_SIG <= ks:
        return "diagnose"
    if SOLVE_SIG <= ks:
        return "solve"
    if PRAC_SIG <= ks:
        return "practice"
    if QA_SIG <= ks:
        return "grounded_qa"
    return "other"


def _filter_rows_by_intent(rows: list[dict], intents: set[str]) -> list[dict]:
    if not intents:
        return rows
    selected = []
    for row in rows:
        try:
            intent = _intent(json.loads(row["messages"][-1]["content"]))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            intent = "other"
        if intent in intents:
            selected.append(row)
    return selected


def _row_cache_key(row: dict) -> str:
    payload = json.dumps(
        {"course": row.get("course"), "messages": row.get("messages")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _standalone_signature(adapter: str, val_path: str, *, max_seq: int,
                          max_new_tokens: int, intents: set[str],
                          refresh_production_prompts: bool) -> str:
    adapter_path = adapter if os.path.isabs(adapter) else os.path.join(HERE, adapter)
    fingerprints = []
    for name in ("adapter_config.json", "adapter_model.safetensors"):
        path = os.path.join(adapter_path, name)
        stat = os.stat(path)
        fingerprints.append((name, stat.st_size, stat.st_mtime_ns))
    val_stat = os.stat(val_path)
    payload = {
        "version": CACHE_VERSION,
        "adapter": os.path.realpath(adapter_path),
        "adapter_files": fingerprints,
        "val": (os.path.realpath(val_path), val_stat.st_size, val_stat.st_mtime_ns),
        "max_seq": max_seq,
        "max_new_tokens": max_new_tokens,
        "intents": sorted(intents),
        "refresh_production_prompts": refresh_production_prompts,
        "production_prompt_contract": (
            _production_prompt_contract_fingerprint()
            if refresh_production_prompts else None
        ),
        "production_task_max_tokens": (
            _production_task_limits() if refresh_production_prompts else None
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _extract_json(text: str):
    """从生成文本里抠出第一个完整 JSON 对象；失败返回 None。"""
    s = text.strip()
    # 去掉可能的 ```json 围栏
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = s[start:i + 1]
                try:
                    return json.loads(blob)
                except Exception:
                    return None
    return None


def _norm_step(v) -> str:
    """first_wrong_step 归一化比较：数字/字符串统一成去空白小写串。"""
    if v is None:
        return ""
    return str(v).strip().lower().replace(" ", "")


def _generate(model, tokenizer, messages, max_new_tokens: int) -> str:
    import torch
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs, max_new_tokens=max_new_tokens, do_sample=False,
            pad_token_id=(tokenizer.pad_token_id or tokenizer.eos_token_id),
        )
    gen = out[0][inputs["input_ids"].shape[1]:]
    return tokenizer.decode(gen, skip_special_tokens=True)


def _generation_messages(row: dict, intent: str,
                         refresh_production_prompts: bool) -> list[dict]:
    messages = [dict(message) for message in row["messages"][:-1]]
    if not refresh_production_prompts or intent != "diagnose":
        return messages
    from backend.agents.prompts import build_diagnose_system
    from backend.core.courses import get_course
    course = str(row.get("course") or "ds-algo")
    if course not in {"co", "ds-algo", "net", "os"}:
        course = "ds-algo"
    system = {"role": "system", "content": build_diagnose_system(get_course(course))}
    if messages and messages[0].get("role") == "system":
        messages[0] = system
    else:
        messages.insert(0, system)
    return messages


def run_test(model, tokenizer, tag: str = "best", val_path: str = VAL,
             limit: int | None = None, max_new_tokens: int = 768,
             intents: set[str] | None = None,
             refresh_production_prompts: bool = False,
             prediction_cache: str | None = None,
             run_signature: str | None = None,
             adapter: str | None = None) -> dict:
    """对内存中的模型在 val 上评测，打印指标表并写报告。返回 metrics dict。"""
    rows = [json.loads(l) for l in open(val_path, encoding="utf-8")]
    intents = set(intents or ())
    rows = _filter_rows_by_intent(rows, intents)
    if limit:
        rows = rows[:limit]

    cached = {}
    if prediction_cache and run_signature and os.path.exists(prediction_cache):
        for line in open(prediction_cache, encoding="utf-8"):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("run_signature") == run_signature and item.get("row_key"):
                cached[item["row_key"]] = str(item.get("raw_output") or "")
    cache_hits = 0
    cache_handle = None
    if prediction_cache and run_signature:
        os.makedirs(os.path.dirname(prediction_cache), exist_ok=True)
        cache_handle = open(prediction_cache, "a", encoding="utf-8")

    # 逐意图累计
    agg = {}
    def bucket(it):
        return agg.setdefault(it, {"n": 0, "json_ok": 0, "keys_ok": 0,
                                   "step_ok": 0, "tag_ok": 0, "diag_n": 0})

    t0 = time.time()
    samples = []  # 保存少量样例进报告
    try:
        for idx, ex in enumerate(rows):
            msgs = ex["messages"]
            gold = json.loads(msgs[-1]["content"])
            it = _intent(gold)
            course = ex.get("course", "?")
            grp = f"{course}/{it}"
            b = bucket(grp)
            b["n"] += 1
            row_key = _row_cache_key(ex)
            if row_key in cached:
                text = cached[row_key]
                cache_hits += 1
            else:
                text = _generate(
                    model, tokenizer,
                    _generation_messages(ex, it, refresh_production_prompts),
                    _effective_max_new_tokens(
                        it, max_new_tokens, refresh_production_prompts
                    ),
                )
                if cache_handle:
                    cache_handle.write(json.dumps({
                        "run_signature": run_signature,
                        "row_key": row_key,
                        "raw_output": text,
                    }, ensure_ascii=False) + "\n")
                    cache_handle.flush()
            pred = _extract_json(text)
            json_ok = isinstance(pred, dict)
            b["json_ok"] += 1 if json_ok else 0
            keys_ok = json_ok and set(gold).issubset(set(pred))
            b["keys_ok"] += 1 if keys_ok else 0
            if it == "diagnose":
                b["diag_n"] += 1
                if json_ok:
                    if _norm_step(pred.get("first_wrong_step")) == _norm_step(gold.get("first_wrong_step")):
                        b["step_ok"] += 1
                    gt = {str(x).strip().lower() for x in (gold.get("error_tags") or [])}
                    pt = {str(x).strip().lower() for x in (pred.get("error_tags") or [])}
                    if gt and (gt & pt):
                        b["tag_ok"] += 1
            if len(samples) < 6:
                samples.append({"intent": grp, "json_ok": json_ok, "keys_ok": keys_ok,
                                "gen_head": text.strip()[:200]})
            if (idx + 1) % 20 == 0:
                print(f"  ...已测 {idx + 1}/{len(rows)}  用时 {int(time.time() - t0)}s", flush=True)
    finally:
        if cache_handle:
            cache_handle.close()

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else 0.0

    metrics = {"tag": tag, "val_path": val_path, "n_total": len(rows),
               "adapter": adapter, "run_signature": run_signature,
               "cache_version": CACHE_VERSION,
               "production_prompt_contract_fingerprint": (
                   _production_prompt_contract_fingerprint()
                   if refresh_production_prompts else None
               ),
               "production_task_max_tokens": (
                   _production_task_limits() if refresh_production_prompts else None
               ),
               "intent_filter": sorted(intents),
               "production_prompts_refreshed": refresh_production_prompts,
               "prediction_cache": prediction_cache,
               "prediction_cache_hits": cache_hits,
               "seconds": int(time.time() - t0), "by_intent": {}, "by_course": {}}
    tot = {"n": 0, "json_ok": 0, "keys_ok": 0}
    cagg = {}  # 按课程汇总（跨 intent）
    for grp, b in sorted(agg.items()):
        m = {"n": b["n"],
             "json_valid_rate": pct(b["json_ok"], b["n"]),
             "keys_complete_rate": pct(b["keys_ok"], b["n"])}
        if grp.split("/")[-1] == "diagnose":
            m["first_wrong_step_acc"] = pct(b["step_ok"], b["diag_n"])
            m["error_tag_consistency"] = pct(b["tag_ok"], b["diag_n"])
        metrics["by_intent"][grp] = m
        cb = cagg.setdefault(grp.split("/")[0], {"n": 0, "json_ok": 0, "keys_ok": 0})
        for k in ("n", "json_ok", "keys_ok"):
            tot[k] += b[k]
            cb[k] += b[k]
    for course, cb in sorted(cagg.items()):
        metrics["by_course"][course] = {"n": cb["n"],
                                        "json_valid_rate": pct(cb["json_ok"], cb["n"]),
                                        "keys_complete_rate": pct(cb["keys_ok"], cb["n"])}
    metrics["overall"] = {"n": tot["n"],
                          "json_valid_rate": pct(tot["json_ok"], tot["n"]),
                          "keys_complete_rate": pct(tot["keys_ok"], tot["n"])}
    metrics["samples"] = samples

    _print_report(metrics)
    os.makedirs(EVAL_DIR, exist_ok=True)
    js = os.path.join(EVAL_DIR, f"report_lora_{tag}.json")
    with open(js, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    md = os.path.join(EVAL_DIR, f"report_lora_{tag}.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(_markdown(metrics))
    print(f"\n📄 报告已写入：\n  {js}\n  {md}")
    return metrics


def _print_report(m: dict):
    print("\n" + "=" * 64)
    print(f"CourseLoop LoRA 离线测试报告  tag={m['tag']}")
    print(f"  held-out val：{m['n_total']} 条 | 用时 {m['seconds']}s")
    print("=" * 64)
    o = m["overall"]
    print(f"[总体] JSON 合法率 {o['json_valid_rate']}% | 字段完整率 {o['keys_complete_rate']}%")
    for course, mm in m.get("by_course", {}).items():
        print(f"[课程·{course:<8}] n={mm['n']:>3}  JSON {mm['json_valid_rate']:>5}%  字段完整 {mm['keys_complete_rate']:>5}%")
    for grp, mm in m["by_intent"].items():
        line = (f"[{grp:<18}] n={mm['n']:>3}  JSON {mm['json_valid_rate']:>5}%  "
                f"字段完整 {mm['keys_complete_rate']:>5}%")
        if grp.split("/")[-1] == "diagnose":
            line += (f"  首错定位 {mm['first_wrong_step_acc']:>5}%  "
                     f"错因一致 {mm['error_tag_consistency']:>5}%")
        print(line)


def _markdown(m: dict) -> str:
    o = m["overall"]
    lines = [f"# CourseLoop LoRA 离线测试报告（tag={m['tag']}）", "",
             f"- 测试集：held-out `{os.path.relpath(m['val_path'], HERE)}`，共 **{m['n_total']}** 条（训练从未见过）",
             f"- 生成：贪心解码、非思考模式；用时 {m['seconds']}s",
             "- 指标含义：与每条自带 teacher-gold 比对的**蒸馏一致性**（非人工真值）",
             "", "## 分课程汇总", "",
             "| 课程 | n | JSON 合法率 | 字段完整率 |", "|---|---|---|---|"]
    for course, mm in m.get("by_course", {}).items():
        lines.append(f"| {course} | {mm['n']} | {mm['json_valid_rate']}% | {mm['keys_complete_rate']}% |")
    lines += ["", "## 分课程×意图指标", "",
              "| 课程/意图 | n | JSON 合法率 | 字段完整率 | 首错定位准确率 | 错因标签一致率 |",
              "|---|---|---|---|---|---|"]
    for grp, mm in m["by_intent"].items():
        s = mm.get("first_wrong_step_acc", "-")
        t = mm.get("error_tag_consistency", "-")
        s = f"{s}%" if s != "-" else "—"
        t = f"{t}%" if t != "-" else "—"
        lines.append(f"| {grp} | {mm['n']} | {mm['json_valid_rate']}% | {mm['keys_complete_rate']}% | {s} | {t} |")
    lines.append(f"| **总体** | {o['n']} | {o['json_valid_rate']}% | {o['keys_complete_rate']}% | — | — |")
    lines += ["", "## 抽样", ""]
    for i, s in enumerate(m["samples"], 1):
        lines.append(f"**#{i} [{s['intent']}] json_ok={s['json_ok']} keys_ok={s['keys_ok']}**")
        lines.append("```")
        lines.append(s["gen_head"])
        lines.append("```")
    return "\n".join(lines) + "\n"


def _load_standalone(adapter: str, max_seq: int):
    from unsloth import FastLanguageModel
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=adapter, max_seq_length=max_seq, load_in_4bit=True, dtype=None)
    FastLanguageModel.for_inference(model)
    return model, tokenizer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default=ADAPTER)
    ap.add_argument("--val", default=VAL)
    ap.add_argument("--tag", default="best")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-seq", type=int, default=4096)
    ap.add_argument("--max-new-tokens", type=int, default=768)
    ap.add_argument("--intent", action="append", default=[],
                    help="只评测指定 intent；可重复指定")
    ap.add_argument("--refresh-production-prompts", action="store_true",
                    help="只替换诊断 system prompt，题目与 gold 保持冻结")
    args = ap.parse_args()
    intents = set(args.intent)
    signature = _standalone_signature(
        args.adapter, args.val, max_seq=args.max_seq,
        max_new_tokens=args.max_new_tokens, intents=intents,
        refresh_production_prompts=args.refresh_production_prompts,
    )
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.tag)
    prediction_cache = os.path.join(EVAL_DIR, f"lora_predictions_{safe_tag}.jsonl")
    model, tokenizer = _load_standalone(args.adapter, args.max_seq)
    run_test(model, tokenizer, tag=args.tag, val_path=args.val,
             limit=args.limit, max_new_tokens=args.max_new_tokens,
             intents=intents,
             refresh_production_prompts=args.refresh_production_prompts,
             prediction_cache=prediction_cache, run_signature=signature,
             adapter=args.adapter)


if __name__ == "__main__":
    main()

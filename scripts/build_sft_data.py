"""SFT 数据构造：蒸馏 teacher + 确定性验证器过滤 → unsloth 就绪 JSONL（408 四门）。

三个 JSON 任务（solve / diagnose / practice），每条样本形如：
    {"messages": [{"role":"system",...},{"role":"user",...},{"role":"assistant", <验证过的干净JSON>}], "course": "co"}

核心思想：teacher 生成 → 用确定性验证器**自动筛只留通过的**，得到天然干净语料。按科目切验证 gate：
- 数据结构(code_enabled)：沙箱运行参考代码/错误代码（原逻辑不变）。
- 计组/OS/网络(非代码)：final_answer_canonical 数值自洽 / 错误答案数值反证 / rubric 覆盖（见 numeric_checker）。

用法（rag_env，只调 API + 本地校验，不占显存）：
    rag_env python -m scripts.build_sft_data --course co --intent all --target 1500 --workers 32
产物：data/sft/<course>/{solve,diagnose,practice}.jsonl；合并 + 分层切分 → data/sft/train.jsonl / val.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from backend.core import llm as llm_mod
from backend.core.jsonio import extract_json
from backend.core.courses import get_course, is_valid_course, course_ids
from backend.agents import solver as solver_mod
from backend.agents import error_diagnoser as diag_mod
from backend.agents.prompts import build_practice_system
from backend.verification.unit_test_runner import run_python_tests
from backend.verification.numeric_checker import check_numeric
from backend.verification.rule_checker import check_concept
from backend.learning.error_taxonomy import ERROR_CODES, normalize, name_of

SFT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "sft"))
TEACHER_DEFAULT = "deepseek-v4-pro"

DIFFICULTIES = [1, 2, 3, 4]
WRONG_TYPES = ["boundary", "reasoning", "strategy", "concept_confuse", "complexity", "calculation"]

# ---------- grounded 蒸馏（E9）：按 topic 缓存教材/课件检索片段，注入 teacher 出题/解题 ----------
# 默认关闭；--grounded 开启。预热时串行检索（避免 32 线程并发调 bge-m3），worker 只读缓存。
_GROUNDED = False
_TOPIC_CTX: dict[tuple, str] = {}
_CTX_MAX_CHARS = 1600


def prewarm_contexts(course: str, topics: list[str]) -> int:
    """串行为每个 topic 检索 top-k 教材/课件片段并缓存（供 grounded 注入）。返回命中 topic 数。"""
    if not _GROUNDED:
        return 0
    try:
        from backend.rag.retriever import get_engine
        engine = get_engine()
        if not engine.has_corpus():
            print("  [grounded] 向量库为空，退化为非 grounded（请先 ingest 课件）")
            return 0
    except Exception as e:  # noqa: BLE001
        print(f"  [grounded] 检索器不可用，退化为非 grounded：{e}")
        return 0
    hit = 0
    for t in topics:
        try:
            hits = engine.retrieve(t, doc_types=["textbook", "courseware"], top_n=3, course=course)
        except Exception:  # noqa: BLE001
            hits = []
        ctx = engine.build_context(hits)[:_CTX_MAX_CHARS] if hits else ""
        _TOPIC_CTX[(course, t)] = ctx
        if ctx:
            hit += 1
    print(f"  [grounded] 预热 {len(topics)} 个 topic，{hit} 个命中教材/课件片段")
    return hit


def topic_ctx(course: str, topic: str) -> str:
    return _TOPIC_CTX.get((course, topic), "") if _GROUNDED else ""

# schema 白名单：assistant 目标只保留提示词要求的字段（去后处理注入键）。按 code_enabled 分两套。
SOLVE_KEYS_CODE = ["problem_type", "knowledge_points", "givens", "target", "strategy",
                   "steps", "final_answer", "common_errors", "code", "func_name", "complexity"]
SOLVE_KEYS_NC = ["problem_type", "knowledge_points", "givens", "target", "strategy",
                 "steps", "final_answer", "common_errors", "final_answer_canonical"]
PRAC_KEYS_CODE = ["stem", "type", "difficulty", "knowledge_points", "answer",
                  "code", "func_name", "test_cases", "hint"]
PRAC_KEYS_NC = ["stem", "type", "difficulty", "knowledge_points", "answer",
                "final_answer_canonical", "rubric", "hint"]
DIAG_KEYS = ["correct", "score", "first_wrong_step", "error_tags", "diagnosis", "remediation", "reexplain"]

_print_lock = threading.Lock()


def _solve_keys(c):
    return SOLVE_KEYS_CODE if c.code_enabled else SOLVE_KEYS_NC


def _prac_keys(c):
    return PRAC_KEYS_CODE if c.code_enabled else PRAC_KEYS_NC


def _teacher(messages, teacher, temperature=0.4, max_tokens=2048):
    return llm_mod.chat(messages, model=teacher, temperature=temperature, max_tokens=max_tokens)


def _clean_target(raw: str, keys: list[str]) -> str | None:
    """从 teacher 原始输出取 JSON，仅保留 schema 字段，去 steps[].verified 等，返回紧凑 JSON 串。"""
    obj = extract_json(raw)
    if not obj:
        return None
    out = {k: obj[k] for k in keys if k in obj}
    if "code" in keys and not out.get("code"):
        out["code"] = ""
    if isinstance(out.get("steps"), list):
        out["steps"] = [{k: s[k] for k in ("index", "action", "basis", "result") if k in s}
                        for s in out["steps"] if isinstance(s, dict)]
    return json.dumps(out, ensure_ascii=False)


def _sample(msgs, assistant_json, course: str) -> dict:
    return {"messages": list(msgs) + [{"role": "assistant", "content": assistant_json}], "course": course}


def _wrong_sys(c) -> str:
    if c.code_enabled:
        return (f"你是{c.display}教学数据构造助手。请扮演一个犯了【指定错误类型】的学生，"
                "针对给定题目写出一份【看似合理但确实错误】的作答（代码题给出错误的 Python 函数实现，函数名保持一致）。"
                "只输出一个 JSON：{\"student_code\":\"错误实现(代码题)\",\"student_answer\":\"错误作答文字\"}，不要解释。")
    return (f"你是{c.display}教学数据构造助手。请扮演一个犯了【指定错误类型】的学生，"
            "针对给定题目写出一份【看似合理但最终答案确实错误】的解答（含简短错误推导 + 一个错误的最终答案）。"
            "只输出一个 JSON：{\"student_answer\":\"错误解答含错误最终答案\"}，不要解释。")


# ---------- 题目生成（practice 样本 + solve/diagnose 的题源） ----------
def gen_problem(knowledge: str, difficulty: int, teacher: str, course: str) -> dict | None:
    c = get_course(course)
    if c.code_enabled:
        extra = "必须是【可编程的代码实现题】，给出 Python 参考实现与至少 3 条 test_cases。"
    else:
        extra = ("必须给出可机器校验的 final_answer_canonical（规范答案，见系统说明），"
                 "尽量出成有明确数值/选项/结构化答案的题（计算题或选择题）。")
    ctx = topic_ctx(course, knowledge)
    ground = (f"请参考以下《{c.short}》教材/课件片段命题（贴合材料中的概念与表述，"
              f"不要照抄原文）：\n{ctx}\n\n" if ctx else "")
    user = f"{ground}目标知识点：{knowledge}\n目标难度：{difficulty}（1易~5难）\n{extra}\n\n请出一道新的变式题："
    msgs = [{"role": "system", "content": build_practice_system(c)}, {"role": "user", "content": user}]
    try:
        raw = _teacher(msgs, teacher)
    except Exception:  # noqa: BLE001
        return None
    data = extract_json(raw)
    if not data:
        return None

    if c.code_enabled:
        code = (data.get("code") or "").strip()
        func = (data.get("func_name") or "").strip()
        tests = data.get("test_cases") or []
        if not (code and func and tests):
            return None
        if not run_python_tests(code, func, tests).passed:   # 沙箱 gate
            return None
        clean = _clean_target(raw, PRAC_KEYS_CODE)
        if not clean:
            return None
        return {"stem": data.get("stem", ""), "func": func, "code": code, "tests": tests,
                "knowledge": knowledge, "difficulty": difficulty, "msgs": msgs, "clean": clean,
                "answer": data.get("answer", ""), "canonical": None}

    # 非代码门：canonical 数值自洽 / rubric 覆盖 gate
    canonical = data.get("final_answer_canonical")
    rubric = data.get("rubric") or []
    if canonical:
        # 出题者自报 answer 与自报 canonical 不能自相矛盾
        if check_numeric(str(data.get("answer", "")), canonical).passed is False:
            return None
    elif not rubric:
        return None   # 既无 canonical 又无 rubric → 无法确定性校验 → 丢
    clean = _clean_target(raw, PRAC_KEYS_NC)
    if not clean:
        return None
    return {"stem": data.get("stem", ""), "func": "", "code": "", "tests": [],
            "knowledge": knowledge, "difficulty": difficulty, "msgs": msgs, "clean": clean,
            "answer": data.get("answer", ""), "canonical": canonical, "rubric": rubric}


# ---------- solve 样本 ----------
def make_solve(knowledge: str, difficulty: int, teacher: str, course: str) -> dict | None:
    c = get_course(course)
    prob = gen_problem(knowledge, difficulty, teacher, course)
    if not prob:
        return None
    msgs = solver_mod.build_messages(prob["stem"], context=topic_ctx(course, knowledge), course=course)
    try:
        raw = _teacher(msgs, teacher)
    except Exception:  # noqa: BLE001
        return None
    data = solver_mod.finalize(raw, self_test_cases=prob["tests"], messages=msgs, course=course)
    if data.get("error"):
        return None
    ver = data.get("verification")
    # 代码门：沙箱须过；非代码门：final_answer 与自报 canonical 数值自洽（finalize 已算）
    if ver is not None and ver.get("passed") is False:
        return None
    clean = _clean_target(raw, _solve_keys(c))
    if not clean:
        return None
    return _sample(msgs, clean, course)


# ---------- diagnose 样本 ----------
def make_diagnose(knowledge: str, difficulty: int, teacher: str, course: str) -> dict | None:
    c = get_course(course)
    prob = gen_problem(knowledge, difficulty, teacher, course)
    if not prob:
        return None
    err = random.choice(WRONG_TYPES)

    if c.code_enabled:
        wuser = (f"题目：{prob['stem']}\n参考答案(函数名 {prob['func']})：\n{prob['code']}\n"
                 f"要求错误类型：{name_of(err)}（{err}）。请写出该类型的错误学生作答：")
    else:
        wuser = (f"题目：{prob['stem']}\n参考答案：{prob['answer']}\n规范答案：{json.dumps(prob.get('canonical'), ensure_ascii=False)}\n"
                 f"要求错误类型：{name_of(err)}（{err}）。请写出该类型的错误学生作答（最终答案要确实错）：")
    try:
        wraw = _teacher([{"role": "system", "content": _wrong_sys(c)},
                         {"role": "user", "content": wuser}], teacher, temperature=0.7)
    except Exception:  # noqa: BLE001
        return None
    w = extract_json(wraw) or {}
    scode = (w.get("student_code") or "").strip()
    sans = (w.get("student_answer") or "").strip()
    if not (scode or sans):
        return None

    if c.code_enabled and scode:
        # 代码错解须经沙箱确认「确实错」
        if run_python_tests(scode, prob["func"], prob["tests"]).passed:
            return None
        msgs, verification = diag_mod.build_messages(
            prob["stem"], "", reference=prob["answer"] or prob["code"],
            student_code=scode, func_name=prob["func"], tests=prob["tests"], course=course)
    elif not c.code_enabled and prob.get("canonical"):
        # 非代码错解：数值反证「确实错」（wrong 答案 != canonical）——与沙箱同强的确定性 gate
        if check_numeric(sans, prob["canonical"]).passed is not False:
            return None   # None(判不了)/True(竟对了) 都丢，只留可确定为错的
        msgs, verification = diag_mod.build_messages(prob["stem"], sans, reference=prob["answer"], course=course)
    else:
        # 无 canonical（纯 rubric 题）：无法确定性反证，丢弃以保证错样本可靠
        return None

    try:
        raw = _teacher(msgs, teacher)
    except Exception:  # noqa: BLE001
        return None
    d = diag_mod.finalize(raw, verification, messages=msgs)
    if d.get("error"):
        return None
    tags = d.get("error_tags") or []
    if d.get("correct") is True:
        return None
    if not tags or any(normalize(t) not in ERROR_CODES for t in tags):
        return None
    if not isinstance(d.get("first_wrong_step"), int):
        return None
    clean = _clean_target(raw, DIAG_KEYS)
    if not clean:
        return None
    return _sample(msgs, clean, course)


# ---------- practice 样本 ----------
def make_practice(knowledge: str, difficulty: int, teacher: str, course: str) -> dict | None:
    prob = gen_problem(knowledge, difficulty, teacher, course)
    if not prob:
        return None
    return _sample(prob["msgs"], prob["clean"], course)


MAKERS = {"solve": make_solve, "diagnose": make_diagnose, "practice": make_practice}


def _count_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def build_intent(intent: str, target: int, teacher: str, workers: int, course: str) -> int:
    """边生成边追加落盘（崩溃/中断安全 + 可续跑）。返回该 intent 最终样本总数。"""
    maker = MAKERS[intent]
    out_dir = os.path.join(SFT_DIR, course)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{intent}.jsonl")
    have = _count_lines(path)
    if have >= target:
        print(f"  [{course}/{intent}] 已有 {have} ≥ 目标 {target}，跳过")
        return have
    need = target - have
    topics = get_course(course).topics
    kept = attempts = 0
    max_attempts = need * 10           # 非代码门过滤更严，留足预算
    io_lock = threading.Lock()
    fout = open(path, "a", encoding="utf-8")

    def task(_i):
        return maker(random.choice(topics), random.choice(DIFFICULTIES), teacher, course)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = set()
            for _ in range(min(max_attempts, workers * 3)):
                futures.add(ex.submit(task, 0))
            while futures and kept < need and attempts < max_attempts:
                done = next(as_completed(futures))
                futures.discard(done)
                attempts += 1
                try:
                    s = done.result()
                except Exception:  # noqa: BLE001
                    s = None
                if s:
                    with io_lock:
                        fout.write(json.dumps(s, ensure_ascii=False) + "\n")
                        fout.flush()
                        kept += 1
                if kept < need and attempts + len(futures) < max_attempts:
                    futures.add(ex.submit(task, 0))
                with _print_lock:
                    print(f"\r  [{course}/{intent}] 通过 {have+kept}/{target} | 本轮尝试 {attempts}", end="", flush=True)
    finally:
        fout.close()
    print()
    return have + kept


def write_jsonl(path: str, rows: list[dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def merge_all(val_ratio: float) -> tuple[int, int]:
    """扫描 data/sft/<course>/*.jsonl 合并，按 course 分层切分 train/val。返回 (train, val)。"""
    by_course: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(SFT_DIR, "*", "*.jsonl"))):
        course = os.path.basename(os.path.dirname(path))
        for line in open(path, encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            row.setdefault("course", course)   # 补 course（迁移来的旧 ds 行无此字段）
            by_course[course].append(row)

    train: list[dict] = []
    val: list[dict] = []
    for course, rows in by_course.items():
        random.shuffle(rows)
        nv = max(1, int(len(rows) * val_ratio)) if rows else 0
        val.extend(rows[:nv])
        train.extend(rows[nv:])
        print(f"  {course}: {len(rows)} 条 → train {len(rows)-nv} / val {nv}")
    random.shuffle(train)
    random.shuffle(val)
    write_jsonl(os.path.join(SFT_DIR, "train.jsonl"), train)
    write_jsonl(os.path.join(SFT_DIR, "val.jsonl"), val)
    return len(train), len(val)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", default="ds-algo", help=f"课程：{course_ids()}")
    ap.add_argument("--intent", choices=["solve", "diagnose", "practice", "all"], default="all")
    ap.add_argument("--target", type=int, default=40, help="每个 intent 目标样本数（过滤后）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--teacher", default=TEACHER_DEFAULT)
    ap.add_argument("--val-ratio", type=float, default=0.05)
    ap.add_argument("--grounded", action="store_true",
                    help="注入 RAG 教材/课件片段做 grounded 蒸馏（需先 ingest 课件）")
    ap.add_argument("--merge-only", action="store_true", help="跳过生成，仅重建 train/val")
    args = ap.parse_args()

    if not args.merge_only:
        if not is_valid_course(args.course):
            print(f"未知 course={args.course}，可选 {course_ids()}"); return
        if not llm_mod.settings.text_llm_ready:
            print("未配置 LLM，无法蒸馏。请检查 .env"); return
        global _GROUNDED
        _GROUNDED = args.grounded
        intents = ["solve", "diagnose", "practice"] if args.intent == "all" else [args.intent]
        print(f"course={args.course} | teacher={args.teacher} | 每 intent 目标={args.target} "
              f"| workers={args.workers} | grounded={_GROUNDED}")
        if _GROUNDED:
            prewarm_contexts(args.course, get_course(args.course).topics)
        for it in intents:
            print(f"[{args.course}/{it}] 生成中（边生成边落盘，可续跑）...")
            n = build_intent(it, args.target, args.teacher, args.workers, args.course)
            print(f"  → data/sft/{args.course}/{it}.jsonl 现有 {n} 条")

    print("\n[合并] 扫描 data/sft/<course>/ 分层切分 train/val ...")
    ntr, nva = merge_all(args.val_ratio)
    print(f"合计 → train {ntr} / val {nva}")


if __name__ == "__main__":
    main()

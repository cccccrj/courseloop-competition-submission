"""真题/试卷 → 题库 gold 入库（408 四门）。

真题 PDF 版面乱、含选择题+综合题+子问题，直接入库不可靠，故分两段（人工 QA 卡在中间）：

  阶段一 抽取（LLM 辅助，产出草稿供人工校对，**不直接入库**）：
      # 扫描/拍照真题（fitz 抽不出字）→ 先 OCR（见 scripts.ocr_bench.transcribe_all），再喂 full.md：
      rag_env python -m scripts.ingest_past_papers extract \
          --md data/ocr/transcripts/sample_exam --source sample-exam --course co \
          --out data/processed/papers/sample_exam.json
      # 文本型 PDF 仍可直接 --pdf；若有单独答案 PDF 再加 --answer-pdf ...
      # 产出后**人工核对 answer/canonical** 再 load

  阶段二 入库（把人工校对过的 JSON 写进题库）：
      rag_env python -m scripts.ingest_past_papers load \
          --in data/processed/papers/408_2019.json

题目 JSON schema（每条）：
  {
    "course": "co",                     # ds-algo|co|os|net（缺省时用 --course）
    "type": "计算题",                    # 题型
    "stem": "……",
    "knowledge": "Cache",               # 知识点
    "difficulty": 3,
    "verification_type": "numeric"|"rule"|"unit_test",
    "canonical": {"kind":"percent","value":0.375,"tol":0.001},   # numeric 必填（见 numeric_checker）
    "answer": "参考答案文字",             # rule/展示用；numeric 若无 canonical 也可放规范答案串
    "rubric": [{"desc":"得分点","score":4,"evidence":"必须命中的关键词"}]   # rule 用
  }
入库规则：numeric→canonical 存进 problems.answer(JSON 串，与 /problems/judge 的 _canonical_of 对齐)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from backend.core import db
from backend.core.courses import is_valid_course
from scripts.knowledge_align import KnowledgeAligner, stem_key

_SEG_SYS = (
    "你是 408 真题结构化助手。把给定真题原文切成一道道独立题目，只输出 JSON 数组，每个元素字段：\n"
    '{"course":"ds-algo|co|os|net","type":"题型","stem":"完整题干(含选项)","knowledge":"知识点",'
    '"difficulty":1-5,"verification_type":"numeric|rule|unit_test",'
    '"canonical":{数值题的规范答案,见说明;非数值题填 null},"answer":"参考答案/要点",'
    '"rubric":[{"desc":"得分点","score":分值,"evidence":"关键词"}]}\n'
    "canonical 规范（numeric 题必填）：kind ∈ scalar/percent/int/choice/bits/ipv4net/set；"
    "选择题用 {\"kind\":\"choice\",\"value\":\"B\"}；能给明确数值/结构化答案的用对应 kind；"
    "多解或需证明的题 verification_type=rule 且 canonical=null。不要编造答案，无法确定就留 answer 空。"
)


def extract_text(pdf_path: str) -> str:
    import fitz
    return "\n".join(p.get_text() for p in fitz.open(pdf_path))


def read_md(md_path: str) -> str:
    """读 OCR 转写的 Markdown（扫描/拍照真题走这条：fitz 抽不出文字，故先 OCR）。
    支持传单卷 full.md，或整卷目录（自动拼接 page_*.md）。"""
    p = Path(md_path)
    if p.is_dir():
        pages = sorted(p.glob("page_*.md"))
        return "\n\n".join(f.read_text(encoding="utf-8") for f in pages)
    return p.read_text(encoding="utf-8")


def md_chunks(md_path: str) -> list[tuple[str, str]]:
    """把整卷切成小块逐块喂 LLM（避免单次输出撞 max_tokens 被截断）。
    目录→每个 page_*.md 一块；full.md→按 '## 第 N 页' 分块；单页 md→整块。"""
    p = Path(md_path)
    if p.is_dir():
        return [(f.stem, f.read_text(encoding="utf-8")) for f in sorted(p.glob("page_*.md"))]
    text = p.read_text(encoding="utf-8")
    parts = re.split(r"(?=^##\s*第\s*\d+\s*页)", text, flags=re.M)
    parts = [s for s in parts if s.strip()]
    return [(f"块{i+1}", s) for i, s in enumerate(parts)] if len(parts) > 1 else [(p.stem, text)]


def _parse_items(raw: str) -> list[dict]:
    """稳健解析 LLM 切题输出——兼容顶层 JSON 数组 / {"items":[…]} / 单对象，
    并能从**被 max_tokens 截断**的数组里抢救已完整的题（逐个 raw_decode）。
    （extract_json 只认对象 {…} 不认数组，故此处单独处理。）"""
    from backend.core.jsonio import extract_json
    s = re.sub(r"```$", "", re.sub(r"^```(?:json)?", "", (raw or "").strip()).strip()).strip()
    a = s.find("[")
    if a != -1:
        body, idx, n = s, a + 1, len(s)
        dec, items = json.JSONDecoder(), []
        while idx < n:
            while idx < n and body[idx] in " \t\r\n,":
                idx += 1
            if idx >= n or body[idx] != "{":
                break
            try:
                obj, end = dec.raw_decode(body, idx)
            except json.JSONDecodeError:
                break  # 截断在此，前面已完整的题照收
            if isinstance(obj, dict):
                items.append(obj)
            idx = end
        if items:
            return items
    data = extract_json(raw)
    if isinstance(data, dict):
        data = data.get("items") or data.get("questions") or ([data] if data else [])
    return data or []


def llm_segment(paper_text: str, answer_text: str = "", default_course: str = "") -> list[dict]:
    """LLM 把真题原文切题（草稿，需人工 QA）。"""
    from backend.core import llm as llm_mod
    from backend.core.config import settings
    user = f"真题原文：\n{paper_text[:24000]}"
    if answer_text:
        user += f"\n\n官方答案：\n{answer_text[:12000]}"
    if default_course:
        user += f"\n\n（本卷默认 course={default_course}，无法判断时用它）"
    raw = llm_mod.chat([{"role": "system", "content": _SEG_SYS}, {"role": "user", "content": user}],
                       model=settings.workhorse_model, temperature=0.2, max_tokens=8000)
    return _parse_items(raw)


# canonical 能判分的那几种 kind（与 numeric_checker / mock_exam.is_choice 同一把尺子）
_CANONICAL_KINDS = ("scalar", "percent", "int", "choice", "bits", "ipv4net", "set")
_CHOICE_LETTER = re.compile(r"^[A-Za-z]$")


def canonical_ok(canon) -> bool:
    """canonical 是否真的能判分。挡的是切题模型给出的**自相矛盾的标注**。

    实测一批草稿里出现过 `{"kind":"choice","value":"0xffeb"}`、
    `{"kind":"choice","value":"512，1ABC，83."}` —— 那些其实是填空题/计算题，
    被顺手标成了 choice。放进去的后果比"少几道题"严重得多：
    `mock_exam.is_choice()` 只看 kind，会把它们抽进模考**单选段**，
    于是卷面上出现一道没有 ABCD 可选的"单选题"，而判分器 `_check_choice`
    从作答里抽不出字母，只能返回"无法判定"。一道永远判不了的题。

    所以 choice 的 value 必须是单个字母（或一组字母，多选题）。
    不合格的不是丢掉，调用方会把它降级成 rule —— 题干和答案都还在，
    只是不进选择题池。
    """
    if not isinstance(canon, dict):
        return False
    kind = canon.get("kind")
    if kind not in _CANONICAL_KINDS:
        return False
    if kind == "choice":
        vals = canon.get("value")
        vals = vals if isinstance(vals, list) else [vals]
        return bool(vals) and all(_CHOICE_LETTER.match(str(v).strip()) for v in vals)
    return True


def _verification_type_of(q: dict) -> str:
    """定这道题按什么方式判分。**canonical 说了算，不是 verification_type 那个字段说了算。**

    切题模型经常自相矛盾：给出了 `canonical={"kind":"choice","value":"B"}`，却把
    verification_type 标成 rule（实测一份 37 题的草稿里，10 道选择题全是这个样子）。
    照字段直接入库的后果极其隐蔽 —— 题确实进库了、总量确实涨了，但
    `mock_exam.is_choice()` 的判据是 `verification_type=="numeric"` 且 canonical.kind
    是 choice，于是这批题**一道都不会进模考单选池**，脚本报"灌了 37 题"、组卷照旧喊缺。

    所以：只要有一个可判分的 canonical，就按 numeric 入库。反过来不成立 ——
    没有 canonical 的题即便标了 numeric 也走不通（下面会跳过它），仍归 rule。
    """
    if canonical_ok(q.get("canonical")):
        return "numeric"
    declared = q.get("verification_type")
    if declared in ("numeric", "unit_test", "rule"):
        # 声明了 numeric 却没有 canonical：保留声明，由下面那段统一跳过并打印原因
        return declared
    return "rule"


def insert_questions(questions: list[dict], source: str, default_course: str = "",
                     replace: bool = False) -> dict:
    """把（人工校对过的）题目写进题库。返回统计。

    ``replace=True`` 时先删掉同 source 的旧题再灌，用于校对后重灌；不加则是纯追加。
    知识点走 KnowledgeAligner 对齐到大纲，不再用自由文本 get_or_create 建无章节的孤儿节点。
    """
    stat = {"numeric": 0, "rule": 0, "unit_test": 0, "skipped": 0, "duplicate": 0}
    aligners: dict[str, KnowledgeAligner] = {}
    if replace:
        for cid in {q.get("course") or default_course for q in questions}:
            if is_valid_course(cid):
                n = db.clear_problem_bank(cid, source=source)
                if n:
                    print(f"[replace] {cid} 删除旧 source={source} 题目 {n} 道")
    # 同一年份常有多份扫描件（"组原2023年卷" 与 "计算机组成原理2023试卷" 就是同一场考试），
    # 逐卷入库会把同一道题灌进去两遍 —— 模考组卷随后会在一张卷子上出两道一样的题。
    # 按题干指纹去重，既挡跨卷重复，也让同一份卷重复 load 不会叠加。
    existing: dict[str, set[str]] = {}
    for cid in {q.get("course") or default_course for q in questions}:
        if not is_valid_course(cid):
            continue
        with db.get_conn() as conn:
            rows = conn.execute("SELECT stem FROM problems WHERE course_id=?", (cid,)).fetchall()
        existing[cid] = {stem_key(r[0]) for r in rows}

    for q in questions:
        course = q.get("course") or default_course
        if not is_valid_course(course):
            print(f"[跳过] 未知 course={course!r}: {str(q.get('stem'))[:30]}")
            stat["skipped"] += 1
            continue
        vtype = _verification_type_of(q)
        stem = (q.get("stem") or "").strip()
        if not stem:
            stat["skipped"] += 1
            continue
        key = stem_key(q.get("stem") or "")
        pool = existing.setdefault(course, set())
        if key in pool:
            stat["duplicate"] += 1
            continue
        pool.add(key)
        aligner = aligners.setdefault(course, KnowledgeAligner(course))
        raw_knowledge = q.get("knowledge", "")
        knowledge, chapter, kdiff = aligner.align(raw_knowledge) if raw_knowledge else ("", "", 2)
        difficulty = int(q.get("difficulty", 3))

        # 答案与得分点都没有的题不入库：判不了分，讲评时"参考："后面是空的，
        # 学生只会觉得系统坏了。切题模型漏抽答案是常事（实测 235 题里 20 道）。
        if not (q.get("answer") or "").strip() and not q.get("rubric"):
            print(f"[跳过·无答案无得分点] {stem[:30]}")
            stat["skipped"] += 1
            continue

        if vtype == "numeric":
            canonical = q.get("canonical")
            if not canonical:
                print(f"[跳过·numeric 缺 canonical] {stem[:30]}")
                stat["skipped"] += 1
                continue
            answer = json.dumps(canonical, ensure_ascii=False)  # canonical 存进 answer 字段
            pid = db.insert_problem(course, q.get("type", "计算题"), stem, difficulty,
                                    answer, "numeric", knowledge=knowledge, source=source)
        elif vtype == "unit_test":
            # 代码题（数据结构）：需 ref_code+func+tests，通常仍走 seed_problems，此处兜底存展示
            pid = db.insert_problem(course, q.get("type", "代码实现"), stem, difficulty,
                                    q.get("answer", ""), "unit_test", knowledge=knowledge,
                                    func_name=q.get("func_name", ""), ref_code=q.get("ref_code", ""),
                                    source=source)
            for t in q.get("tests", []):
                db.add_test_case(pid, json.dumps(t["args"]), json.dumps(t["expected"]),
                                 1 if t.get("hidden") else 0)
        else:  # rule
            pid = db.insert_problem(course, q.get("type", "分析题"), stem, difficulty,
                                    q.get("answer", ""), "rule", knowledge=knowledge, source=source)
            for r in q.get("rubric", []):
                db.add_rubric_item(pid, r.get("desc", ""), float(r.get("score", 0)), r.get("evidence", ""))

        if knowledge:
            db.link_problem_knowledge(pid, db.upsert_knowledge_point(course, chapter, knowledge,
                                                                     difficulty=kdiff))
        stat[vtype] = stat.get(vtype, 0) + 1
    for cid, al in aligners.items():
        stat[f"align_{cid}"] = dict(al.stats)
    return stat


def cmd_extract(args):
    answer = extract_text(args.answer_pdf) if args.answer_pdf else ""
    if args.md:  # OCR 转写 Markdown（扫描/拍照真题）→ 分块逐块切题，避免输出截断
        chunks = md_chunks(args.md)
        print(f"[抽取] {args.md} → {len(chunks)} 块，逐块 LLM 切题…")
        questions: list[dict] = []
        for label, text in chunks:
            qs = llm_segment(text, answer, default_course=args.course or "")
            print(f"  {label}: {len(text)} 字 → {len(qs)} 题", flush=True)
            questions.extend(qs)
    elif args.pdf:  # 文本型 PDF（fitz 可直接抽字）
        paper = extract_text(args.pdf)
        print(f"[抽取] 原文 {len(paper)} 字，调用 LLM 切题…")
        questions = llm_segment(paper, answer, default_course=args.course or "")
    else:
        raise SystemExit("需指定 --md（OCR 转写）或 --pdf（文本型 PDF）之一")
    for q in questions:
        q.setdefault("source", args.source)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[完成] 共切出 {len(questions)} 题 → {out}\n  ⚠️ 请人工核对 answer/canonical 后再 load 入库。")


def cmd_load(args):
    db.init_db()
    questions = json.loads(Path(args.infile).read_text(encoding="utf-8"))
    source = args.source or f"papers:{Path(args.infile).stem}"
    stat = insert_questions(questions, source=source, default_course=args.course or "",
                            replace=getattr(args, "replace", False))
    print(f"[入库] source={source} → {stat}")
    print("知识点分布(该门)：", db.knowledge_distribution(args.course) if args.course else "(多门，略)")


def main():
    ap = argparse.ArgumentParser(description="408 真题/试卷 → 题库 gold 入库（两段式）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="LLM 切题产出草稿（需人工 QA，不入库）")
    e.add_argument("--md", default="", help="OCR 转写 Markdown（单卷 full.md 或整卷目录）——扫描/拍照真题走这条")
    e.add_argument("--pdf", default="", help="文本型 PDF（fitz 可直接抽字）")
    e.add_argument("--answer-pdf", default="")
    e.add_argument("--source", required=True, help="来源标签，如 真题-2019")
    e.add_argument("--course", default="", help="默认课程 ds-algo|co|os|net")
    e.add_argument("--out", required=True, help="草稿 JSON 输出路径")
    e.set_defaults(func=cmd_extract)

    l = sub.add_parser("load", help="把校对过的题目 JSON 入库")
    l.add_argument("--in", dest="infile", required=True)
    l.add_argument("--source", default="", help="来源标签，缺省用文件名")
    l.add_argument("--replace", action="store_true",
                   help="先删掉同 source 的旧题再灌（校对后重灌用）；缺省是纯追加")
    l.add_argument("--course", default="", help="缺省课程（JSON 内未标 course 时用）")
    l.set_defaults(func=cmd_load)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

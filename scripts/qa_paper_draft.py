"""真题草稿的机器校对：把 LLM 切出来的题规整成可判分的形态，并挑出必须人工看的条目。

`ingest_past_papers extract` 出的草稿有三个系统性毛病，直接 load 会静默降级：

1. **选择题被标成 `rule`**（answer 只是 "B"）。而 `mock_exam.is_choice` 认的是
   canonical 的 `kind=="choice"` —— 标成 rule 的选择题进了库，组卷不算它是单选，
   判分也退化成关键词覆盖，等于白入。
2. **题型串成占位串**（`type: "题型"`），是 LLM 把表头当内容抄了下来。
3. **缺 answer**：切题时答案在另一页（试卷与解析分开），这类必须人工补，不能猜。

用法：
    python -m scripts.qa_paper_draft --in <草稿.json>                    # 只出报告
    python -m scripts.qa_paper_draft --in <草稿.json> --out <校对.json>  # 规整后另存
    python -m scripts.qa_paper_draft --in <草稿.json> --out <校对.json> --drop-unanswered

规整之后仍需人工过一遍：OCR 会漏选项（2023 卷的"① … ③ …"就缺了②④），
机器看不出来，只有对着原卷才发现。
"""
from __future__ import annotations

import argparse
import json
import re

CHOICE_TYPES = ("选择", "单选", "多选")
TRUE_FALSE_TYPES = ("判断", "是非")
BAD_TYPES = {"题型", "", "未知"}

_LETTER = re.compile(r"^[（(\[【]?\s*([A-Da-d])\s*[)）\]】]?\s*[.。、]?\s*$")
_TRUE = ("对", "正确", "是", "T", "true", "√")
_FALSE = ("错", "错误", "否", "F", "false", "×")


def _letters(answer: str) -> list[str]:
    """从答案串里抽出选项字母。

    认四种写法：'B'、'(B)'、'B.'、'AC'/'A、C'，以及 **'C. 执行 DMA 操作'**
    —— 最后这种最常见：模型顺手把选项正文也抄了下来，只看整串匹配会全判成"不是字母"。
    """
    text = (answer or "").strip()
    m = _LETTER.match(text)
    if m:
        return [m.group(1).upper()]
    letters = re.findall(r"[A-Da-d]", text)
    # 多选先判：整串只剩字母和分隔符时才算，避免把整句解析里的字母抓出来。
    # 这一条必须排在"首字母+选项正文"之前，否则 "A、C" 会被当成 A 后面跟着正文。
    if letters and len(re.sub(r"[A-Da-d\s、,，和与/]", "", text)) == 0:
        seen, out = set(), []
        for ch in letters:
            up = ch.upper()
            if up not in seen:
                seen.add(up)
                out.append(up)
        return out
    lead = re.match(r"^([A-Da-d])\s*[.。、．)）]\s*\S", text)
    if lead:
        return [lead.group(1).upper()]
    return []


def _truth(answer: str) -> bool | None:
    text = (answer or "").strip()
    if any(text.startswith(t) or text == t for t in _TRUE):
        return True
    if any(text.startswith(f) or text == f for f in _FALSE):
        return False
    return None


def normalize(item: dict) -> tuple[dict, list[str]]:
    """返回 (规整后的题, 该题的问题清单)。不改原对象。"""
    q = dict(item)
    issues: list[str] = []
    qtype = (q.get("type") or "").strip()
    stem = (q.get("stem") or "").strip()
    answer = str(q.get("answer") or "").strip()

    if not stem:
        issues.append("缺题干")
    if qtype in BAD_TYPES:
        # 题干里带 A. B. C. D. 的基本都是选择题，据此回填比留个占位串强
        q["type"] = "单项选择题" if re.search(r"[A-D][.、．]", stem) else "解答题"
        issues.append(f"题型异常({qtype!r}) → 回填为 {q['type']}")
        qtype = q["type"]
    if not answer:
        issues.append("缺答案（多半在解析页，需人工补）")

    if any(k in qtype for k in CHOICE_TYPES) and answer:
        letters = _letters(answer)
        if letters:
            q["canonical"] = ({"kind": "choice", "value": letters[0]} if len(letters) == 1
                              else {"kind": "choice", "value": letters})
            q["verification_type"] = "numeric"
            q.pop("rubric", None)
        else:
            issues.append(f"选择题但答案不是选项字母：{answer[:30]!r}")
    elif any(k in qtype for k in TRUE_FALSE_TYPES) and answer:
        truth = _truth(answer)
        if truth is None:
            # 答案是整段论述 → 这题本来就不是判断题，是模型把题型贴错了。
            # 短答案读不出对错才值得人工看一眼。
            if len(answer) > 8:
                q["type"] = "解答题"
                q["verification_type"] = "rule"
                issues.append("题型贴成判断题但答案是论述 → 改判为解答题")
            else:
                issues.append(f"判断题但答案读不出对错：{answer[:30]!r}")
        else:
            # 判断题按 choice 归一：判分器认的是 canonical，前端也照选择题渲染
            q["canonical"] = {"kind": "choice", "value": "对" if truth else "错"}
            q["verification_type"] = "numeric"
            q.pop("rubric", None)
    elif q.get("canonical"):
        q["verification_type"] = "numeric"

    # 收口到判分器认得的三种。LLM 会自造 "choice"/"填空"/null 这类值，而
    # ingest_past_papers 只认 numeric / unit_test，其余一律当 rule ——
    # 于是一道写着 "choice" 的选择题会被当成概念题按关键词覆盖判分，静默降级。
    vt = q.get("verification_type")
    if vt == "unit_test":
        # 计组/操作系统/网络没有可跑的沙箱题；没有 tests 的 unit_test 判不了
        if not q.get("tests") or q.get("course") != "ds-algo":
            issues.append("unit_test 无沙箱样例 → 改判为 rule")
            vt = "rule"
    if vt not in ("numeric", "rule", "unit_test"):
        if vt not in (None, "", "rule"):
            issues.append(f"判分类型不合法({vt!r}) → 按 {'numeric' if q.get('canonical') else 'rule'} 处理")
        vt = "numeric" if q.get("canonical") else "rule"
    if vt == "numeric" and not q.get("canonical"):
        issues.append("标了 numeric 却没有 canonical → 改判为 rule")
        vt = "rule"
    q["verification_type"] = vt

    return q, issues


def qa(items: list[dict], drop_unanswered: bool = False) -> tuple[list[dict], dict]:
    kept, report = [], {"total": len(items), "kept": 0, "dropped": 0,
                        "issues": [], "by_verification": {}, "choice_count": 0}
    for idx, item in enumerate(items, 1):
        q, issues = normalize(item)
        unanswered = "缺答案" in "".join(issues) or "缺题干" in "".join(issues)
        if issues:
            report["issues"].append({"index": idx, "stem": (q.get("stem") or "")[:60],
                                     "issues": issues})
        if drop_unanswered and unanswered:
            report["dropped"] += 1
            continue
        kept.append(q)
    report["kept"] = len(kept)
    for q in kept:
        vt = q.get("verification_type") or "rule"
        report["by_verification"][vt] = report["by_verification"].get(vt, 0) + 1
        canon = q.get("canonical")
        if isinstance(canon, dict) and canon.get("kind") == "choice":
            report["choice_count"] += 1
    return kept, report


def main() -> None:
    ap = argparse.ArgumentParser(description="真题草稿机器校对")
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", default="", help="规整后另存；不给则只出报告")
    ap.add_argument("--drop-unanswered", action="store_true",
                    help="丢掉缺答案/缺题干的题（默认保留，交人工补）")
    args = ap.parse_args()

    with open(args.infile, encoding="utf-8") as f:
        items = json.load(f)
    kept, report = qa(items, args.drop_unanswered)

    print(f"[{args.infile}] {report['total']} 题 → 保留 {report['kept']}，丢弃 {report['dropped']}")
    print(f"  判分类型：{report['by_verification']}；其中可自动判分的选择/判断题 {report['choice_count']} 道")
    for row in report["issues"][:20]:
        print(f"  #{row['index']:>2} {row['stem']}… → {'；'.join(row['issues'])}")
    if len(report["issues"]) > 20:
        print(f"  …另有 {len(report['issues']) - 20} 条")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False, indent=2)
        print(f"[写入] {args.out}（人工过一遍再 load）")


if __name__ == "__main__":
    main()

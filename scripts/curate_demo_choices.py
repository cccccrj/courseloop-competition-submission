"""人工审查规则：清理视觉抽取的缺图题，并修正可确定的识别错答。"""
from __future__ import annotations

import argparse
import json

from backend.core import db


REJECT_CONTAINS = (
    "根据题中表格数据",
    "该图为6个顶点",
    "边(A,C)和边(B,D)",
    "关于平衡二叉树，以下说法错误",
    "关于平衡二叉树（AVL树）的以下操作",
)

# 这些结论不依赖原卷图表，可由定义直接复核。
ANSWER_CORRECTIONS = {
    "中序线索化二叉树中": "B",          # 右线索指向中序后继
    "以下关于红黑树的叙述": "D",        # 根黑、NIL 黑、各路径黑高相同
    '字符串P="aabaab"': "C",           # 最长公共前后缀 aab，长度 3
    "下列关于二叉树的叙述": "A",        # 左子树最右结点是前驱，不是后继
}


def curate(apply: bool, source: str = "local-demo-408-mock") -> dict:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id,stem,answer FROM problems WHERE source=? ORDER BY id", (source,)).fetchall()
        rejected, corrected = [], []
        for row in rows:
            if any(text in row["stem"] for text in REJECT_CONTAINS):
                rejected.append(row["id"])
                continue
            for prefix, answer in ANSWER_CORRECTIONS.items():
                if row["stem"].startswith(prefix):
                    current = json.loads(row["answer"] or "{}")
                    if current.get("value") != answer:
                        corrected.append((row["id"], answer))
                    break
        if apply:
            if rejected:
                holes = ",".join("?" * len(rejected))
                conn.execute(f"DELETE FROM problem_knowledge WHERE problem_id IN ({holes})", rejected)
                conn.execute(f"DELETE FROM test_cases WHERE problem_id IN ({holes})", rejected)
                conn.execute(f"DELETE FROM rubric_items WHERE problem_id IN ({holes})", rejected)
                conn.execute(f"DELETE FROM problems WHERE id IN ({holes})", rejected)
            for problem_id, answer in corrected:
                conn.execute("UPDATE problems SET answer=? WHERE id=?",
                             (json.dumps({"kind": "choice", "value": answer}, ensure_ascii=False),
                              problem_id))
        kept = len(rows) - len(rejected)
    return {"source_rows": len(rows), "rejected": rejected, "corrected": corrected,
            "kept": kept, "applied": apply}


def main() -> None:
    parser = argparse.ArgumentParser(description="演示选择题人工审查规则")
    parser.add_argument("--source", default="local-demo-408-mock")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(curate(args.apply, args.source))


if __name__ == "__main__":
    main()

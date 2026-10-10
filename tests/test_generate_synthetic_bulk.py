import json

import scripts.generate_synthetic_bulk as bulk
from scripts.generate_synthetic_bulk import CELL_TARGETS, TOTAL_TARGET, _practice_slots


def test_bulk_quota_is_balanced_and_totals_3000():
    assert TOTAL_TARGET == 3000
    assert sum(value for (task, _), value in CELL_TARGETS.items() if task == "practice") == 300
    assert sum(value for (task, _), value in CELL_TARGETS.items() if task == "diagnose") == 900
    assert sum(value for (task, _), value in CELL_TARGETS.items() if task == "grounded_qa") == 900
    assert sum(value for (task, _), value in CELL_TARGETS.items() if task == "solve") == 900


def test_bulk_practice_ids_are_unique():
    rows = _practice_slots("os", 120)
    assert len(rows) == len({row["candidate_id"] for row in rows}) == 120


def test_solve_pool_excludes_targeted_source_ids(monkeypatch):
    seeds = [
            {
                "candidate_id": f"source-{i}",
                "parsed": {"stem": f"question-{i} A. yes B. no",
                           "final_answer_canonical": {"kind": "choice", "value": "A"}},
            }
        for i in range(230)
    ]
    monkeypatch.setattr(bulk, "_practice_rows", lambda course: seeds)
    monkeypatch.setattr(bulk, "_bulk_practice_rows", lambda course: [])
    monkeypatch.setattr(bulk, "_supplemental_practice_rows", lambda course: [])

    rows = bulk._solve_slots("os", 300, {"source-1", "source-2"})

    assert len(rows) == 228
    assert {row["source_candidate_id"] for row in rows}.isdisjoint({"source-1", "source-2"})


def test_solve_pool_excludes_malformed_legacy_canonical(monkeypatch):
    valid = [
        {
            "candidate_id": f"valid-{i}",
            "parsed": {
                "stem": f"question-{i} A. yes B. no",
                "final_answer_canonical": {"kind": "choice", "value": "A"},
            },
        }
        for i in range(225)
    ]
    malformed = {
        "candidate_id": "malformed",
        "parsed": {
            "stem": "compound answer mislabeled as choice",
            "final_answer_canonical": {
                "kind": "choice",
                "value": [["内存", "外存"], ["RAM", "DAM"]],
            },
        },
    }
    monkeypatch.setattr(bulk, "_practice_rows", lambda course: valid + [malformed])
    monkeypatch.setattr(bulk, "_bulk_practice_rows", lambda course: [])
    monkeypatch.setattr(bulk, "_supplemental_practice_rows", lambda course: [])

    rows = bulk._solve_slots("os", 300)

    assert len(rows) == 225
    assert "malformed" not in {row["source_candidate_id"] for row in rows}


def test_solve_pool_excludes_choice_without_options_and_incomplete_multipart_gold(monkeypatch):
    valid = [
        {"candidate_id": f"valid-{i}", "parsed": {
            "stem": f"问题 {i} A. 正确 B. 错误",
            "final_answer_canonical": {"kind": "choice", "value": "A"},
        }}
        for i in range(225)
    ]
    invalid = [
        {"candidate_id": "opaque-choice", "parsed": {
            "stem": "这个概念属于哪一类？",
            "final_answer_canonical": {"kind": "choice", "value": "B"},
        }},
        {"candidate_id": "multipart", "parsed": {
            "stem": "请回答以下问题：\n1. 求结果\n2. 判断溢出",
            "final_answer_canonical": {"kind": "int", "value": 1},
        }},
    ]
    monkeypatch.setattr(bulk, "_practice_rows", lambda course: valid + invalid)
    monkeypatch.setattr(bulk, "_bulk_practice_rows", lambda course: [])
    monkeypatch.setattr(bulk, "_supplemental_practice_rows", lambda course: [])

    rows = bulk._solve_slots("co", 300)

    assert len(rows) == 225
    assert {row["source_candidate_id"] for row in rows}.isdisjoint(
        {"opaque-choice", "multipart"})


def test_solve_schema_metric_is_independent_from_wrong_answer(monkeypatch):
    parsed = {
        "problem_type": "选择题",
        "knowledge_points": ["进程调度"],
        "givens": ["题干"],
        "target": "选择正确答案",
        "strategy": "逐项判断",
        "steps": [{"index": 1, "action": "判断", "basis": "定义", "result": "B"}],
        "final_answer": "答案：B",
        "common_errors": [],
        "final_answer_canonical": {"kind": "choice", "value": "B"},
    }

    def fake_validated_call(*args, **kwargs):
        call = {"model": args[0], "error": "", "usage": {}, "raw_output": "{}"}
        # 模拟 JSON/字段完整，但确定性答案契约失败。
        return [call], parsed, False

    monkeypatch.setattr(bulk, "_validated_call", fake_validated_call)
    row = bulk._solve({
        "candidate_id": "candidate-1",
        "course": "os",
        "source_candidate_id": "source-1",
        "problem": {
            "stem": "以下哪项正确？ A. 正确项 B. 错误项",
            "final_answer_canonical": {"kind": "choice", "value": "A"},
        },
    })

    assert row["schema_ok"] is True
    assert row["schema_by_model"] == {bulk.KIMI: True, bulk.QWEN: True}
    assert row["accepted"] is False
    assert "teacher_disagreement_or_wrong" in row["reject_reasons"]


def test_source_tokens_catch_cross_task_reuse():
    targeted_solve = {"intent": "solve", "source_candidate_id": "legacy-7"}
    bulk_diagnose = {"task": "diagnose", "source_candidate_id": "legacy-7"}

    assert bulk._source_tokens(targeted_solve) & bulk._source_tokens(bulk_diagnose) == {
        ("candidate", "legacy-7")
    }


def test_supplemental_solve_seed_slots_are_unique_and_course_scoped():
    rows = bulk._supplemental_practice_slots("net", 200)

    assert len(rows) == len({row["candidate_id"] for row in rows}) == 200
    assert all(row["course"] == "net" for row in rows)


def test_accepted_unique_counts_training_messages_not_candidate_ids():
    def row(candidate_id, content, accepted=True):
        return {
            "candidate_id": candidate_id,
            "accepted": accepted,
            "training_sample": {"messages": [{"role": "assistant", "content": content}]},
        }

    rows = [row("a", "same"), row("b", "same"), row("c", "different"),
            row("rejected", "third", accepted=False)]
    selected = bulk._accepted_unique(rows)

    assert len(selected) == 2
    assert {item["training_sample"]["messages"][-1]["content"] for item in selected} == {
        "same", "different",
    }


def test_needs_quota_accounts_for_semantic_denylist(tmp_path, monkeypatch):
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    rows = [
        {"candidate_id": str(i), "accepted": True,
         "training_sample": {"messages": [{"role": "assistant", "content": str(i)}]}}
        for i in range(3)
    ]
    (candidates / "practice_co.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    rejected = tmp_path / "rejected.json"
    rejected.write_text('{"candidate_ids":["2"]}', encoding="utf-8")
    monkeypatch.setattr(bulk, "CANDIDATES_DIR", candidates)
    monkeypatch.setattr(bulk, "BLIND_REJECTED_IDS", rejected)

    assert bulk._needs_quota("practice_co", 2) is False
    assert bulk._needs_quota("practice_co", 3) is True


def test_repair_quota_uses_small_adaptive_batch(tmp_path, monkeypatch):
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    rejected = tmp_path / "rejected.json"
    rejected.write_text('{"candidate_ids":[]}', encoding="utf-8")
    monkeypatch.setattr(bulk, "CANDIDATES_DIR", candidates)
    monkeypatch.setattr(bulk, "BLIND_REJECTED_IDS", rejected)

    def accepted_row(candidate_id):
        return {"candidate_id": candidate_id, "accepted": True, "schema_ok": True,
                "calls": [], "reject_reasons": [],
                "training_sample": {"messages": [
                    {"role": "assistant", "content": candidate_id},
                ]}}

    existing = [accepted_row(f"old-{i}") for i in range(9)]
    (candidates / "repair.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in existing), encoding="utf-8")
    calls = []

    def generate(slot):
        calls.append(slot["candidate_id"])
        return accepted_row(slot["candidate_id"])

    bulk._run_to_quota("repair", [
        {"candidate_id": f"new-{i}"} for i in range(20)
    ], generate, quota=10, workers=2)

    assert len(calls) == 4


def test_reselection_preserves_still_valid_previous_samples():
    def row(candidate_id, content):
        return {"candidate_id": candidate_id,
                "training_sample": {"messages": [
                    {"role": "assistant", "content": content},
                ]}}

    accepted = [row("new-low-hash", "new"), row("old-a", "a"), row("old-b", "b")]
    seen = set()
    chosen = bulk._choose_preserving_previous(
        accepted, target=2, selected_hashes=seen, preferred_ids=["old-b", "old-a"])

    assert [item["candidate_id"] for item in chosen] == ["old-b", "old-a"]

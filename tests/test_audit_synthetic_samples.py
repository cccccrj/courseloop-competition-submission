from scripts.audit_synthetic_samples import _judge_model, _select
from scripts.generate_sft_pilot import KIMI, QWEN


def test_audit_uses_opposite_teacher_for_single_model_tasks():
    assert _judge_model({"intent": "practice"}) == KIMI
    assert _judge_model({"intent": "diagnose"}) == QWEN
    assert _judge_model({"intent": "grounded_qa"}) == QWEN


def test_audit_selection_is_stable_and_unique():
    rows = [{"candidate_id": str(i), "intent": "solve" if i % 2 else "diagnose",
             "course": "os" if i % 3 else "net"} for i in range(80)]
    one = _select(rows, 50)
    two = _select(list(reversed(rows)), 50)
    assert [row["candidate_id"] for row in one] == [row["candidate_id"] for row in two]
    assert len({row["candidate_id"] for row in one}) == 50


def test_audit_selection_excludes_prior_round_ids():
    rows = [{"candidate_id": str(i), "intent": "solve", "course": "os"}
            for i in range(10)]
    selected = _select(rows, 5, {"0", "1", "2"})

    assert len(selected) == 5
    assert {row["candidate_id"] for row in selected}.isdisjoint({"0", "1", "2"})

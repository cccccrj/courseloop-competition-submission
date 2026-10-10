from __future__ import annotations

import json

from backend.verification.unit_test_runner import run_python_tests
from scripts.run_targeted_synthetic_pilot import (
    TARGETS,
    _locked_evidence,
    _mutants,
    _schema_row,
)


def test_targeted_plan_has_exactly_200_candidates():
    assert TARGETS == {"diagnose": 60, "grounded_qa": 50, "solve": 50, "schema_regression": 40}
    assert sum(TARGETS.values()) == 200


def test_ast_mutations_are_provably_wrong():
    problem = {
        "code": "def add_one(x):\n    if x < 10:\n        return x + 1\n    return x\n",
        "func_name": "add_one",
        "test_cases": [
            {"args": [1], "expected": 2},
            {"args": [9], "expected": 10},
            {"args": [10], "expected": 10},
        ],
    }
    mutants = _mutants(problem, limit=3)
    assert mutants
    assert all(run_python_tests(code, "add_one", problem["test_cases"]).passed is False
               for code, _, _ in mutants)


def test_locked_evidence_is_exact_contiguous_substring():
    context = "第一段介绍进程。虚拟内存允许逻辑地址空间大于物理内存，并采用按需调页机制。最后说明页面置换。"
    evidence = _locked_evidence(context, "case-1")
    assert evidence in context
    assert len(evidence) >= 20


def test_schema_wrapper_drops_training_sample_and_only_checks_format():
    raw = {
        "candidate_id": "x", "task": "solve", "schema_ok": True, "accepted": False,
        "calls": [{"error": ""}], "training_sample": {"messages": []},
        "reject_reasons": ["teacher_disagreement_or_wrong"],
    }
    wrapped = _schema_row(raw, "solve")
    assert wrapped["task"] == "schema_regression"
    assert wrapped["accepted"] is True
    assert wrapped["training_sample"] is None
    assert json.dumps(wrapped)

from __future__ import annotations

import json

from scripts.teacher_bakeoff import _answer_contains_canonical, _wrong_token, score_result


def _result(raw: dict, model: str = "deepseek-v4-flash") -> dict:
    return {
        "requested_model": model,
        "raw_output": json.dumps(raw, ensure_ascii=False),
        "error": "",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "latency_seconds": 1.0,
    }


def test_wrong_token_is_rejected_by_numeric_checker():
    from backend.verification.numeric_checker import check_numeric

    canonicals = [
        {"kind": "choice", "value": "B"},
        {"kind": "int", "value": 4},
        {"kind": "percent", "value": 0.25},
        {"kind": "bits", "value": "1011", "base": 2},
        {"kind": "ipv4net", "network": "192.168.1.0", "prefix": 24},
    ]
    for canonical in canonicals:
        wrong, _ = _wrong_token(canonical)
        assert check_numeric(wrong, canonical).passed is False


def test_score_numeric_solve_passes_gold_answer():
    case = {
        "case_id": "x", "task": "solve", "course": "co",
        "expected": {"verification_type": "numeric", "canonical": {"kind": "int", "value": 4}},
    }
    raw = {
        "problem_type": "计算题", "knowledge_points": ["CPI"], "givens": ["x"],
        "target": "求值", "strategy": "计算", "steps": [], "final_answer": "4",
        "common_errors": [], "final_answer_canonical": {"kind": "int", "value": 4},
    }
    score = score_result(case, _result(raw))
    assert score["parsed_ok"] is True
    assert score["schema_ok"] is True
    assert score["task_pass"] is True


def test_score_diagnose_checks_first_step_and_tag():
    case = {
        "case_id": "d", "task": "diagnose", "course": "net",
        "expected": {"gold_first_wrong_step": 1, "gold_error_tag": "calculation"},
    }
    raw = {
        "correct": False, "score": 0, "first_wrong_step": 1,
        "error_tags": ["calculation"], "diagnosis": "算错", "remediation": "重算", "reexplain": "说明",
    }
    score = score_result(case, _result(raw))
    assert score["schema_ok"] is True
    assert score["diag_step_ok"] is True
    assert score["diag_tag_ok"] is True
    assert score["task_pass"] is True


def test_score_grounded_qa_requires_exact_evidence():
    context = "TCP 提供可靠传输，并通过流量控制避免发送方压垮接收方。"
    case = {
        "case_id": "q", "task": "grounded_qa", "course": "net",
        "expected": {"abstain": False, "source_id": "s1", "context": context},
    }
    raw = {
        "answer": "TCP 提供可靠传输，并具有流量控制。", "abstain": False,
        "citations": [{"source_id": "s1", "evidence": "TCP 提供可靠传输"}],
    }
    score = score_result(case, _result(raw))
    assert score["schema_ok"] is True
    assert score["task_pass"] is True


def test_practice_canonical_matches_final_number_not_first_intermediate_number():
    answer = "10ns 与 100ns 加权计算，最终约为 5015.0ns。"
    canonical = {"kind": "scalar", "value": 5015.0, "tol": 0.5, "unit": "ns"}
    assert _answer_contains_canonical(answer, canonical) is True


def test_mislabeled_hex_scalar_is_rejected_without_exception():
    assert _answer_contains_canonical("结果为 BA3F", {"kind": "scalar", "value": "BA3F"}) is False


def test_grounded_qa_allows_pdf_whitespace_changes_but_not_paraphrase():
    context = "运算器\n控制器\n存储器"
    case = {
        "case_id": "q2", "task": "grounded_qa", "course": "co",
        "expected": {"abstain": False, "source_id": "s1", "context": context},
    }
    base = {"answer": "包含运算器和控制器。", "abstain": False}
    exact = dict(base, citations=[{"source_id": "s1", "evidence": "运算器 控制器"}])
    paraphrase = dict(base, citations=[{"source_id": "s1", "evidence": "CPU 由运算和控制部件组成"}])
    assert score_result(case, _result(exact))["task_pass"] is True
    assert score_result(case, _result(paraphrase))["task_pass"] is False

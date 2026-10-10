import json

from backend.agents import error_diagnoser, practice_generator, solver


def test_diagnoser_uses_diagnose_task_route(monkeypatch):
    called = []
    payload = {
        "correct": False, "score": 0, "first_wrong_step": 1,
        "error_tags": ["calculation"], "diagnosis": "错因",
        "remediation": "修正", "reexplain": "重讲",
    }
    monkeypatch.setattr(error_diagnoser.llm_mod, "ask_diagnose",
                        lambda messages: called.append(messages) or json.dumps(payload))
    monkeypatch.setattr(error_diagnoser.telemetry, "log_llm_call", lambda *a, **k: None)

    result = error_diagnoser.diagnose("1+1", "3", reference="2", course="co")

    assert called and result["error_tags"] == ["calculation"]


def test_solver_uses_solve_task_route(monkeypatch):
    called = []
    payload = {
        "problem_type": "计算题", "knowledge_points": ["加法"], "givens": ["1+1"],
        "target": "求和", "strategy": "相加", "steps": [], "final_answer": "2",
        "common_errors": [], "final_answer_canonical": {"kind": "int", "value": 2},
    }
    monkeypatch.setattr(solver.llm_mod, "ask_solve",
                        lambda messages: called.append(messages) or json.dumps(payload))
    monkeypatch.setattr(solver.telemetry, "log_llm_call", lambda *a, **k: None)

    result = solver.solve("1+1", course="co")

    assert called and result["final_answer"] == "2"


def test_practice_generator_uses_practice_task_route(monkeypatch):
    called = []
    payload = {
        "stem": "1+1=?", "answer": "2", "difficulty": 1,
        "final_answer_canonical": {"kind": "int", "value": 2},
    }
    monkeypatch.setattr(practice_generator.llm_mod, "ask_practice",
                        lambda messages: called.append(messages) or json.dumps(payload))
    monkeypatch.setattr(practice_generator.telemetry, "log_llm_call", lambda *a, **k: None)

    result = practice_generator.generate("加法", course="co", max_retries=0)

    assert called and result["verified"] is True

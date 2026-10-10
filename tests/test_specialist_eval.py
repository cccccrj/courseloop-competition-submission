import json

import scripts.build_runtime_routes as runtime_routes
import scripts.evaluate_qa_probe_rescue as qa_gate
from scripts.prepare_specialist_eval import prepare


def test_prepare_specialist_eval_filters_by_gold_schema(tmp_path):
    source = tmp_path / "val.jsonl"
    out = tmp_path / "diagnose.jsonl"
    rows = [
        {"messages": [{"role": "assistant", "content": json.dumps({
            "correct": False, "first_wrong_step": 1, "error_tags": ["boundary"],
            "diagnosis": "x", "score": 0,
        })}]},
        {"messages": [{"role": "assistant", "content": json.dumps({
            "problem_type": "x", "steps": [], "final_answer": "x",
        })}]},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    report = prepare(source, out, "diagnose")

    assert report["n"] == 1
    assert len(out.read_text().splitlines()) == 1


def test_qa_gate_is_idempotent_and_requires_structural_quality(tmp_path, monkeypatch):
    gate = tmp_path / "gate.json"
    base_report = tmp_path / "base.json"
    rescue_report = tmp_path / "rescue.json"
    gate.write_text(json.dumps({
        "task_accuracy": 88.0, "adapter": "base-adapter",
    }), encoding="utf-8")
    base_report.write_text(json.dumps({
        "overall": {"task_accuracy": 88.0, "json_valid_rate": 98.0,
                    "keys_complete_rate": 98.0},
    }), encoding="utf-8")
    rescue_report.write_text(json.dumps({
        "overall": {"task_accuracy": 92.0, "json_valid_rate": 94.0,
                    "keys_complete_rate": 96.0},
    }), encoding="utf-8")
    monkeypatch.setattr(qa_gate, "GATE", gate)
    monkeypatch.setattr(qa_gate, "BASE_REPORT", base_report)
    monkeypatch.setattr(qa_gate, "RESCUE", rescue_report)
    monkeypatch.setattr(qa_gate, "PRODUCTION", tmp_path / "missing-production.json")
    monkeypatch.setattr(runtime_routes, "build", lambda _root: {})

    qa_gate.main()
    first = json.loads(gate.read_text(encoding="utf-8"))
    qa_gate.main()
    second = json.loads(gate.read_text(encoding="utf-8"))

    assert first["passed"] is True
    assert first["deployment_passed"] is False
    assert first["deployment_checks"]["production_prompts_refreshed"] is False
    assert first["deployment_checks"]["json_valid_95"] is False
    assert first["attempts"] == {"base_probe": 88.0, "rescue": 92.0}
    assert second["attempts"] == first["attempts"]

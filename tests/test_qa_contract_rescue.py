import json

import scripts.run_qa_contract_rescue as rescue


def test_qa_contract_rescue_requires_terminal_selection_and_status(tmp_path, monkeypatch):
    selection = tmp_path / "selection.json"
    status = tmp_path / "status.json"
    monkeypatch.setattr(rescue, "SELECTION", selection)
    monkeypatch.setattr(rescue, "OPTIMIZATION_STATUS", status)

    selection.write_text(json.dumps({
        "passed": False, "attempt": "primary", "reason": "primary_gate_failed",
    }), encoding="utf-8")
    status.write_text(json.dumps({"stage": "incremental_rescue_running"}), encoding="utf-8")
    assert rescue._final_selection_ready() is False

    selection.write_text(json.dumps({
        "passed": False,
        "reason": "primary_and_rescue_failed_keep_old_sft",
        "attempts": {"primary": {}, "rescue": {}},
    }), encoding="utf-8")
    status.write_text(json.dumps({"stage": "incremental_selection_complete"}), encoding="utf-8")
    assert rescue._final_selection_ready() is True


def test_qa_contract_rescue_writes_human_deployment_evidence(tmp_path, monkeypatch):
    report = tmp_path / "qa-rescue.md"
    monkeypatch.setattr(rescue, "FINAL_REPORT", report)

    rescue._write_human_report({
        "created_at": "2026-08-23 08:00:00",
        "result": "failed_reverted",
        "override_retained": False,
        "adapter": "model/qa",
        "grounded_qa_route": "courseloop-default",
        "task_accuracy": 90.0,
        "json_valid_rate": 94.0,
        "keys_complete_rate": 94.0,
        "production_report": "data/eval/report.json",
        "deployment_checks": {"json_valid_95": False, "target_accuracy_90": True},
    })

    text = report.read_text(encoding="utf-8")
    assert "failed_reverted" in text
    assert "[ ] `json_valid_95`" in text
    assert "[x] `target_accuracy_90`" in text

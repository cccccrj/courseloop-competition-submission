import json

import scripts.build_runtime_routes as route_builder
import scripts.compare_incremental_sft as comparison
import scripts.run_15h_optimization as pipeline
from scripts.run_15h_optimization import (
    _finalize_existing_selection,
    _adapter_complete,
    _selection_is_final,
    _selection_is_primary_intermediate,
)


def _write(path, **payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_primary_failure_is_intermediate_until_rescue_finishes(tmp_path):
    report = tmp_path / "selection.json"
    _write(report, passed=False, attempt="primary", reason="primary_gate_failed")
    assert _selection_is_final(report) is False
    assert _selection_is_primary_intermediate(report) is True


def test_pass_or_terminal_fallback_is_final(tmp_path):
    report = tmp_path / "selection.json"
    _write(report, passed=True, attempt="primary", reason="primary_gate_passed")
    assert _selection_is_final(report) is True
    assert _selection_is_primary_intermediate(report) is False

    _write(report, passed=False, reason="primary_and_rescue_failed_keep_old_sft",
           attempts={"primary": {}, "rescue": {}})
    assert _selection_is_final(report) is True


def test_missing_or_corrupt_selection_is_not_final(tmp_path):
    report = tmp_path / "selection.json"
    assert _selection_is_final(report) is False
    assert _selection_is_primary_intermediate(report) is False
    report.write_text("not-json", encoding="utf-8")
    assert _selection_is_final(report) is False
    assert _selection_is_primary_intermediate(report) is False


def test_complete_primary_adapter_is_a_durable_posttrain_resume_point(tmp_path):
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    assert _adapter_complete(adapter) is False
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    (adapter / "adapter_model.safetensors").write_bytes(b"weights")
    assert _adapter_complete(adapter) is True


def test_terminal_recovery_rebuilds_routes_and_human_report_before_status(
        tmp_path, monkeypatch):
    selection = tmp_path / "selection.json"
    routes = tmp_path / "runtime_routes.json"
    final_report = tmp_path / "final.md"
    status = tmp_path / "status.json"
    _write(selection, passed=True, selected_adapter="model/new",
           reason="primary_gate_passed")
    monkeypatch.setattr(pipeline, "RUNTIME_ROUTES", routes)
    monkeypatch.setattr(pipeline, "FINAL_REPORT", final_report)
    monkeypatch.setattr(pipeline, "STATUS", status)
    monkeypatch.setattr(
        route_builder, "build",
        lambda root: routes.write_text("{}", encoding="utf-8"),
    )
    monkeypatch.setattr(
        comparison, "_write_final_report",
        lambda result: final_report.write_text(result["reason"], encoding="utf-8"),
    )

    result = _finalize_existing_selection(selection)

    saved = json.loads(status.read_text(encoding="utf-8"))
    assert result["selected_adapter"] == "model/new"
    assert routes.exists() and final_report.exists()
    assert saved["stage"] == "incremental_selection_complete"
    assert saved["runtime_routes"] == str(routes)

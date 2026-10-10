import json
from pathlib import Path

import scripts.build_runtime_routes as routes


def _adapter(root: Path, relative: str):
    path = root / relative
    path.mkdir(parents=True)
    (path / "adapter_config.json").write_text("{}")
    (path / "adapter_model.safetensors").write_bytes(b"ok")


def test_runtime_routes_only_select_specialists_that_pass(tmp_path):
    _adapter(tmp_path, "model/loras/courseloop-408")
    _adapter(tmp_path, "model/loras/unified")
    _adapter(tmp_path, "model/loras/diag")
    _adapter(tmp_path, "model/loras/qa")
    (tmp_path / "data/eval").mkdir(parents=True)
    (tmp_path / "data/diagnose_probe").mkdir(parents=True)
    (tmp_path / "data/qa_probe").mkdir(parents=True)
    (tmp_path / "data/eval/synth3000_selection.json").write_text(json.dumps({
        "selected_adapter": "model/loras/unified", "reason": "passed"}))
    (tmp_path / "data/diagnose_probe/gate.json").write_text(json.dumps({
        "passed": True, "adapter": "model/loras/diag"}))
    (tmp_path / "data/qa_probe/gate.json").write_text(json.dumps({
        "passed": False, "adapter": "model/loras/qa"}))

    result = routes.build(tmp_path)

    assert result["routes"]["diagnose"] == "courseloop-diagnose"
    assert result["routes"]["grounded_qa"] == "courseloop-default"
    assert result["adapters"]["courseloop-default"].endswith("model/loras/unified")
    assert result["adapters"]["courseloop-diagnose"].endswith("model/loras/diag")


def test_diagnose_fallback_only_applies_when_unified_model_is_not_selected(tmp_path):
    for adapter in ("model/loras/courseloop-408", "model/loras/unified", "model/loras/fallback"):
        _adapter(tmp_path, adapter)
    (tmp_path / "data/eval").mkdir(parents=True)
    (tmp_path / "data/diagnose_probe").mkdir(parents=True)
    (tmp_path / "data/qa_probe").mkdir(parents=True)
    gate = {"passed": False, "deployment_passed": False, "fallback_passed": True,
            "fallback_adapter": "model/loras/fallback"}
    (tmp_path / "data/diagnose_probe/gate.json").write_text(json.dumps(gate))

    old_result = routes.build(tmp_path)
    assert old_result["routes"]["diagnose"] == "courseloop-diagnose"
    assert old_result["adapters"]["courseloop-diagnose"].endswith("model/loras/fallback")

    (tmp_path / "data/eval/synth3000_selection.json").write_text(json.dumps({
        "selected_adapter": "model/loras/unified", "reason": "primary_gate_passed"}))
    unified_result = routes.build(tmp_path)
    assert unified_result["routes"]["diagnose"] == "courseloop-default"

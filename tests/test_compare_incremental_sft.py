import json

import scripts.compare_incremental_sft as comparison
from scripts.compare_incremental_sft import (
    _deployed_semantic_groups,
    _qa_deployment_checks,
    _replace_intent,
    _standalone_report_checks,
    _weighted_diagnose_metrics,
    _weighted_metrics,
    _weighted_structure_metrics,
)


def _m(n, json_rate, keys_rate, task_rate):
    return {
        "n": n,
        "json_valid_rate": json_rate,
        "keys_complete_rate": keys_rate,
        "task_accuracy": task_rate,
    }


def test_deployed_metrics_replace_default_qa_with_passed_specialist():
    default = {
        "ds-algo/diagnose": _m(60, 100, 100, 80),
        "os/solve": _m(40, 100, 100, 95),
        "co/grounded_qa": _m(50, 10, 10, 10),
    }
    specialist = {"co/grounded_qa": _m(50, 96, 96, 92)}

    deployed = _deployed_semantic_groups(default, specialist)
    overall = _weighted_metrics(deployed)

    assert deployed["co/grounded_qa"] == specialist["co/grounded_qa"]
    assert overall == {
        "n": 150,
        "json_valid_rate": 98.67,
        "keys_complete_rate": 98.67,
        "task_accuracy": 88.0,
    }


def test_deployed_metrics_keep_default_qa_without_specialist():
    default = {
        "os/solve": _m(1, 100, 100, 100),
        "os/grounded_qa": _m(1, 50, 50, 50),
    }
    assert _deployed_semantic_groups(default, None) == default
    assert _weighted_metrics(default)["task_accuracy"] == 75.0


def test_qa_deployment_requires_exact_production_prompt_evidence(monkeypatch):
    expected_signature = "expected-production-signature"
    monkeypatch.setattr(comparison, "_semantic_signature", lambda *args, **kwargs: expected_signature)
    report = {
        "adapter": comparison.QA_SPECIALIST_ADAPTER,
        "data": str(comparison.QA_EVAL_DATA),
        "n": 50,
        "run_signature": expected_signature,
        "cache_version": comparison.SEMANTIC_CACHE_VERSION,
        "production_prompt_contract_fingerprint": comparison.semantic_prompt_fingerprint(),
        "production_prompts_refreshed": False,
        "overall": {"task_accuracy": 92.0, "json_valid_rate": 96.0,
                    "keys_complete_rate": 96.0},
    }
    assert _qa_deployment_checks(report) == {
        "production_prompts_refreshed": False,
        "adapter_matches": True,
        "frozen_eval_data_matches": True,
        "frozen_qa_n_50": True,
        "run_signature_matches_current": True,
        "cache_signature_current": True,
        "production_prompt_contract_matches": True,
        "target_accuracy_90": True,
        "json_valid_95": True,
        "keys_complete_95": True,
    }
    report["production_prompts_refreshed"] = True
    assert all(_qa_deployment_checks(report).values())


def test_qa_deployment_rejects_wrong_adapter_data_or_population(monkeypatch):
    monkeypatch.setattr(comparison, "_semantic_signature", lambda *args, **kwargs: "expected")
    report = {
        "adapter": "model/loras/wrong",
        "data": "data/qa_probe/wrong.jsonl",
        "n": 49,
        "run_signature": "sha256",
        "production_prompts_refreshed": True,
        "overall": {"task_accuracy": 100.0, "json_valid_rate": 100.0,
                    "keys_complete_rate": 100.0},
    }
    checks = _qa_deployment_checks(report)
    assert checks["adapter_matches"] is False
    assert checks["frozen_eval_data_matches"] is False
    assert checks["frozen_qa_n_50"] is False


def test_standalone_old_val_report_requires_adapter_data_and_signature(monkeypatch):
    monkeypatch.setattr(comparison, "_standalone_signature", lambda *args, **kwargs: "expected")
    monkeypatch.setattr(comparison, "standalone_prompt_fingerprint", lambda: "prompt")
    report = {
        "adapter": comparison.PRIMARY_ADAPTER,
        "val_path": str(comparison.HERE / "data/sft/val.jsonl"),
        "n_total": 467,
        "production_prompts_refreshed": True,
        "run_signature": "expected",
        "cache_version": comparison.STANDALONE_CACHE_VERSION,
        "production_prompt_contract_fingerprint": "prompt",
    }
    assert all(_standalone_report_checks(report, comparison.PRIMARY_ADAPTER).values())
    report["adapter"] = comparison.OLD_ADAPTER
    assert _standalone_report_checks(
        report, comparison.PRIMARY_ADAPTER
    )["adapter_matches"] is False


def test_weighted_diagnose_metrics_uses_only_diagnose_groups():
    report = {"by_intent": {
        "co/diagnose": {"n": 1, "json_valid_rate": 100, "keys_complete_rate": 100,
                         "first_wrong_step_acc": 100, "error_tag_consistency": 80},
        "os/diagnose": {"n": 3, "json_valid_rate": 100, "keys_complete_rate": 100,
                         "first_wrong_step_acc": 60, "error_tag_consistency": 40},
        "os/solve": {"n": 99, "json_valid_rate": 0, "keys_complete_rate": 0},
    }}
    assert _weighted_diagnose_metrics(report) == {
        "n": 4, "json_valid_rate": 100.0, "keys_complete_rate": 100.0,
        "first_wrong_step_acc": 70.0, "error_tag_consistency": 50.0,
    }


def test_structure_baseline_can_merge_current_diagnose_with_historical_other_groups():
    assert _weighted_structure_metrics({
        "os/solve": {"n": 2, "json_valid_rate": 50, "keys_complete_rate": 50},
        "os/diagnose": {"n": 1, "json_valid_rate": 100, "keys_complete_rate": 100},
    }) == {"n": 3, "json_valid_rate": 66.67, "keys_complete_rate": 66.67}


def test_stale_old_diagnose_baseline_is_refreshed(tmp_path, monkeypatch):
    stale = {"marker": "stale"}
    fresh = {"marker": "fresh"}
    report_path = tmp_path / "report_lora_diagnose-old-prodprompt-regression.json"
    report_path.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(comparison, "EVAL", tmp_path)
    check_results = iter([{"signature": False}, {"signature": True}])
    monkeypatch.setattr(
        comparison, "_standalone_report_checks",
        lambda *args, **kwargs: next(check_results),
    )
    commands = []

    def fake_run(command):
        commands.append(command)
        report_path.write_text(json.dumps(fresh), encoding="utf-8")

    monkeypatch.setattr(comparison, "_run", fake_run)
    assert comparison._ensure_old_diagnose_production_report() == fresh
    assert "--intent" in commands[0]
    assert "diagnose" in commands[0]


def test_stale_full_old_baseline_is_refreshed_under_production_contract(
        tmp_path, monkeypatch):
    stale = {"marker": "historical-768"}
    fresh = {"marker": "current-task-limits"}
    report_path = tmp_path / "report_lora_408-old-prodprompt-regression.json"
    report_path.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(comparison, "EVAL", tmp_path)
    check_results = iter([{"signature": False}, {"signature": True}])
    monkeypatch.setattr(
        comparison, "_standalone_report_checks",
        lambda *args, **kwargs: next(check_results),
    )
    commands = []

    def fake_run(command):
        commands.append(command)
        report_path.write_text(json.dumps(fresh), encoding="utf-8")

    monkeypatch.setattr(comparison, "_run", fake_run)
    assert comparison._ensure_old_production_report() == fresh
    assert "--refresh-production-prompts" in commands[0]
    assert "--intent" not in commands[0]
    assert comparison.OLD_ADAPTER in commands[0]


def test_verified_diagnosis_fallback_raises_only_the_baseline_route():
    old = {
        "ds-algo/diagnose": _m(60, 100, 100, 36.67),
        "os/solve": _m(40, 100, 100, 90),
    }
    fallback = {"ds-algo/diagnose": _m(60, 100, 100, 73.33)}
    deployed = _replace_intent(old, "diagnose", fallback)

    assert deployed["ds-algo/diagnose"]["task_accuracy"] == 73.33
    assert deployed["os/solve"] == old["os/solve"]
    # Candidate groups are intentionally not passed through this helper by the
    # selector: a unified model must earn the right to replace the fallback.
    assert old["ds-algo/diagnose"]["task_accuracy"] == 36.67


def test_final_selection_updates_optimization_status(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"stage": "gpu_jobs_queued", "kept": 1}))
    monkeypatch.setattr(comparison, "OPTIMIZATION_STATUS", status)

    comparison._write_optimization_status("incremental_selection_complete", {
        "selected_adapter": "model/loras/new",
        "reason": "primary_gate_passed",
    })

    saved = json.loads(status.read_text())
    assert saved["stage"] == "incremental_selection_complete"
    assert saved["selected_adapter"] == "model/loras/new"
    assert saved["selection_reason"] == "primary_gate_passed"
    assert saved["kept"] == 1


def test_final_report_records_selection_metrics_and_failed_gates(tmp_path, monkeypatch):
    report = tmp_path / "final.md"
    monkeypatch.setattr(comparison, "FINAL_REPORT", report)

    comparison._write_final_report({
        "created_at": "2026-08-22 22:00:00",
        "attempt": "primary",
        "passed": False,
        "selected_adapter": "model/loras/courseloop-408",
        "reason": "primary_gate_failed",
        "checks": {"semantic_absolute_90": False, "old_json_drop_le_1pp": True},
        "old_sft_targeted_semantic": {
            "n": 150, "json_valid_rate": 100.0,
            "keys_complete_rate": 98.0, "task_accuracy": 91.0,
        },
        "candidate_targeted_semantic": {
            "n": 150, "json_valid_rate": 100.0,
            "keys_complete_rate": 99.0, "task_accuracy": 89.0,
        },
    })

    text = report.read_text()
    assert "model/loras/courseloop-408" in text
    assert "任务准确 89.0%" in text
    assert "`semantic_absolute_90`" in text
    assert "未通过，保留已验证旧模型" in text

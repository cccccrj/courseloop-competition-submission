from scripts.evaluate_diagnose_fallback import fallback_decision


def _regression(step=80.0, tags=90.0):
    return {
        "production_prompts_refreshed": True,
        "by_intent": {"ds-algo/diagnose": {
            "n": 153,
            "json_valid_rate": 100.0,
            "keys_complete_rate": 100.0,
            "first_wrong_step_acc": step,
            "error_tag_consistency": tags,
        }},
    }


def test_diagnose_fallback_requires_gain_and_no_regression():
    targeted = {"overall": {
        "task_accuracy": 73.33, "json_valid_rate": 100.0, "keys_complete_rate": 100.0}}
    baseline = {"by_group": {"ds-algo/diagnose": {"task_accuracy": 36.67}}}

    assert fallback_decision(targeted, baseline, _regression(), _regression(79, 89))["passed"]
    failed = fallback_decision(targeted, baseline, _regression(), _regression(77, 87))
    assert failed["passed"] is False
    assert failed["checks"]["old_step_drop_le_2pp"] is False
    assert failed["checks"]["old_tag_drop_le_2pp"] is False

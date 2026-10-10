import json

from scripts.prepare_rescue_sft import prepare


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _row(candidate_id, intent):
    return {
        "candidate_id": candidate_id,
        "intent": intent,
        "messages": [{"role": "user", "content": candidate_id},
                     {"role": "assistant", "content": "{}"}],
    }


def test_rescue_oversamples_only_weak_intent_and_keeps_eval_content_out(tmp_path):
    data = tmp_path / "incremental"
    out = tmp_path / "rescue"
    data.mkdir()
    new = [_row("d1", "diagnose"), _row("q1", "grounded_qa"), _row("s1", "solve")]
    replay = [_row("old1", "practice")]
    _write(data / "new_train.jsonl", new)
    _write(data / "old_replay.jsonl", replay)
    _write(data / "train_with_replay.jsonl", new + replay)
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({
        "passed": False,
        "checks": {
            "old_json_drop_le_1pp": True,
            "old_keys_drop_le_1pp": True,
            "old_diagnose_step_drop_le_2pp": True,
            "old_diagnose_tag_drop_le_2pp": True,
            "targeted_json_absolute_95": True,
            "targeted_keys_absolute_95": True,
            "semantic_absolute_90": False,
        },
        "targeted_semantic_by_group": {
            "candidate": {
                "co/grounded_qa": {"task_accuracy": 72.0},
                "os/solve": {"task_accuracy": 96.0},
            }
        },
    }), encoding="utf-8")

    report = prepare(data, selection, out)

    assert report["weak_intents"] == ["grounded_qa"]
    assert report["targeted_eval_content_used"] is False
    assert report["original_train"] == 4
    # one weak QA + one replay guard
    assert report["extra_rows"] == 2
    assert len((out / "train.jsonl").read_text().splitlines()) == 6


def test_rescue_adds_replay_when_old_validation_regresses(tmp_path):
    data = tmp_path / "incremental"
    out = tmp_path / "rescue"
    data.mkdir()
    new = [_row("s1", "solve")]
    replay = [_row("old1", "practice")]
    _write(data / "new_train.jsonl", new)
    _write(data / "old_replay.jsonl", replay)
    taxonomy = [{**_row("old-diag", "diagnose"), "taxonomy_replay_old_sft": True}]
    _write(data / "old_diagnose_taxonomy_replay.jsonl", taxonomy)
    _write(data / "train_with_replay.jsonl", new + replay)
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({
        "passed": False,
        "checks": {
            "old_json_drop_le_1pp": False,
            "old_keys_drop_le_1pp": True,
            "old_diagnose_step_drop_le_2pp": False,
            "old_diagnose_tag_drop_le_2pp": True,
            "targeted_json_absolute_95": True,
            "targeted_keys_absolute_95": True,
            "semantic_absolute_90": True,
        },
        "targeted_semantic_by_group": {
            "candidate": {"os/solve": {"task_accuracy": 96.0}}
        },
    }), encoding="utf-8")

    report = prepare(data, selection, out)

    assert report["old_regression"] is True
    assert report["weak_intents"] == []
    assert report["extra_rows"] == 2
    assert report["diagnose_taxonomy_replay_available"] == 1


def test_structural_rescue_does_not_oversample_healthy_specialist_route(tmp_path):
    data = tmp_path / "incremental"
    out = tmp_path / "rescue"
    data.mkdir()
    new = [_row("d1", "diagnose"), _row("q1", "grounded_qa")]
    replay = [_row("old1", "practice")]
    _write(data / "new_train.jsonl", new)
    _write(data / "old_replay.jsonl", replay)
    _write(data / "train_with_replay.jsonl", new + replay)
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({
        "passed": False,
        "checks": {
            "old_json_drop_le_1pp": True,
            "old_keys_drop_le_1pp": True,
            "old_diagnose_step_drop_le_2pp": True,
            "old_diagnose_tag_drop_le_2pp": True,
            "targeted_json_absolute_95": False,
            "targeted_keys_absolute_95": False,
            "semantic_absolute_90": True,
        },
        "targeted_semantic_by_group": {
            "candidate": {
                "ds-algo/diagnose": {
                    "task_accuracy": 92.0,
                    "json_valid_rate": 90.0,
                    "keys_complete_rate": 90.0,
                },
                "co/grounded_qa": {
                    "task_accuracy": 92.0,
                    "json_valid_rate": 96.0,
                    "keys_complete_rate": 96.0,
                },
            }
        },
    }), encoding="utf-8")

    report = prepare(data, selection, out)

    assert report["weak_intents"] == ["diagnose"]
    assert report["extra_rows"] == 2  # one diagnosis + replay guard


def test_rescue_targets_relative_group_drop_even_above_absolute_floor(tmp_path):
    data = tmp_path / "incremental"
    out = tmp_path / "rescue"
    data.mkdir()
    new = [_row("d1", "diagnose"), _row("s1", "solve")]
    replay = [_row("old1", "practice")]
    _write(data / "new_train.jsonl", new)
    _write(data / "old_replay.jsonl", replay)
    _write(data / "train_with_replay.jsonl", new + replay)
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({
        "passed": False,
        "checks": {
            "old_json_drop_le_1pp": True,
            "old_keys_drop_le_1pp": True,
            "old_diagnose_step_drop_le_2pp": True,
            "old_diagnose_tag_drop_le_2pp": True,
            "targeted_json_absolute_95": True,
            "targeted_keys_absolute_95": True,
            "targeted_json_not_lower": True,
            "targeted_keys_gain_or_ceiling": True,
            "semantic_absolute_90": True,
            "semantic_gain_or_ceiling": True,
            "semantic_no_group_drop_gt_2pp": False,
        },
        "targeted_semantic_by_group": {
            "baseline": {
                "ds-algo/diagnose": {"task_accuracy": 96.0},
                "os/solve": {"task_accuracy": 92.0},
            },
            "candidate": {
                "ds-algo/diagnose": {"task_accuracy": 92.0},
                "os/solve": {"task_accuracy": 94.0},
            },
        },
    }), encoding="utf-8")

    report = prepare(data, selection, out)

    assert report["weak_intents"] == ["diagnose"]
    assert report["extra_rows"] == 2  # diagnosis + replay guard

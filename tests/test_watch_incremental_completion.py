import json

import scripts.watch_incremental_completion as watcher


def test_watchdog_finishes_only_when_selection_and_final_status_exist(tmp_path, monkeypatch):
    selection = tmp_path / "selection.json"
    status = tmp_path / "status.json"
    selection.write_text(json.dumps({
        "selected_adapter": "model/adapter", "reason": "primary_gate_passed",
    }), encoding="utf-8")
    status.write_text(json.dumps({"stage": "incremental_selection_complete"}),
                      encoding="utf-8")
    monkeypatch.setattr(watcher, "SELECTION", selection)
    monkeypatch.setattr(watcher, "STATUS", status)
    monkeypatch.setattr(watcher, "WATCH_STATE", tmp_path / "watch.json")
    monkeypatch.setattr(watcher, "LOCK", tmp_path / "watch.lock")

    watcher.main(timeout_hours=0.01, max_retries=0)

    state = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))
    assert state["stage"] == "complete"
    assert state["selected_adapter"] == "model/adapter"


def test_primary_selection_is_not_final_while_rescue_runs(tmp_path, monkeypatch):
    selection = tmp_path / "selection.json"
    selection.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watcher, "SELECTION", selection)

    assert watcher._selection_is_final({"stage": "incremental_rescue_running"}) is False
    assert watcher._selection_is_final({"stage": "incremental_selection_complete"}) is True


def test_active_job_detection_checks_pending_and_running(tmp_path, monkeypatch):
    (tmp_path / "pending").mkdir()
    (tmp_path / "running").mkdir()
    monkeypatch.setattr(watcher, "QUEUE", tmp_path)
    assert watcher._job_active() is False
    (tmp_path / "pending/0001__courseloop-synth3000-incremental-sft_.job").write_text(
        "{}", encoding="utf-8")
    assert watcher._job_active() is True

import json

from scripts.repair_bulk_blind_audit import repair


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_repair_quarantines_rejected_and_excludes_whole_round(tmp_path):
    rows = [
        {"candidate_id": "ok", "accepted": True},
        {"candidate_id": "bad", "accepted": False, "severe_error": True},
    ]
    (tmp_path / "blind_audit.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    _write_json(tmp_path / "blind_audit_report.json", {
        "passed": False, "created_at": "now", "accept_rate": 50, "severe_errors": 1,
    })

    result = repair(tmp_path)

    assert result["newly_rejected"] == 1
    assert json.loads((tmp_path / "blind_audit_rejected_ids.json").read_text())["candidate_ids"] == ["bad"]
    assert set(json.loads((tmp_path / "blind_audit_excluded_ids.json").read_text())["candidate_ids"]) == {"ok", "bad"}
    assert (tmp_path / "blind_audit_round_01.jsonl").exists()
    assert not (tmp_path / "blind_audit.jsonl").exists()

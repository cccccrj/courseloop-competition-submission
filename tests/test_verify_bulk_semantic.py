import json

import scripts.verify_bulk_semantic as verifier


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_semantic_verifier_is_resumable_and_blacklists_rejections(tmp_path, monkeypatch):
    rows = [
        {"candidate_id": f"d-{i}", "intent": "diagnose", "messages": []}
        for i in range(900)
    ] + [
        {"candidate_id": f"q-{i}", "intent": "grounded_qa", "messages": []}
        for i in range(900)
    ] + [
        {"candidate_id": f"p-{i}", "intent": "practice", "messages": []}
        for i in range(300)
    ] + [
        {"candidate_id": f"s-{i}", "intent": "solve", "messages": []}
        for i in range(900)
    ]
    input_path = tmp_path / "accepted.jsonl"
    state = tmp_path / "state.jsonl"
    report = tmp_path / "report.json"
    rejected = tmp_path / "rejected.json"
    _write(input_path, rows)
    _write(state, [{"candidate_id": "d-0", "accepted": True, "calls": [{"error": ""}]}])

    def fake_audit(row):
        return {"candidate_id": row["candidate_id"],
                "accepted": row["candidate_id"] != "q-899", "calls": [{"error": ""}]}

    monkeypatch.setattr(verifier, "_audit", fake_audit)
    result = verifier.run(input_path, state, report, rejected, workers=8)

    assert result["cached_before_run"] == 1
    assert result["newly_verified"] == 2999
    assert result["rejected_current"] == 1
    assert result["passed"] is False
    assert json.loads(rejected.read_text())["candidate_ids"] == ["q-899"]

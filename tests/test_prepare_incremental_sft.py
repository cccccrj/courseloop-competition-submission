import json

import pytest

import scripts.prepare_incremental_sft as prep
from scripts.prepare_incremental_sft import _source_group, _source_keys, _stratified_split


def test_incremental_split_is_exact_stable_and_disjoint():
    rows = [{"candidate_id": str(i), "course": ["co", "ds-algo", "net", "os"][i % 4],
             "intent": ["practice", "diagnose", "grounded_qa", "solve"][i % 4],
             "messages": [{"role": "assistant", "content": str(i)}]} for i in range(200)]
    train, val = _stratified_split(rows, 20)
    assert len(train) == 180
    assert len(val) == 20
    assert {row["candidate_id"] for row in train}.isdisjoint({row["candidate_id"] for row in val})
    assert [row["candidate_id"] for row in val] == [row["candidate_id"] for row in _stratified_split(rows, 20)[1]]


def test_incremental_split_keeps_source_variants_together():
    rows = []
    for source in range(40):
        for variant in range(3):
            rows.append({
                "candidate_id": f"qa-{source}-{variant}", "source_id": f"chunk-{source}",
                "course": ["co", "net"][source % 2], "intent": "grounded_qa",
                "messages": [{"role": "assistant", "content": f"{source}-{variant}"}],
            })
    train, val = _stratified_split(rows, 30)

    assert len(val) == 30
    assert {_source_group(row) for row in train}.isdisjoint(
        {_source_group(row) for row in val})


def test_source_keys_detect_topic_leak_even_when_messages_differ():
    targeted = {"candidate_id": "tdiag-1", "intent": "diagnose", "source_candidate_id": "seed-7"}
    bulk = {"candidate_id": "bdiag-9", "intent": "diagnose", "source_candidate_id": "seed-7"}

    assert _source_keys(targeted) & _source_keys(bulk) == {("source_candidate_id", "seed-7")}


def test_failed_diagnose_probe_reweights_only_training_split(tmp_path, monkeypatch):
    monkeypatch.setattr(prep, "HERE", tmp_path)
    (tmp_path / "data/sft").mkdir(parents=True)
    (tmp_path / "data/synthetic_targeted_200").mkdir(parents=True)
    (tmp_path / "data/diagnose_probe").mkdir(parents=True)

    intents = ["practice", "diagnose", "grounded_qa", "solve"]
    bulk = []
    for i in range(3000):
        bulk.append({
            "candidate_id": f"bulk-{i}", "course": "co", "intent": intents[i % 4],
            "messages": [{"role": "user", "content": str(i)},
                         {"role": "assistant", "content": "{}"}],
        })
    old = [{"messages": [{"role": "user", "content": f"old-{i}"},
                          {"role": "assistant", "content": "{}"}]}
           for i in range(600)]
    old_val = [{"messages": [{"role": "user", "content": "held-out"},
                              {"role": "assistant", "content": "{}"}]}]
    targeted = [{"candidate_id": "target-1", "course": "co", "intent": "diagnose",
                 "source_candidate_id": "target-source",
                 "messages": [{"role": "user", "content": "target"},
                              {"role": "assistant", "content": "{}"}]}]

    def write(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    bulk_path = tmp_path / "bulk.jsonl"
    write(bulk_path, bulk)
    write(tmp_path / "data/sft/train.jsonl", old)
    write(tmp_path / "data/sft/val.jsonl", old_val)
    write(tmp_path / "data/synthetic_targeted_200/accepted.jsonl", targeted)
    (tmp_path / "data/diagnose_probe/gate.json").write_text(
        json.dumps({"passed": False, "task_accuracy": 50.0,
                    "oversample_course": "co", "oversample_repeats": 2}), encoding="utf-8")
    bulk_report = tmp_path / "bulk_report.json"
    audit_report = tmp_path / "audit_report.json"
    semantic_report = tmp_path / "semantic_report.json"
    bulk_report.write_text('{"passed": true}', encoding="utf-8")
    audit_report.write_text('{"passed": true}', encoding="utf-8")
    semantic_report.write_text(json.dumps({
        "passed": True, "selected_target": 3000,
        "verified_accepted": 3000, "rejected_current": 0,
    }), encoding="utf-8")

    report = prep.prepare(bulk_path, tmp_path / "out", 150, 600, 900,
                          bulk_report, audit_report, semantic_report)
    new_train = prep._read(tmp_path / "out/new_train.jsonl")
    expected = sum(row["intent"] == "diagnose" for row in new_train)

    assert report["probe_oversample_by_intent"]["diagnose"] == expected * 2
    assert report["probe_oversample_by_intent"]["grounded_qa"] == 0
    assert report["train_with_replay"] == 2850 + 600 + expected * 2
    assert report["targeted_source_overlap"] == 0
    assert report["diagnose_prompts_refreshed"] == 750
    assert report["semantic_verification"]["verified_accepted"] == 3000
    refreshed = [row for row in prep._read(tmp_path / "out/new_train.jsonl")
                 if row["intent"] == "diagnose"]
    assert all(row["messages"][0]["role"] == "system" for row in refreshed)
    assert all("标签判定优先级" in row["messages"][0]["content"]
               for row in refreshed)


def test_diagnose_taxonomy_replay_covers_rare_tags_and_excludes_general_replay():
    rows = []
    for course in ["co", "ds-algo", "net", "os"]:
        for tag, count in [("concept_confuse", 8), ("misread", 2), ("careless", 1)]:
            for i in range(count):
                gold = {"correct": False, "score": 0, "first_wrong_step": 1,
                        "error_tags": [tag], "diagnosis": "x"}
                rows.append({
                    "course": course,
                    "messages": [{"role": "user", "content": f"{course}-{tag}-{i}"},
                                 {"role": "assistant", "content": json.dumps(gold)}],
                })
    # Diagnosis replay refreshes the production system prompt before de-duplication.
    excluded_row = prep._refresh_diagnose_prompt(rows[0], "diagnose")
    excluded = {prep._message_hash(excluded_row)}
    sampled = prep._diagnose_taxonomy_replay(rows, 20, excluded)

    assert len(sampled) == 20
    assert prep._message_hash(excluded_row) not in {prep._message_hash(row) for row in sampled}
    tags = {tag for row in sampled
            for tag in json.loads(row["messages"][-1]["content"])["error_tags"]}
    assert tags == {"concept_confuse", "misread", "careless"}
    assert all(row["taxonomy_replay_old_sft"] for row in sampled)


def test_live_prepare_waits_until_specialist_queue_is_settled(tmp_path, monkeypatch):
    status = tmp_path / "project/data/optimization_15h_status.json"
    status.parent.mkdir(parents=True)
    status.write_text('{"stage":"preparing_incremental_sft"}', encoding="utf-8")
    pending = tmp_path / "queue/pending"
    pending.mkdir(parents=True)
    job = pending / "000096__courseloop-diagnose-taxonomy-rescue_.job"
    job.write_text("job", encoding="utf-8")
    calls = []

    def settle(_seconds):
        calls.append(True)
        job.unlink()

    monkeypatch.setattr(prep, "HERE", tmp_path / "project")
    monkeypatch.setattr(prep, "GPU_QUEUE", tmp_path / "queue")
    monkeypatch.setattr(prep.time, "sleep", settle)

    prep._wait_for_live_specialists(timeout_seconds=5)

    assert calls == [True]


def test_prepare_refuses_partial_semantic_verification(tmp_path):
    bulk_report = tmp_path / "bulk_report.json"
    audit_report = tmp_path / "audit_report.json"
    semantic_report = tmp_path / "semantic_report.json"
    bulk_report.write_text('{"passed": true}', encoding="utf-8")
    audit_report.write_text('{"passed": true}', encoding="utf-8")
    semantic_report.write_text(json.dumps({
        "passed": True, "selected_target": 1800,
        "verified_accepted": 1800, "rejected_current": 0,
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="3,000 条全量语义验收尚未通过"):
        prep.prepare(tmp_path / "missing.jsonl", tmp_path / "out", 150, 600, 900,
                     bulk_report, audit_report, semantic_report)

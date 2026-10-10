import json

import scripts.prepare_diagnose_rescue_probe as rescue
import scripts.prepare_diagnose_taxonomy_rescue as taxonomy_rescue
import scripts.prepare_qa_rescue_probe as qa_rescue


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_diagnose_rescue_focuses_ds_and_keeps_target_sources_disjoint(tmp_path, monkeypatch):
    source = tmp_path / "train.jsonl"
    target = tmp_path / "target.jsonl"
    out = tmp_path / "out"
    ds = [
        {"candidate_id": f"ds-{i}", "source_candidate_id": f"train-source-{i}",
         "course": "ds-algo", "intent": "diagnose", "messages": []}
        for i in range(225)
    ]
    replay = [
        {"candidate_id": f"old-{i}", "replay_old_sft": True, "messages": []}
        for i in range(300)
    ]
    other = [{"candidate_id": "co-1", "course": "co", "intent": "diagnose", "messages": []}]
    _write(source, ds + replay + other)
    _write(target, [{"source_candidate_id": "external-source", "messages": []}])
    monkeypatch.setattr(rescue, "SOURCE", source)
    monkeypatch.setattr(rescue, "TARGET", target)
    monkeypatch.setattr(rescue, "OUT", out)

    report = rescue.prepare()

    assert report["train_total"] == 225 * 4 + 300
    assert report["targeted_source_overlap"] == 0
    rows = rescue._read(out / "train.jsonl")
    assert sum(row.get("course") == "ds-algo" for row in rows) == 225 * 4
    assert not any(row.get("course") == "co" for row in rows)


def test_qa_rescue_repeats_only_external_disjoint_qa(tmp_path, monkeypatch):
    source = tmp_path / "qa-train.jsonl"
    target = tmp_path / "qa-target.jsonl"
    out = tmp_path / "qa-out"
    qa = [
        {"candidate_id": f"qa-{i}", "source_id": f"train-chunk-{i}",
         "course": "co", "intent": "grounded_qa", "messages": []}
        for i in range(900)
    ]
    replay = [
        {"candidate_id": f"old-{i}", "replay_old_sft": True, "messages": []}
        for i in range(300)
    ]
    _write(source, qa + replay)
    _write(target, [{"source_id": "external-chunk", "messages": []}])
    monkeypatch.setattr(qa_rescue, "SOURCE", source)
    monkeypatch.setattr(qa_rescue, "TARGET", target)
    monkeypatch.setattr(qa_rescue, "OUT", out)
    monkeypatch.setattr(qa_rescue, "SEMANTIC", tmp_path / "missing-report.json")

    report = qa_rescue.prepare()

    assert report["train_total"] == 900 * 2 + 300
    assert report["targeted_source_overlap"] == 0


def test_taxonomy_rescue_refreshes_contract_without_target_content(tmp_path, monkeypatch):
    source = tmp_path / "taxonomy-train.jsonl"
    target = tmp_path / "taxonomy-target.jsonl"
    out = tmp_path / "taxonomy-out"
    mappings = list(taxonomy_rescue.MUTATION_TAG.items())
    ds = []
    for i in range(225):
        mutation, tag = mappings[i % len(mappings)]
        ds.append({
            "candidate_id": f"ds-{i}",
            "source_candidate_id": f"train-source-{i}",
            "course": "ds-algo",
            "intent": "diagnose",
            "verified_mutation": mutation,
            "messages": [
                {"role": "system", "content": "old contract"},
                {"role": "user", "content": f"independent training question {i}"},
                {"role": "assistant", "content": json.dumps({"error_tags": [tag]})},
            ],
        })
    replay = [{
        "candidate_id": f"old-{i}",
        "course": "ds-algo",
        "replay_old_sft": True,
        "messages": [
            {"role": "system", "content": "old contract"},
            {"role": "user", "content": f"old question {i}"},
            {"role": "assistant", "content": json.dumps({"error_tags": ["reasoning"]})},
        ],
    } for i in range(300)]
    _write(source, ds + replay)
    _write(target, [{
        "source_candidate_id": "external-source",
        "messages": [{"role": "user", "content": "must never enter training"}],
    }])
    monkeypatch.setattr(taxonomy_rescue, "SOURCE", source)
    monkeypatch.setattr(taxonomy_rescue, "TARGET", target)
    monkeypatch.setattr(taxonomy_rescue, "OUT", out)

    report = taxonomy_rescue.prepare()

    assert report["train_total"] == 225 * 4 + 300
    assert report["targeted_source_overlap"] == 0
    assert report["targeted_eval_content_used"] is False
    rows = taxonomy_rescue._read(out / "train.jsonl")
    assert "标签判定优先级" in rows[0]["messages"][0]["content"]
    assert all("must never enter training" not in json.dumps(row) for row in rows)

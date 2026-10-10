from scripts.eval_targeted_semantic import (
    _effective_max_new_tokens,
    _exclude_intents,
    _generation_messages,
    _semantic_signature,
    semantic_score,
)
import scripts.eval_targeted_semantic as semantic_eval


def _row(intent, gold, user="材料：TCP 提供可靠传输。"):
    import json
    return {"intent": intent, "course": "net", "messages": [
        {"role": "user", "content": user},
        {"role": "assistant", "content": json.dumps(gold, ensure_ascii=False)},
    ]}


def test_semantic_diagnose_requires_step_and_tag():
    gold = {"correct": False, "first_wrong_step": 1, "error_tags": ["boundary"]}
    assert semantic_score(_row("diagnose", gold), dict(gold))["task_ok"] is True
    assert semantic_score(_row("diagnose", gold), {**gold, "first_wrong_step": 2})["task_ok"] is False


def test_semantic_qa_requires_exact_source_evidence():
    gold = {"answer": "TCP可靠", "abstain": False,
            "citations": [{"source_id": "s1", "evidence": "TCP 提供可靠传输"}]}
    good = dict(gold)
    bad = {**gold, "citations": [{"source_id": "s1", "evidence": "TCP 保证绝不丢包"}]}
    row = _row("grounded_qa", gold, "source_id: s1\n材料：TCP 提供可靠传输。")
    assert semantic_score(row, good)["task_ok"] is True
    assert semantic_score(row, bad)["task_ok"] is False


def test_semantic_choice_solve_uses_gold_canonical():
    gold = {"final_answer": "答案：B", "final_answer_canonical": {"kind": "choice", "value": "B"}}
    row = _row("solve", gold, "题目：A.甲 B.乙 C.丙 D.丁")
    assert semantic_score(row, dict(gold))["task_ok"] is True
    assert semantic_score(row, {**gold, "final_answer": "答案：A",
                                "final_answer_canonical": {"kind": "choice", "value": "A"}})["task_ok"] is False


def test_production_prompt_refresh_changes_diagnose_system_message():
    row = _row("diagnose", {
        "correct": False, "first_wrong_step": 1, "error_tags": ["reasoning"],
    })
    original_user = row["messages"][0]

    messages = _generation_messages(row, refresh_production_prompts=True)

    assert messages[0]["role"] == "system"
    assert "标签判定优先级" in messages[0]["content"]
    assert messages[1] == original_user
    assert all(message.get("role") != "assistant" for message in messages)


def test_production_prompt_refresh_uses_shared_grounded_qa_contract():
    row = _row("grounded_qa", {"answer": "x", "abstain": False, "citations": []})
    messages = _generation_messages(row, refresh_production_prompts=True)

    assert messages[0]["role"] == "system"
    assert "evidence 必须是材料原文的连续子串" in messages[0]["content"]
    assert all(message.get("role") != "assistant" for message in messages)


def test_exclude_intents_keeps_only_default_route_tasks():
    rows = [
        {"intent": "diagnose", "candidate_id": "d"},
        {"intent": "grounded_qa", "candidate_id": "q"},
        {"intent": "solve", "candidate_id": "s"},
    ]
    selected = _exclude_intents(rows, {"grounded_qa"})
    assert [row["candidate_id"] for row in selected] == ["d", "s"]


def test_semantic_eval_uses_runtime_task_limits_under_production_contract():
    assert _effective_max_new_tokens("solve", 768, True) == 1024
    assert _effective_max_new_tokens("grounded_qa", 1024, True) == 512


def test_semantic_cache_signature_binds_adapter_data_parameters_and_prompt_contract(
        tmp_path, monkeypatch):
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}")
    weights = adapter / "adapter_model.safetensors"
    weights.write_bytes(b"v1")
    data = tmp_path / "eval.jsonl"
    data.write_text("{}\n")

    kwargs = dict(max_seq=4096, max_new=1024,
                  refresh_production_prompts=True,
                  excluded_intents={"grounded_qa"})
    first = _semantic_signature(str(adapter), data, **kwargs)
    assert first == _semantic_signature(str(adapter), data, **kwargs)
    weights.write_bytes(b"new-weights")
    assert first != _semantic_signature(str(adapter), data, **kwargs)
    assert first != _semantic_signature(
        str(adapter), data, **{**kwargs, "max_new": 768}
    )
    monkeypatch.setattr(
        semantic_eval, "build_grounded_qa_system", lambda: "changed-production-contract"
    )
    assert first != _semantic_signature(str(adapter), data, **kwargs)

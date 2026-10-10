import json

import scripts.test_lora as lora_eval

from scripts.test_lora import (
    _effective_max_new_tokens,
    _filter_rows_by_intent,
    _generation_messages,
    _intent,
    _row_cache_key,
    _standalone_signature,
)


def test_grounded_qa_intent_is_not_reported_as_other():
    assert _intent({"answer": "x", "abstain": False, "citations": []}) == "grounded_qa"


def test_filter_rows_by_intent_selects_without_mutating_rows():
    rows = []
    for intent, gold in (
        ("diagnose", {"correct": False, "score": 0, "first_wrong_step": 1,
                      "error_tags": ["reasoning"], "diagnosis": "x"}),
        ("solve", {"problem_type": "x", "knowledge_points": [], "givens": [],
                   "target": "x", "strategy": "x"}),
    ):
        rows.append({"id": intent, "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": json.dumps(gold)},
        ]})
    assert [row["id"] for row in _filter_rows_by_intent(rows, {"diagnose"})] == ["diagnose"]
    assert len(rows) == 2


def test_regression_refresh_only_replaces_diagnose_system_prompt():
    row = {
        "course": "ds-algo",
        "messages": [
            {"role": "system", "content": "old"},
            {"role": "user", "content": "frozen question"},
            {"role": "assistant", "content": "{}"},
        ],
    }
    refreshed = _generation_messages(row, "diagnose", True)
    assert "标签判定优先级" in refreshed[0]["content"]
    assert refreshed[1] == row["messages"][1]
    assert len(refreshed) == 2


def test_production_eval_uses_same_task_output_limits_as_runtime():
    assert _effective_max_new_tokens("solve", 768, True) == 1024
    assert _effective_max_new_tokens("practice", 768, True) == 1024
    assert _effective_max_new_tokens("diagnose", 1024, True) == 768
    assert _effective_max_new_tokens("grounded_qa", 1024, True) == 512
    assert _effective_max_new_tokens("solve", 768, False) == 768


def test_prediction_cache_key_and_signature_are_model_specific(tmp_path):
    row = {"course": "ds-algo", "messages": [{"role": "user", "content": "q"}]}
    assert _row_cache_key(row) == _row_cache_key(dict(row))

    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}")
    weights = adapter / "adapter_model.safetensors"
    weights.write_bytes(b"one")
    val = tmp_path / "val.jsonl"
    val.write_text("{}\n")
    first = _standalone_signature(
        str(adapter), str(val), max_seq=4096, max_new_tokens=768,
        intents={"diagnose"}, refresh_production_prompts=True)
    weights.write_bytes(b"different-size")
    second = _standalone_signature(
        str(adapter), str(val), max_seq=4096, max_new_tokens=768,
        intents={"diagnose"}, refresh_production_prompts=True)
    assert first != second

    monkey_contract = lambda: "changed-diagnose-contract"
    original = lora_eval._production_prompt_contract_fingerprint
    try:
        lora_eval._production_prompt_contract_fingerprint = monkey_contract
        third = _standalone_signature(
            str(adapter), str(val), max_seq=4096, max_new_tokens=768,
            intents={"diagnose"}, refresh_production_prompts=True)
    finally:
        lora_eval._production_prompt_contract_fingerprint = original
    assert second != third

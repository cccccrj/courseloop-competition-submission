import backend.agents.prompts as prompts
from backend.agents.prompts import GROUNDED_QA_SYSTEM, build_grounded_qa_system
from backend.rag.retriever import Hit, RagEngine, build_rag_system


def test_rag_production_uses_the_shared_grounded_qa_contract():
    # The production contract may be a gate-approved persistent override.
    # RAG must consume the same resolved contract as synthesis/evaluation,
    # rather than being pinned forever to the built-in fallback constant.
    assert build_rag_system(None) == build_grounded_qa_system()
    engine = RagEngine.__new__(RagEngine)
    prompt = engine.build_grounded_user(
        "TCP 有什么性质？",
        [Hit("TCP 提供可靠传输。", 1.0, "教材", 1, 1, "传输层")],
        "net",
    )
    assert "source_id: 1" in prompt
    assert "课程: net" in prompt
    assert "材料：\nTCP 提供可靠传输。" in prompt
    assert prompt.endswith("问题：TCP 有什么性质？")


def test_rag_production_preserves_text_api_for_structured_lora_output():
    assert RagEngine.parse_grounded_answer(
        '{"answer":"TCP 提供可靠传输","abstain":false,"citations":[]}'
    ) == "TCP 提供可靠传输"
    assert RagEngine.parse_grounded_answer(
        '{"answer":"","abstain":true,"citations":[]}'
    ) == "教材中未提及"
    assert RagEngine.parse_grounded_answer("普通云端文本") == "普通云端文本"


def test_grounded_qa_contract_override_is_shared_and_reversible(tmp_path, monkeypatch):
    override = tmp_path / "qa-contract.txt"
    monkeypatch.setattr(prompts, "GROUNDED_QA_OVERRIDE", override)
    assert prompts.build_grounded_qa_system() == GROUNDED_QA_SYSTEM

    compact = GROUNDED_QA_SYSTEM + prompts.GROUNDED_QA_COMPACT_SUFFIX
    override.write_text(compact, encoding="utf-8")
    assert prompts.build_grounded_qa_system() == compact

    override.unlink()
    assert prompts.build_grounded_qa_system() == GROUNDED_QA_SYSTEM


def test_trial_contract_environment_is_process_local_and_takes_precedence(
        tmp_path, monkeypatch):
    active = tmp_path / "active.txt"
    trial = tmp_path / "trial.txt"
    active.write_text("active-contract", encoding="utf-8")
    trial.write_text("trial-contract", encoding="utf-8")
    monkeypatch.setattr(prompts, "GROUNDED_QA_OVERRIDE", active)
    monkeypatch.setenv("CL_GROUNDED_QA_CONTRACT_FILE", str(trial))

    assert prompts.build_grounded_qa_system() == "trial-contract"

    monkeypatch.delenv("CL_GROUNDED_QA_CONTRACT_FILE")
    assert prompts.build_grounded_qa_system() == "active-contract"

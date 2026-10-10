from backend.core.config import Settings
from backend.core import llm


def test_task_model_and_endpoint_fall_back_to_workhorse():
    settings = Settings(
        _env_file=None,
        llm_text_base_url="http://text/v1",
        llm_text_api_key="text-key",
        llm_workhorse_model="default-model",
    )

    assert settings.model_for_task("diagnose") == "default-model"
    assert settings.endpoint_for_task("diagnose") == ("http://text/v1", "text-key")
    assert settings.temperature_for_task("diagnose") == settings.llm_workhorse_temperature
    assert settings.max_tokens_for_task("diagnose") == 768
    assert settings.max_tokens_for_task("solve") == 1024
    assert settings.max_tokens_for_task("grounded_qa") == 512
    assert settings.max_tokens_for_task("practice") == 1024


def test_task_specific_alias_and_endpoint_override():
    settings = Settings(
        _env_file=None,
        llm_text_base_url="http://text/v1",
        llm_text_api_key="text-key",
        llm_workhorse_model="default-model",
        llm_diagnose_base_url="http://local/v1",
        llm_diagnose_api_key="local-key",
        llm_diagnose_model="diagnose-lora",
    )

    assert settings.model_for_task("diagnose") == "diagnose-lora"
    assert settings.endpoint_for_task("diagnose") == ("http://local/v1", "local-key")


def test_ask_task_passes_model_temperature_and_task(monkeypatch):
    captured = {}

    def fake_chat(messages, model, temperature=0.2, max_tokens=None, task=None):
        captured.update(model=model, temperature=temperature, max_tokens=max_tokens, task=task)
        return "ok"

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm.settings, "llm_diagnose_model", "diagnose-lora")
    monkeypatch.setattr(llm.settings, "llm_diagnose_temperature", 0.05)

    assert llm.ask_diagnose([{"role": "user", "content": "x"}], max_tokens=123) == "ok"
    assert captured == {
        "model": "diagnose-lora", "temperature": 0.05,
        "max_tokens": 123, "task": "diagnose",
    }


def test_ask_task_uses_production_task_limit_when_unspecified(monkeypatch):
    captured = {}

    def fake_chat(messages, model, temperature=0.2, max_tokens=None, task=None):
        captured.update(max_tokens=max_tokens, task=task)
        return "ok"

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert llm.ask_solve([{"role": "user", "content": "x"}]) == "ok"
    assert captured == {"max_tokens": 1024, "task": "solve"}

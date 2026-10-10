"""视觉独立路由不影响文本，且保留既有统一配置兼容。"""
import pytest
from backend.core import llm
from backend.core.config import settings

@pytest.fixture
def routing(monkeypatch):
    values = {"llm_base_url":"https://text.test/v1", "llm_api_key":"text-test-key",
              "llm_text_base_url":"", "llm_text_api_key":"",
              "llm_vision_base_url":"", "llm_vision_api_key":"", "llm_vision_model":"vision-test"}
    for name,value in values.items():
        monkeypatch.setattr(settings,name,value)
    monkeypatch.setattr(llm,"_build_client",lambda base,key:(base,key))
    return monkeypatch

def test_vision_falls_back_to_existing_global_endpoint(routing):
    assert settings.vision_ready
    assert llm.get_vision_client() == ("https://text.test/v1","text-test-key")

def test_dedicated_vision_credentials_do_not_change_text(routing):
    before = (settings.text_llm_base_url,settings.text_llm_api_key)
    routing.setattr(settings,"llm_vision_base_url","https://vision.test/v1")
    routing.setattr(settings,"llm_vision_api_key","vision-test-key")
    assert llm.get_vision_client() == ("https://vision.test/v1","vision-test-key")
    assert (settings.text_llm_base_url,settings.text_llm_api_key) == before

def test_vision_can_be_ready_without_text_configuration(routing):
    routing.setattr(settings,"llm_base_url","")
    routing.setattr(settings,"llm_api_key","")
    routing.setattr(settings,"llm_vision_base_url","https://vision.test/v1")
    routing.setattr(settings,"llm_vision_api_key","vision-test-key")
    assert settings.vision_ready
    assert not settings.text_llm_ready

def test_missing_vision_model_is_not_ready(routing):
    routing.setattr(settings,"llm_vision_model","")
    assert not settings.vision_ready
    with pytest.raises(llm.LLMNotConfigured):
        llm.get_vision_client()

def test_transcription_prompt_forbids_invented_solution_steps():
    from backend.agents.photo_grader import build_prompt
    prompt = build_prompt("ds-algo")
    assert "图片中实际可见" in prompt
    assert "图片没有的内容一律不输出" in prompt
    assert "[?]" in prompt

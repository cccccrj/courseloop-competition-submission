"""GPU交付工具的CPU契约回归，实际CUDA验收单独执行。"""
import json

import pytest
import httpx

from scripts import submission_gpu_test as bench
from scripts import submission_gpu_deploy as deploy


def test_distribution_empty_and_single():
    assert bench.distribution([]) is None
    assert bench.distribution([7])["p95"] == 7


def test_distribution_linear_percentile():
    result = bench.distribution([5, 1, 4, 2, 3])
    assert result["mean"] == 3
    assert result["median"] == 3
    assert result["p95"] == 4.8


@pytest.mark.parametrize("fixed", [True, False])
def test_stream_usage_and_nonempty(monkeypatch, fixed):
    payloads = []
    def handle(request):
        payloads.append(json.loads(request.content))
        events = [dict(choices=[dict(delta={"content":"答案"}, finish_reason=None)]),
                  dict(choices=[dict(delta={}, finish_reason="length")]),
                  dict(choices=[], usage=dict(prompt_tokens=12, completion_tokens=2))]
        return httpx.Response(200, text="\n\n".join("data: "+json.dumps(x) for x in events)
                              +"\n\ndata: [DONE]\n\n")
    original = httpx.Client
    monkeypatch.setattr(bench.httpx, "Client", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handle)))
    result = bench.request("http://127.0.0.1:18003/v1", "model", [], 2, fixed)
    assert result["passed"] and result["content"] == "答案"
    assert result["usage"]["completion_tokens"] == 2
    assert result["ttft_ms"] >= 0
    assert ("ignore_eos" in payloads[0]) is fixed


def test_model_command_is_loopback_and_reproducible(monkeypatch, tmp_path):
    for directory, filename in (("Qwen3-14B-FP8", "model.safetensors.index.json"),
                                ("courseloop-408", "adapter_model.safetensors")):
        folder = tmp_path / "model" / directory
        folder.mkdir(parents=True)
        (folder / filename).touch()
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    cmd = deploy.model_command("vllm")
    assert cmd[cmd.index("--host")+1] == "127.0.0.1"
    assert cmd[cmd.index("--quantization")+1] == "fp8"
    assert "--no-enable-prefix-caching" in cmd
    assert "--enable-lora" in cmd


def test_configure_preserves_existing_demo_database(monkeypatch, tmp_path):
    from scripts import submission_prepare
    root = tmp_path / "courseloop-gpu-submission-test"
    data = root / "data/processed"
    data.mkdir(parents=True)
    database = data / "courseloop.db"
    database.write_bytes(b"preserve-existing-demo")
    (root / ".env").write_text("CL_EXISTING_TEST_MARKER=keep\n")
    monkeypatch.setattr(deploy, "ROOT", root)
    def forbidden_bootstrap():
        raise AssertionError("existing database must not be bootstrapped")
    monkeypatch.setattr(submission_prepare, "bootstrap", forbidden_bootstrap)
    deploy.configure("a"*40, 18003)
    assert database.read_bytes() == b"preserve-existing-demo"
    assert "CL_EXISTING_TEST_MARKER=keep" in (root / ".env").read_text()

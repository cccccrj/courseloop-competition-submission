"""复核调用落账，以及"实测 vs 估算"分开报的成本表。

上一轮全量补题跑完，被问"这批花了多少"时只能给个区间：`llm_calls.jsonl`
只记了出题那一路，而一道选择题要问三家复核员，**复核的调用量是出题的三倍、
一条都没落账**。补上之后要守住两件事，用例各钉一条：

1. 复核每问一家就落一行，带 token 用量；调用失败也要落（否则"否决率变了"
   到底是模型退步还是网关抖动，事后查不出来）；
2. 报表里**实测与估算不合并**。合并的坏处很具体：下次有人拿这张表去谈预算，
   会把估出来的数当成实测的数用。
"""
from __future__ import annotations

import json

import pytest

from backend.core import telemetry
from backend.core.config import settings
from backend.verification import cross_check

from scripts.cost_report import load, price_of, summarize


@pytest.fixture()
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path))
    return tmp_path / telemetry.LOG_FILE


def _rows(path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


# ---------- 落账 ----------

def test_each_judge_call_is_logged_with_token_usage(logs, monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "http://stub")
    monkeypatch.setattr(settings, "llm_api_key", "stub")
    monkeypatch.setattr("backend.core.llm.chat_raw",
                        lambda msgs, **kw: {"content": "B", "latency_ms": 12,
                                            "prompt_tokens": 100, "completion_tokens": 3})
    v = cross_check.review("题干", {"kind": "choice", "value": "B"}, ("gpt-4o", "deepseek-chat"))
    assert v.passed is True

    rows = _rows(logs)
    assert [r["intent"] for r in rows] == ["cross_check"] * 2
    assert {r["model"] for r in rows} == {"gpt-4o", "deepseek-chat"}
    assert all(r["usage"]["prompt_tokens"] == 100 for r in rows)


def test_failed_judge_call_is_logged_too(logs, monkeypatch):
    """一个复核员挂了要留痕：否则否决率的变化分不清是模型退步还是网关抖动。"""
    monkeypatch.setattr(settings, "llm_base_url", "http://stub")
    monkeypatch.setattr(settings, "llm_api_key", "stub")

    def _boom(msgs, **kw):
        raise RuntimeError("Error code: 502")

    monkeypatch.setattr("backend.core.llm.chat_raw", _boom)
    v = cross_check.review("题干", {"kind": "choice", "value": "B"}, ("gpt-4o",))
    assert v.passed is None          # 失败不算同意

    row = _rows(logs)[0]
    assert row["intent"] == "cross_check"
    assert row["verification"]["error"] == "RuntimeError"
    # 没打成的调用没有用量，就不该编一个 usage 出来
    assert "usage" not in row


def test_usage_key_absent_when_gateway_returns_nothing(logs):
    """网关不回 usage 时不写这个键 —— 写一个全 null 的 usage，
    报表就分不清"网关没给"和"调用方忘了传"。"""
    telemetry.log_llm_call("practice", [], "out",
                           usage={"prompt_tokens": None, "completion_tokens": None})
    assert "usage" not in _rows(logs)[0]


# ---------- 报表 ----------

def test_measured_and_estimated_never_merge(logs):
    telemetry.log_llm_call("cross_check", [], "B", model="gpt-4o",
                           usage={"prompt_tokens": 1_000_000, "completion_tokens": 0})
    telemetry.log_llm_call("practice", [], "…", model="gpt-4o-mini")   # 无 usage
    agg = summarize(load(logs))

    measured = agg[("cross_check", "gpt-4o")]
    assert measured["metered"] == 1
    assert measured["measured_cny"] == pytest.approx(18.0)   # 百万输入 token × 18 元
    est = agg[("practice", "gpt-4o-mini")]
    assert est["metered"] == 0 and est["measured_cny"] == 0.0


def test_unknown_model_is_not_given_a_made_up_price(logs):
    """认不出的模型只统计 token，不按别的模型硬套一个价。"""
    assert price_of("some-local-lora") is None
    assert price_of("openai/gpt-4o-mini") == price_of("gpt-4o-mini")   # 带前缀仍认得
    telemetry.log_llm_call("practice", [], "…", model="some-local-lora",
                           usage={"prompt_tokens": 500, "completion_tokens": 500})
    a = summarize(load(logs))[("practice", "some-local-lora")]
    assert a["prompt"] == 500 and a["measured_cny"] == 0.0 and a["unpriced"] == 1


def test_broken_line_does_not_kill_the_report(logs):
    telemetry.log_llm_call("practice", [], "ok", model="gpt-4o-mini")
    with open(logs, "a", encoding="utf-8") as f:
        f.write("{半行坏数据\n")
    assert len(load(logs)) == 1

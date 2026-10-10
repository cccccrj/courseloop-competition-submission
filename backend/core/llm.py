"""统一的 LLM 客户端工厂（OpenAI 兼容接口）。

四角色分工（均经 OpenAI 兼容端点，如智增增 zhizengzeng）：
    expert    严谨生成（批改/文书）
    workhorse 路由 / RAG 总结 / 讲解 / 简单问答
    reasoner  强 CoT：步骤化解析 / 错因诊断（保留 <think>）
    vision    多模态：拍照批改的手写转写（ask_vision）
连接信息由 backend/core/config.py 从 .env 读取（前缀 CL_）。
"""
from __future__ import annotations

import time
from functools import lru_cache

import httpx
from openai import OpenAI

from backend.core.config import settings
from backend.core.sanitize import strip_markers


class LLMNotConfigured(RuntimeError):
    """当代码需要调用 LLM 但尚未在 .env 配置时抛出。"""


@lru_cache(maxsize=8)
def _build_client(base_url: str, api_key: str) -> OpenAI:
    """trust_env=False：忽略环境 http_proxy/https_proxy 强制直连，避免国内 API 被本地
    代理（如 Clash 7897）绕到境外超时。"""
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=httpx.Client(trust_env=False, timeout=httpx.Timeout(600.0, connect=10.0)),
    )


def get_text_client() -> OpenAI:
    if not settings.text_llm_ready:
        raise LLMNotConfigured(
            "文本 LLM 未配置。请在项目根 .env 填写 CL_LLM_BASE_URL / CL_LLM_API_KEY / "
            "CL_LLM_WORKHORSE_MODEL（或单独的 CL_LLM_TEXT_BASE_URL / CL_LLM_TEXT_API_KEY）。")
    return _build_client(settings.text_llm_base_url, settings.text_llm_api_key)


def get_vision_client() -> OpenAI:
    """视觉独立端点/凭据优先，未指定时兼容统一端点；不覆盖文本配置。"""
    if not settings.vision_ready:
        raise LLMNotConfigured(
            "视觉 LLM 未配置。请在项目根 .env 填写 CL_LLM_VISION_BASE_URL / "
            "CL_LLM_VISION_API_KEY（或统一CL_LLM_BASE_URL / CL_LLM_API_KEY）及CL_LLM_VISION_MODEL。")
    return _build_client(settings.vision_llm_base_url, settings.vision_llm_api_key)


def get_client_for_task(task: str) -> OpenAI:
    """任务级端点；未配置专项端点时自动回退统一文本端点。"""
    base, key = settings.endpoint_for_task(task)
    if not (base and key):
        raise LLMNotConfigured(
            f"任务 {task} 的文本 LLM 未配置；请填写 CL_LLM_TEXT_* 或 CL_LLM_{task.upper()}_*。")
    return _build_client(base, key)


def _no_think_extra() -> dict:
    """本地 no-think 微调模型：关闭 Qwen3 思考模式的渲染开关。API 模型下返回空 dict。"""
    if settings.llm_no_think:
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {}


def chat(messages: list[dict], model: str, temperature: float = 0.2,
         max_tokens: int | None = None, task: str | None = None) -> str:
    client = get_client_for_task(task) if task else get_text_client()
    kwargs: dict = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs.update(_no_think_extra())
    resp = client.chat.completions.create(**kwargs)
    # 只删整行的控制标记 / 思考块 / 特殊 token，JSON 输出不受影响（见 sanitize.py）
    return strip_markers(resp.choices[0].message.content)


def chat_raw(messages: list[dict], *, base_url: str, api_key: str, model: str,
             temperature: float = 0.0, max_tokens: int | None = None,
             no_think: bool | None = None) -> dict:
    """显式指定端点的一次调用，回带耗时与 token 用量。

    上面那些 ask_* 只回字符串，对模型对照台不够用 —— 那页要比的正是耗时和用量。
    也不能走 task 路由：对照台的意义是把两个端点摆在一起，端点必须由调用方点名，
    不能有"没配就悄悄回退到 workhorse"这种便利，否则两栏可能其实是同一个模型。

    no_think 默认跟随全局 settings.llm_no_think；对照台会给本地那一侧显式传 True，
    因为那个开关是本地 Qwen3 LoRA 的渲染参数，两侧未必同源。
    """
    if not (base_url and api_key and model):
        raise LLMNotConfigured("chat_raw 需要显式的 base_url / api_key / model")
    client = _build_client(base_url, api_key)
    kwargs: dict = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if (settings.llm_no_think if no_think is None else no_think):
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.perf_counter()
    resp = client.chat.completions.create(**kwargs)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    usage = getattr(resp, "usage", None)
    return {
        "content": resp.choices[0].message.content or "",
        "latency_ms": latency_ms,
        # 有些 OpenAI 兼容网关不回 usage。返回 None 而不是 0 ——
        # "没告诉我用了多少" 和 "用了 0 个 token" 在成本估算上不是一回事。
        "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
    }


def ask_workhorse(messages: list[dict], max_tokens: int | None = None) -> str:
    return chat(messages, model=settings.workhorse_model,
                temperature=settings.llm_workhorse_temperature, max_tokens=max_tokens)


def ask_expert(messages: list[dict]) -> str:
    return chat(messages, model=settings.expert_model, temperature=settings.llm_expert_temperature)


def ask_task(task: str, messages: list[dict], max_tokens: int | None = None) -> str:
    """调用任务专项 LoRA alias；空配置时透明回退 workhorse。"""
    if max_tokens is None:
        max_tokens = settings.max_tokens_for_task(task)
    return chat(messages, model=settings.model_for_task(task),
                temperature=settings.temperature_for_task(task),
                max_tokens=max_tokens, task=task)


def ask_task_stream(task: str, messages: list[dict], max_tokens: int | None = None):
    """ask_task 的流式版：同一个任务路由（端点、模型、温度、max_tokens 都一样），逐块产出 content。

    给"等得见进度"的场景用（个性化练习出题）：结果与非流式完全等价，只是边生成边给。"""
    if max_tokens is None:
        max_tokens = settings.max_tokens_for_task(task)
    client = get_client_for_task(task)
    kwargs: dict = {"model": settings.model_for_task(task), "messages": messages,
                    "temperature": settings.temperature_for_task(task), "stream": True}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs.update(_no_think_extra())
    for chunk in client.chat.completions.create(**kwargs):
        if not chunk.choices:
            continue
        c = getattr(chunk.choices[0].delta, "content", None)
        if c:
            yield c


def ask_diagnose(messages: list[dict], max_tokens: int | None = None) -> str:
    return ask_task("diagnose", messages, max_tokens)


def ask_solve(messages: list[dict], max_tokens: int | None = None) -> str:
    return ask_task("solve", messages, max_tokens)


def ask_grounded_qa(messages: list[dict], max_tokens: int | None = None) -> str:
    return ask_task("grounded_qa", messages, max_tokens)


def ask_practice(messages: list[dict], max_tokens: int | None = None) -> str:
    return ask_task("practice", messages, max_tokens)


def ask_vision(prompt: str, image_data_url: str, max_tokens: int = 2048) -> str:
    """单图问答。image_data_url 形如 data:image/jpeg;base64,xxx。

    不带 _no_think_extra()：那个开关是给本地 Qwen3 文本 LoRA 的渲染参数，
    视觉端点走的是另一家 API，传过去只会被拒。"""
    client = get_vision_client()
    resp = client.chat.completions.create(
        model=settings.vision_model,
        temperature=settings.llm_vision_temperature,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url}}]}],
    )
    return strip_markers(resp.choices[0].message.content)


def ask_workhorse_stream(messages: list[dict]):
    """流式调用工作模型，逐块产出 content 增量。"""
    client = get_text_client()
    stream = client.chat.completions.create(
        model=settings.workhorse_model, messages=messages,
        temperature=settings.llm_workhorse_temperature, stream=True,
        **_no_think_extra())
    for chunk in stream:
        if not chunk.choices:
            continue
        c = getattr(chunk.choices[0].delta, "content", None)
        if c:
            yield c


def ask_reasoner_stream(messages: list[dict]):
    """流式调用推理模型，逐块产出 (kind, delta)，kind ∈ {reasoning, content}。
    用于步骤化解析/错因诊断的「解题思路」透明展示。"""
    client = get_text_client()
    stream = client.chat.completions.create(
        model=settings.reasoner_model, messages=messages,
        temperature=settings.llm_reasoner_temperature, stream=True,
        **_no_think_extra())
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            yield ("reasoning", rc)
        c = getattr(delta, "content", None)
        if c:
            yield ("content", c)

"""把异常翻成学生/助教能照做的一句话，原文只进日志。

起因见 docs/34 §2.2：前端的 axios 通道在 3d70713 已经不再甩接口路径和后端原文，
但流式接口（大厅、解题、拍照、模考）仍是 `f"LLM 调用失败：{e}"` 直出 ——
而 `LLMNotConfigured` 的原文本身就写着 `.env` 和环境变量名。

每次翻译都生成一个**错误编号**，同一个编号写进日志、也随话术一起返回。
学生报告"刚才报错了"时说出这个编号，运维就能在日志里直接找到那一次的堆栈，
不必再让学生描述"大概几点、点了哪里"。
"""
from __future__ import annotations

import logging
import uuid

from backend.core.llm import LLMNotConfigured

logger = logging.getLogger("courseloop.errors")

AI_UNAVAILABLE = "AI 服务暂未开通，请稍后再试或联系老师。"
AI_TIMEOUT = "AI 这次回复超时了，请重试。"
GENERIC = "这一步没有完成，请重试。"


def new_error_id() -> str:
    return uuid.uuid4().hex[:8]


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "timedout" in name


def classify(exc: BaseException) -> str:
    """not_configured / timeout / other。前端按这个给不同的下一步（重试、手动输入……）。"""
    if isinstance(exc, LLMNotConfigured):
        return "not_configured"
    if _is_timeout(exc):
        return "timeout"
    return "other"


def public_message(exc: BaseException) -> str:
    kind = classify(exc)
    return {"not_configured": AI_UNAVAILABLE, "timeout": AI_TIMEOUT}.get(kind, GENERIC)


def report(exc: BaseException, where: str, **ctx) -> dict:
    """记日志并返回可外发的错误体：{message, error_id, kind}。

    `where` 是日志里的定位词（如 "chat_stream"），不外发。
    """
    error_id = new_error_id()
    extra = " ".join(f"{k}={v}" for k, v in ctx.items())
    logger.error("[%s] %s 失败 %s", error_id, where, extra, exc_info=exc)
    return {"message": public_message(exc), "error_id": error_id, "kind": classify(exc)}

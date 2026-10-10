"""验证器统一接口。"""
from __future__ import annotations

from dataclasses import dataclass, field

# 本包实际提供的验证器，与 backend/verification/ 下的五个模块一一对应。
# 这是**课程级** `Course.verification_types` 的合法取值域：它声明这门课能用哪些验证方式，
# 供 prompt 组装与 SFT 生成 gate 使用。
#
# 注意与**题目级** `problems.verification_type` 区分 —— 后者是判分分派用的，
# 只认 numeric / rule / unit_test 三种（见 core_routes 的判分分支与
# scripts/qa_paper_draft.py 的收口）。complexity 与 citation 是伴随判定，
# 不作为一道题的主判分方式。两者混为一谈的代价上一轮踩过：LLM 自造的
# verification_type="choice" 不在任何词表里，进库后被当概念题按关键词覆盖判分。
VERIFICATION_TYPES = frozenset({"unit_test", "complexity", "rule", "citation", "numeric"})

# 题目级判分分派认得的取值，`qa_paper_draft` 与判题端点共用同一份口径。
PROBLEM_VERIFICATION_TYPES = frozenset({"numeric", "rule", "unit_test"})


@dataclass
class VerifyResult:
    method: str                 # VERIFICATION_TYPES 之一
    passed: bool | None         # True/False；None=无法判定（证据不足，安全边界）
    details: str = ""
    confidence: float = 1.0
    extra: dict = field(default_factory=dict)

    def to_dict(self, include_tech: bool = False) -> dict:
        """序列化。默认不带 extra["tech"]（原始异常 / 沙箱输出）：这个 dict 常被原样
        返回给页面，原始报错只该进日志和助教的"技术详情"（I-05）。"""
        extra = self.extra if include_tech else {k: v for k, v in self.extra.items() if k != "tech"}
        return {"method": self.method, "passed": self.passed,
                "details": self.details, "confidence": self.confidence, **({"extra": extra} if extra else {})}


class Verifier:
    method = "base"

    def verify(self, *args, **kwargs) -> VerifyResult:  # noqa: D401
        raise NotImplementedError

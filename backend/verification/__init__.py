"""确定性验证器（三层验证之第二层）。

对外统一 VerifyResult / Verifier 接口，供 solver / diagnoser / practice_generator 调用：
    - unit_test_runner : 沙箱运行代码 + 测试样例
    - complexity_checker: 复杂度规则/AST 估计
    - rule_checker      : 概念/边界规则校验
"""
from backend.verification.base import (  # noqa: F401
    PROBLEM_VERIFICATION_TYPES, VERIFICATION_TYPES, VerifyResult, Verifier,
)

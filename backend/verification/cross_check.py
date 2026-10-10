"""选择题交叉复核：让别的模型独立把题做一遍，不同意就否掉。

## 为什么需要它

本包其余四个验证器都是**确定性**的（跑单测、算数值、查得分点覆盖）。选择题走
`numeric_checker` 的 choice 分支，判的是「模型给的 answer 与它自己的
final_answer_canonical 是否一致」—— 那是**自洽**，不是**正确**。

实测证据：`grow_bank` 出的 30 道数据结构选择题全部通过既有门禁（接受率 100%），
但让 gpt-4o 与 deepseek-chat 各自独立作答后，**9 道（30%）标答是错的或题目本身
是坏的**。典型两例：

- 「Dijkstra 的时间与空间复杂度」标答 O(n²)/O(1) —— 空间是 O(n)（dist 数组），
  两个复核员都判 B；
- 「A到B最短路径为 d…求 A到u 的最短路径」题干自相矛盾，两个复核员都判「题目有问题」。

这类题通过所有现有门禁进库，然后出现在模考卷上。对"AI 助教"这个定位，
一道答案错的题比少十道题伤害大得多。

## 判据：一票否决

任一复核员不同意就否掉。**丢一道对的题只是多跑一次生成（¥0.0025），
留一道错的题是产品事故** —— 两侧代价不对称，就不该取中间。

复核员看不到标准答案，也看不到出题时用的教材片段：给了就成了"请确认"，
模型会顺着标答说。同题独立作答才是真的第二意见。

`run_targeted_synthetic_pilot` 里那 50 道操作系统选择题用的就是这个办法
（Kimi/Qwen 同题求解，二者都必须命中规范答案），本模块把它接到常规出题链路上。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from backend.verification.base import VerifyResult

# 默认复核阵容：三家不同来源的模型。**跨厂比多加同厂模型有用得多** ——
# 实测 gpt-4o 与 deepseek-chat 各自漏判的题不重合（并起来在 30 道里抓出 9 道），
# 但它们同时漏掉了「多个选项都对」这一类：
#   「以下关于红黑树的性质，哪一项是正确的」A（非红即黑）与 D（两个红节点不相邻）
#   都是红黑树的性质，两个复核员恰好都选了标答 A，于是放行。
# 加入 gemini-2.5-flash 后这一类被抓出来。三家同时漏判同一道题的概率显著更低。
DEFAULT_JUDGES = ("gpt-4o", "deepseek-chat", "gemini-2.5-flash")

_SYS = ("你是 408 计算机考研阅卷老师。只做题，不出题。"
        "仔细计算与推理后，只输出正确选项的字母（A/B/C/D），不要解释、不要任何其他文字。"
        "如果题目本身有错（题干矛盾、没有正确选项、多个选项都对），输出 X。")

_LETTER = re.compile(r"\b([A-DX])\b")


def _normalize(value) -> set[str]:
    """标准答案归一成字母集合。多选题的 value 是列表。"""
    vals = value if isinstance(value, list) else [value]
    return {str(v).strip().upper() for v in vals if str(v).strip()}


def _ask_one(judge: str, stem: str) -> tuple[str, str]:
    """让一个复核员做题。返回 (模型名, 字母 / 'X' / '?')。

    调用失败返回 '?' 而不是抛异常：一个复核员挂了不该让整批出题停下，
    但也**不能算它同意** —— 见 `review()` 里对 '?' 的处置。
    """
    from backend.core import llm as llm_mod
    from backend.core import telemetry
    from backend.core.config import settings

    msgs = [{"role": "system", "content": _SYS}, {"role": "user", "content": stem}]
    try:
        out = llm_mod.chat_raw(
            msgs, model=judge,
            base_url=settings.llm_base_url, api_key=settings.llm_api_key,
            # temperature=0：复核要的是可复现的判断，不是多样性。
            # max_tokens 给足：推理模型会先花掉一大截 token 想，给少了只返回空串
            # （实测 gpt-5-nano 在 1600 下 100% 返回空内容）。
            temperature=0, max_tokens=8000)
        m = _LETTER.search((out["content"] or "").strip().upper())
        verdict = m.group(1) if m else "?"
        # 复核这一路此前完全没落账：一道题要问三家，调用量是出题的三倍，
        # 而 llm_calls.jsonl 里一条都没有 —— "这批题花了多少钱"于是永远只是个估数。
        # 这里带上 chat_raw 回的 token 用量，成本才算得出来而不是猜出来。
        telemetry.log_llm_call("cross_check", msgs, out["content"] or "", model=judge,
                               verification={"passed": None, "verdict": verdict},
                               usage={"prompt_tokens": out.get("prompt_tokens"),
                                      "completion_tokens": out.get("completion_tokens"),
                                      "latency_ms": out.get("latency_ms")})
        return judge, verdict
    except Exception as exc:  # noqa: BLE001
        # 失败也记一行：一批题里有多少复核请求根本没打成，是判断"否决率变化"
        # 是模型退步还是网关抖动的直接证据。
        telemetry.log_llm_call("cross_check", msgs, "", model=judge,
                               verification={"passed": None,
                                             "error": type(exc).__name__})
        return judge, f"?{type(exc).__name__}"


def review(stem: str, canonical: dict, judges: tuple[str, ...] = DEFAULT_JUDGES) -> VerifyResult:
    """让复核员们独立做这道选择题，全体同意才算通过。

    `passed=False` 有人明确反对；`passed=None` 没人反对但也没凑齐同意
    （复核员调用失败/答不出）—— 调用方按"存疑"处理，同样不入库，
    但和"确定错"要分开记，否则一次网络抖动会被当成模型质量下降。
    """
    gold = _normalize(canonical.get("value"))
    if not gold:
        return VerifyResult(method="cross_check", passed=None,
                            details="标准答案为空，无从复核")

    with ThreadPoolExecutor(max_workers=len(judges)) as ex:
        verdicts = dict(ex.map(lambda j: _ask_one(j, stem), judges))

    agree = [j for j, v in verdicts.items() if v in gold]
    broken = [j for j, v in verdicts.items() if v == "X"]
    unusable = [j for j, v in verdicts.items() if v.startswith("?")]
    dissent = [j for j, v in verdicts.items()
               if v not in gold and v != "X" and not v.startswith("?")]

    shown = "；".join(f"{j}={v}" for j, v in verdicts.items())
    if broken:
        return VerifyResult(method="cross_check", passed=False,
                            details=f"复核认为题目本身有问题（{', '.join(broken)}）：{shown}",
                            extra={"reason": "broken", "verdicts": verdicts})
    if dissent:
        return VerifyResult(method="cross_check", passed=False,
                            details=f"复核答案与标答不符（标答 {'/'.join(sorted(gold))}）：{shown}",
                            extra={"reason": "disagree", "verdicts": verdicts})
    if unusable:
        return VerifyResult(method="cross_check", passed=None,
                            details=f"复核未能给出判断：{shown}",
                            extra={"reason": "unusable", "verdicts": verdicts},
                            confidence=len(agree) / len(judges) if judges else 0.0)
    return VerifyResult(method="cross_check", passed=True,
                        details=f"{len(agree)}/{len(judges)} 位复核员独立作答一致：{shown}",
                        extra={"reason": "agree", "verdicts": verdicts})

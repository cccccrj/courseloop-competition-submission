"""拍照批改 agent：草稿纸照片 -> 手写解题步骤转写文本。

只负责「转写」这一步；转写结果交给 error_diagnoser 走既有诊断链路，不另起一套判错逻辑。

提示词沿用 scripts/ocr_bench/run_api.py 已横评验证过的两条纪律：公式用 LaTeX、
看不清写 [?] 绝不猜测补全。这两条不是修辞——转写幻觉会顺着
诊断 -> 错题本 -> 薄弱画像 -> 复习计划整条闭环脏下去，宁可留白让学生自己补。
"""
from __future__ import annotations

from backend.core import llm as llm_mod
from backend.core import telemetry
from backend.core.config import settings
from backend.core.courses import get_course

TRANSCRIBE_PROMPT = (
    "这是一张学生手写的{display}解题草稿纸。请忠实转写【手写内容】：\n"
    "1) 按解题步骤原有顺序逐行转写，保留学生的分步结构（第一步/第二步、(1)(2) 等编号照抄）；\n"
    "2) 公式、下标、求和号一律用 LaTeX（行内 $...$）；表格用 Markdown 表格；\n"
    "3) 画的图/树/链表结构无法文字化的，用 [图:一句话描述] 占位；\n"
    "4) 划掉重写的内容以最终保留的那版为准，不必转写被划掉的。\n"
    "★只输出图片中实际可见的文字（印刷或手写均忠实照抄）。不要根据课程或题型"
    "补写第一步/第二步、计算过程、页面数、图示或结论；图片没有的内容一律不输出。\n"
    "★看不清的字写 [?]，绝不猜测、绝不替学生补全或修正他写错的步骤——"
    "你的任务是誊抄，不是解题。直接输出 Markdown，不要加任何前言。"
)


def build_prompt(course: str = "ds-algo") -> str:
    return TRANSCRIBE_PROMPT.format(display=get_course(course).display)


def transcribe(image_data_url: str, course: str = "ds-algo") -> str:
    """调视觉模型把照片转成 Markdown 文本。未配置视觉模型时抛 LLMNotConfigured。"""
    prompt = build_prompt(course)
    raw = llm_mod.ask_vision(prompt, image_data_url)
    # 埋点只留提示词与转写结果，不落图片 base64——那会让 llm_calls.jsonl 迅速膨胀到不可读。
    telemetry.log_llm_call("photo_transcribe",
                           [{"role": "user", "content": prompt}],
                           raw, parsed=None, verification=None,
                           model=settings.vision_model)
    return raw.strip()

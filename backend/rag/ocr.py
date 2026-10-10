"""扫描件 OCR：没有文本层的 PDF，逐页交给视觉模型转写成文字，再走正常的分块入索引。

## 为什么单独一个模块

`scripts/vlm_ocr_test.py` 早就把这条路走通了（`data/ocr` 那 53 页就是它转的，
成本有账可查），但它是个**对照实验脚本**：自己建 OpenAI 客户端、自己读 key、
结果写到 `data/ocr/vlm_out` 供人肉比对。上传链路要的是另一回事 ——
走项目统一的视觉端点、结果直接进索引、每一页都落账。所以把可复用的那部分
搬到这里，脚本留着做模型对照。

## 什么时候被调用

只在**上传链路**里、且只在"数字版解析出 0 块"之后（`admin_routes._run_ingest`）。
不放进 `ingest.parse_one`：那条路是批量重建索引用的，`data/raw` 下随便一份扫描件
就会让一次重建悄悄烧掉几十块钱。**花钱的事必须发生在有人按了按钮之后。**

## 三条边界

1. **页数上限**（`MAX_OCR_PAGES`）。一份 200 页的扫描教材按页调视觉模型，
   费用和耗时都不是上传接口该承受的。超了就转写前 N 页并在任务行里写明
   "只转写了前 N 页"——半份能检索的资料，好过一份传了等于没传的资料，
   但必须让人看见这件事发生过。
2. **逐页失败不终止**。某一页转写失败（超时/内容被网关拦）只丢这一页，
   其余照常入库。整份都失败才算失败。
3. **页码要留住**。转写结果按页交给 `chunk_pages`，引用里才能出现"第 12 页"——
   这是本项目"教材依据"这个承诺的最小单位。拼成一根长字符串就没有页码了。
"""
from __future__ import annotations

import base64

from backend.core import telemetry
from backend.core.config import settings
from backend.rag.chunking import Chunk, chunk_pages

# 单次上传最多转写多少页。50 页按视觉模型的价大约几元，是"一份卷子/一章讲义"
# 的量级；再多就该走离线批处理而不是卡在上传接口里。
MAX_OCR_PAGES = 50
# 渲染 DPI。150 是 vlm_ocr_test 实测下来公式和下标还认得清、图又不至于太大的点。
RENDER_DPI = 150

_PROMPT = (
    "这是一页扫描的课程资料（可能是教材、课件或试卷），特点：可能双栏排版、"
    "含公式/表格/图，可能有手写笔迹。请提取【印刷体的正文内容】，要求：\n"
    "1) 按正确阅读顺序输出：先读完左栏、再读右栏，不要左右串行；\n"
    "2) 公式用 LaTeX（$...$），表格用 Markdown 表格；\n"
    "3) 忽略手写笔迹与批改痕迹；\n"
    "4) 只输出转写内容本身，不要写任何说明或前后缀。"
)


def has_text_layer(pdf_path: str) -> bool:
    """这份 PDF 有没有文本层。没有就是扫描件，只能走 OCR。"""
    import fitz

    doc = fitz.open(pdf_path)
    try:
        for page in doc:
            if (page.get_text() or "").strip():
                return True
        return False
    finally:
        doc.close()


def render_page(pdf_path: str, page_no: int, dpi: int = RENDER_DPI) -> bytes:
    """把第 page_no 页（1 起）渲成 PNG。"""
    import fitz

    doc = fitz.open(pdf_path)
    try:
        return doc[page_no - 1].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()


def transcribe_page(png: bytes) -> str:
    """一页 → 文字。走项目统一的视觉端点，并落账。"""
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    out = llm_ask_vision(_PROMPT, url)
    # OCR 是这条链路里最费钱的一步（按页计费），不落账就又是一笔糊涂账 ——
    # 复核那边刚补上，这里不能再欠一笔。messages 只记提示词不记图：
    # 一页 PNG 的 base64 有几百 KB，写进 JSONL 会让日志迅速失控。
    telemetry.log_llm_call("ocr", [{"role": "user", "content": _PROMPT}], out,
                           model=settings.vision_model)
    return out


def llm_ask_vision(prompt: str, url: str) -> str:
    """薄封装，方便用例替换掉真实调用。"""
    from backend.core import llm as llm_mod

    return llm_mod.ask_vision(prompt, url)


def transcribe_pdf(pdf_path: str, max_pages: int = MAX_OCR_PAGES) -> tuple[list[str], dict]:
    """整份转写。返回 (逐页文本, 统计)。

    统计里的 `truncated` / `failed_pages` 是要给教务看的：一份资料只进来一半，
    而列表上显示"已入库"，那是最难查的一类问题。
    """
    import fitz

    doc = fitz.open(pdf_path)
    total = doc.page_count
    doc.close()

    limit = min(total, max(1, max_pages))
    pages: list[str] = []
    failed = 0
    for no in range(1, limit + 1):
        try:
            pages.append(transcribe_page(render_page(pdf_path, no)).strip())
        except Exception:  # noqa: BLE001 —— 一页转不出来不该让整份作废
            failed += 1
            pages.append("")
    return pages, {"total_pages": total, "ocr_pages": limit,
                   "failed_pages": failed, "truncated": limit < total}


def ocr_chunks(pdf_path: str, name: str, doc_type: str, course: str,
               max_pages: int = MAX_OCR_PAGES) -> tuple[list[Chunk], dict]:
    """扫描件 → 分块。页码随块保留，引用里才能给出"第 N 页"。"""
    pages, stat = transcribe_pdf(pdf_path, max_pages)
    if not any(p.strip() for p in pages):
        return [], stat
    return chunk_pages(pages, name, doc_type=doc_type, course=course), stat

"""CourseLoop 结构化分块解析器。

按文档层级切分教材：第X章 -> X.Y(.Z) 小节，每个 chunk 携带来源/章节/页码，
用于检索后向学生标注「参考了教材第几章 / 第几页」。已适配《Hello 算法》：
    - 章标题支持阿拉伯数字（第2章）与中文数字（第二章）；
    - 过滤 www.hello-algo.com 页脚行。
另保留固定长度+重叠切分（chunk_fixed_overlap），供排版乱的课件/OCR 文档使用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import fitz  # PyMuPDF

CN_NUM = "一二三四五六七八九十百零两"
# 章：兼容「第2章」「第 二 章」「第0 章前言」；英文教材「Chapter 3」
RE_CHAPTER = re.compile(rf"^第\s*[0-9{CN_NUM}]+\s*章")
RE_CHAPTER_EN = re.compile(r"^Chapter\s+\d+\b", re.I)
# 小节：如 "2.1"、"2.1.3"，后跟标题词（中英通用）
RE_SECTION = re.compile(r"^(\d+(?:\.\d+){1,3})(\s+\S.*)?$")
RE_TOC = re.compile(r"\.{4,}\s*\d+\s*$")           # 目录点线行
RE_PAGE_NO = re.compile(r"^\s*\d{1,4}\s*$")         # 纯页码行
# 页脚：hello-algo.com（含普通连字符与 U+2011 非断连字符）
RE_FOOTER = re.compile(r"hello[\-‑]algo\.com", re.I)

MIN_CHARS = 120
MAX_CHARS = 1200
OVERLAP = 120

FIXED_CHUNK_SIZE = 1000
FIXED_OVERLAP = 200


@dataclass
class Chunk:
    text: str
    source: str
    page_start: int
    page_end: int
    chapter: str = ""
    section: str = ""
    doc_type: str = ""
    chunk_index: int = 0
    course: str = ""            # 所属科目（408 四门之一），检索按此隔离，杜绝跨课串味

    @property
    def title_path(self) -> str:
        return " / ".join(p for p in (self.chapter, self.section) if p)

    def to_metadata(self) -> dict:
        return {
            "source": self.source,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "chapter": self.chapter,
            "section": self.section,
            "title_path": self.title_path,
            "doc_type": self.doc_type,
            "chunk_index": self.chunk_index,
            "course": self.course,
        }


@dataclass
class _Line:
    text: str
    page: int


def _extract_lines(pdf_path: str) -> list[_Line]:
    doc = fitz.open(pdf_path)
    lines: list[_Line] = []
    for pno in range(len(doc)):
        for raw in doc[pno].get_text().splitlines():
            s = raw.strip()
            if not s:
                continue
            if RE_TOC.search(s) or RE_PAGE_NO.match(s) or RE_FOOTER.search(s):
                continue
            lines.append(_Line(text=s, page=pno + 1))
    doc.close()
    return lines


def _is_heading(line: str) -> tuple[str, str] | None:
    if len(line) > 48 or line.endswith(("。", "；", "，", "：", ":", "、", ".", ",")):
        return None
    if RE_CHAPTER.match(line) or RE_CHAPTER_EN.match(line):
        return "chapter", line
    m = RE_SECTION.match(line)
    if m and m.group(2) and m.group(2).strip():
        return "section", line
    return None


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """不依赖换行的兜底切分：在句末/换行处断，断不到就按长度硬断。

    从 ``chunk_fixed_overlap`` 抽出来共用。按段落切分（``_split_long``）遇到
    "一段本身就超长"时会原样吐出，而 PDF 里整页连成一片没有换行是常见的 ——
    那时"按段落切"等于没切，必须有一条不依赖任何分隔符的退路。
    """
    n = len(text)
    out: list[str] = []
    breakers = "。！？\n.!?"
    start = 0
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[max(start, end - 100):end]
            cut = max(window.rfind(c) for c in breakers)
            if cut != -1:
                end = max(start, end - 100) + cut + 1
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    paras = [p for p in re.split(r"\n+", text) if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        # 单独一段就超长时，先把它自己切开 —— 否则它会原样进 buf 再原样吐出，
        # 「切分」这个函数对这种输入等于没做事，而没有任何迹象表明它没做事
        pieces = _hard_split(p, MAX_CHARS, OVERLAP) if len(p) > MAX_CHARS else [p]
        for piece in pieces:
            if len(buf) + len(piece) + 1 > MAX_CHARS and buf:
                out.append(buf.strip())
                buf = buf[-OVERLAP:] + "\n" + piece
            else:
                buf = (buf + "\n" + piece) if buf else piece
    if buf.strip():
        out.append(buf.strip())
    return out


def parse_structured_pdf(pdf_path: str, source: str, doc_type: str = "textbook",
                         course: str = "") -> list[Chunk]:
    """解析文本型 PDF，返回结构化 chunk（按章/小节切，带页码）。"""
    lines = _extract_lines(pdf_path)
    raw_chunks: list[Chunk] = []
    cur_chapter = cur_section = ""
    body: list[str] = []
    page_start = lines[0].page if lines else 1
    page_end = page_start

    def flush():
        nonlocal body, page_start, page_end
        text = "\n".join(body).strip()
        if text:
            raw_chunks.append(Chunk(text=text, source=source, page_start=page_start,
                                    page_end=page_end, chapter=cur_chapter, section=cur_section))
        body = []

    for ln in lines:
        head = _is_heading(ln.text)
        if head:
            level, title = head
            flush()
            if level == "chapter":
                cur_chapter, cur_section = title, ""
            else:
                cur_section = title
            page_start = page_end = ln.page
            body = [title]
        else:
            if not body:
                page_start = ln.page
            body.append(ln.text)
            page_end = ln.page
    flush()

    chunks = _postprocess(raw_chunks)
    for i, c in enumerate(chunks):
        c.doc_type = doc_type
        c.chunk_index = i
        c.course = course
    return chunks


def chunk_pages(pages: list[str], source: str, doc_type: str = "courseware", course: str = "",
                chunk_size: int = FIXED_CHUNK_SIZE, label: str = "第 {a}~{b} 页") -> list[Chunk]:
    """按「页」聚合成块，保住页码。

    课件（幻灯片导出的 PDF / PPTX）每页字很少，一页一块会碎、检索命中率低；而
    ``chunk_fixed_overlap`` 把全书拼成一根长字符串再切，页码只能填 0 —— 引用就指不回
    原始页，人工核对无从下手。这里折中：按页顺序累加，超过 chunk_size 就断开，
    记录该块覆盖的首末页。``pages`` 的下标即页序（从 1 开始计）。
    """
    chunks: list[Chunk] = []
    buf: list[str] = []
    start_page = 0
    idx = 0

    def flush(end_page: int) -> None:
        nonlocal buf, start_page, idx
        text = re.sub(r"\n{2,}", "\n", "\n".join(buf)).strip()
        if not text:
            buf = []
            return
        # 必须再切一道：本函数按「页」累加，超过 chunk_size 才断开 —— 而这个判据
        # 只在页与页之间生效，**从不切开单独一页**。整份文档导成一张超长页的 PDF
        # （计网有 5 份，运输层/应用层/链路层等核心章节都在内）因此各自只出 1 块，
        # 7800 字符一块在 BM25 下什么词都能沾一点，排名近乎随机，引用还只标"第1~1页"。
        sec = label.format(a=start_page, b=end_page)
        for i, piece in enumerate(_split_long(text)):
            chunks.append(Chunk(text=piece, source=source, page_start=start_page,
                                page_end=end_page, doc_type=doc_type,
                                section=(f"{sec} (续{i})" if i else sec),
                                chunk_index=idx, course=course))
            idx += 1
        buf = []

    size = 0
    for page_no, raw in enumerate(pages, 1):
        text = (raw or "").strip()
        if not text:
            continue
        if not buf:
            start_page = page_no
        buf.append(text)
        size = sum(len(x) for x in buf)
        if size >= chunk_size:
            flush(page_no)
            size = 0
    if buf:
        flush(len(pages))
    return chunks


def _postprocess(chunks: list[Chunk]) -> list[Chunk]:
    merged: list[Chunk] = []
    for c in chunks:
        if merged and len(c.text) < MIN_CHARS and merged[-1].chapter == c.chapter:
            prev = merged[-1]
            prev.text += "\n" + c.text
            prev.page_end = c.page_end
            if not prev.section:
                prev.section = c.section
        else:
            merged.append(c)

    final: list[Chunk] = []
    for c in merged:
        pieces = _split_long(c.text)
        if len(pieces) == 1:
            final.append(c)
        else:
            for i, piece in enumerate(pieces):
                final.append(Chunk(text=piece, source=c.source, page_start=c.page_start,
                                   page_end=c.page_end, chapter=c.chapter,
                                   section=(f"{c.section} (续{i})" if i else c.section)))
    return final


# --------- 固定长度 + 重叠切分（课件/排版乱文档备用） ---------
def chunk_fixed_overlap(text: str, source: str, doc_type: str = "courseware",
                        course: str = "",
                        chunk_size: int = FIXED_CHUNK_SIZE, overlap: int = FIXED_OVERLAP) -> list[Chunk]:
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return [Chunk(text=piece, source=source, page_start=0, page_end=0,
                  section=f"片段 {idx}", doc_type=doc_type, chunk_index=idx, course=course)
            for idx, piece in enumerate(_hard_split(text, chunk_size, overlap))]


# --------- 按标题切分（Word / 纯文本：没有页码，标题路径是唯一的定位物） ---------
def chunk_by_heading(blocks: list[tuple[int, str]], source: str, doc_type: str = "",
                     course: str = "", chunk_size: int = FIXED_CHUNK_SIZE) -> list[Chunk]:
    """按标题层级聚合成块。``blocks`` 是 (标题级别, 文本)，级别 0 表示正文段落。

    为什么不直接用 ``chunk_fixed_overlap``：Word 和 txt 没有页码，``page_start``
    只能填 0，引用上不会出现"第 87 页"。那时 ``title_path`` 就是这条引用**唯一**
    的定位物 —— 而固定长度切分给出的是"片段 3"，人工核对时等于没有。

    标题文本会作为所在块的首行一起进正文。FTS 表里 chapter/section 是 UNINDEXED
    的（检索只打 text 列），不把标题写进 text，「数据链路层」这种查询就检不到那一节。
    """
    chunks: list[Chunk] = []
    chapter = section = ""
    buf: list[str] = []
    idx = 0

    def flush() -> None:
        nonlocal buf, idx
        text = re.sub(r"\n{2,}", "\n", "\n".join(buf)).strip()
        if text:
            chunks.append(Chunk(text=text, source=source, page_start=0, page_end=0,
                                chapter=chapter, section=section, doc_type=doc_type,
                                chunk_index=idx, course=course))
            idx += 1
        buf = []

    for level, raw in blocks:
        text = (raw or "").strip()
        if not text:
            continue
        if level:
            # 标题落在块的开头而不是结尾：一个块的内容属于它上面那个标题
            flush()
            if level == 1:
                chapter, section = text, ""
            else:
                section = text
            buf.append(text)
            continue
        buf.append(text)
        if sum(len(x) for x in buf) >= chunk_size:
            flush()
    flush()
    return chunks

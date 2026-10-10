"""从 data/raw 直接建 FTS5 检索库：分块页码与表结构一致性。

钉两件容易静默出错的事：
1. `chunk_pages` 必须把页码带出来 —— 课件走 `chunk_fixed_overlap` 时 page_start 恒为 0，
   引用就指不回原始页，人工核对无从下手；
2. 新建的表结构必须与 `build_light_rag.py`、上传接口一字不差 —— 列顺序/UNINDEXED
   一变，`LightRagEngine.retrieve()` 的 `bm25(chunks)` 排序就会静默失效。
"""
from __future__ import annotations

import sqlite3

from backend.rag.chunking import MAX_CHARS, chunk_fixed_overlap, chunk_pages
from backend.rag.indexer import FTS_DDL, _row


def test_chunk_pages_keeps_page_range():
    pages = ["第一页" * 60, "第二页" * 60, "第三页" * 60]
    chunks = chunk_pages(pages, "课件A", doc_type="courseware", course="co", chunk_size=150)
    assert chunks, "不该切成空"
    assert all(c.page_start >= 1 for c in chunks), "页码必须从 1 起，不能留 0"
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == len(pages)
    # 页码单调不倒退，块间不重叠错位
    for prev, cur in zip(chunks, chunks[1:]):
        assert cur.page_start >= prev.page_end


def test_chunk_pages_skips_blank_pages():
    chunks = chunk_pages(["", "   ", "有内容的一页"], "课件B", chunk_size=10)
    assert len(chunks) == 1
    assert chunks[0].page_start == 3, "空页不该占页码，块要指到真正有内容的那页"


def test_chunk_pages_labels_slides_differently():
    c = chunk_pages(["幻灯片正文"], "课件C", label="第 {a}~{b} 张幻灯片", chunk_size=5)[0]
    assert c.section == "第 1~1 张幻灯片"
    assert c.title_path.endswith("张幻灯片")


def test_one_giant_page_is_split_into_readable_chunks():
    """整份文档导成一张超长页的 PDF，不能只出 1 块。

    chunk_pages 按「页」累加、超过 chunk_size 才断开 —— 这个判据只在页与页之间
    生效，从不切开单独一页。计网有 5 份这样的文件（运输层/应用层/链路层/网络层/
    计算机网络和因特网），每份原本只出 1 块，最长的 8031 字符。一块这么大的文本
    在 BM25 下什么词都能沾一点，排名近乎随机，而引用只标得出「第 1~1 页」。
    """
    page = "\n".join(f"第{i}行 拥塞控制与滑动窗口" for i in range(400))
    chunks = chunk_pages([page], "运输层", course="net")

    assert len(chunks) > 1, "单页超长必须切开"
    assert max(len(c.text) for c in chunks) <= MAX_CHARS + 200, "切完不能还有超大块"
    assert all(c.page_start == 1 and c.page_end == 1 for c in chunks), "页码仍是那一页"
    assert len({c.chunk_index for c in chunks}) == len(chunks), "chunk_index 不能撞"


def test_splitting_survives_a_page_without_any_line_breaks():
    """连成一片、没有任何换行的一页 —— 按段落切等于没切，要有不依赖分隔符的退路。"""
    chunks = chunk_pages(["数据链路层" * 1200], "某课件", course="net")
    assert len(chunks) > 1
    assert max(len(c.text) for c in chunks) <= MAX_CHARS + 200


def test_normal_multi_page_documents_are_unchanged():
    """回归：正常的多页课件不该因为这个改动被切碎。"""
    chunks = chunk_pages(["这一页只有一点点字"] * 20, "某课件", course="net")
    assert len(chunks) == 1, "20 个短页仍应聚成 1 块"


def test_fixed_overlap_still_splits_after_sharing_the_splitter():
    """chunk_fixed_overlap 改成复用同一个切分核心，行为不能变。"""
    chunks = chunk_fixed_overlap("这是一个句子。" * 400, "某文档")
    assert len(chunks) > 1
    assert all(c.page_start == 0 for c in chunks), "这条路本来就没有页码"
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_rows_match_table_columns():
    chunks = chunk_pages(["一些正文内容"], "课件D", doc_type="courseware", course="net", chunk_size=5)
    conn = sqlite3.connect(":memory:")
    conn.execute(FTS_DDL)
    conn.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)", [_row(c) for c in chunks])
    row = conn.execute("SELECT course, doc_type, source, page_start FROM chunks").fetchone()
    assert row == ("net", "courseware", "课件D", 1)


def test_schema_shared_by_every_writer():
    """所有写索引的地方必须写同一张表：共用同一个 DDL 常量，而不是各抄一份。

    常量归 backend/rag/indexer.py（增量写入与去重也在那里），两个构建脚本
    与上传接口都从它取。列顺序/UNINDEXED 一变，`LightRagEngine.retrieve()`
    的 `bm25(chunks)` 排序就会静默失效。
    """
    from scripts import build_fts_from_raw, build_light_rag

    assert build_light_rag.FTS_DDL is FTS_DDL
    assert build_fts_from_raw.indexer.FTS_DDL is FTS_DDL
    conn = sqlite3.connect(":memory:")
    conn.execute(FTS_DDL)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)")]
    assert cols == ["text", "course", "doc_type", "source", "title_path",
                    "page_start", "page_end", "chunk_index"]

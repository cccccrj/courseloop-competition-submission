"""FTS5 检索索引的增量写入与按内容去重。

从 `scripts/build_fts_from_raw.py` 的整轮重建里抽出来，让"传一份课件就只索引这一份"
成为可能 —— 原来那个脚本只能整轮跑（或按文件名 resume），没法给上传接口用。

## 为什么要按内容哈希去重

`docs/23-检索评测报告` §3 查明：四门里计算机网络的引用命中率只有 70%（其余 90~100%），
根因不是检索算法，是**语料里有重复**：

- 计算机网络有两套重叠教材 —— 121 块的合订本，和按章拆分的课件。同一段内容进了两次，
  合订本那份在 BM25 下压过按章文件，于是命中的是"对的内容、错的出处"；
- 计组的《综合练习2015级》(1)(2)(3) 干脆就是同一个文件的四个副本。

重复不会让检索报错，只会让引用指向一个不该被引的地方，而这正是"教材依据"这条
产品承诺的落点。所以去重放在**写索引**这一层，而不是查询时再排重 ——
查询时排重要每次都付代价，且改变不了"排名被压过"这件事。

## 哈希存在单独的表里，不进 FTS 的列

FTS5 的 UNINDEXED 列建不了索引，逐条查重就得全表扫。更要紧的是：给虚拟表加列
需要重建整张表，而线上已经有一份 2585 块的索引。`chunk_hashes` 是一张普通表，
主键天然有索引，对既有索引库只需一次性回填。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from backend.rag.chunking import Chunk

FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
    "text,course UNINDEXED,doc_type UNINDEXED,source UNINDEXED,title_path UNINDEXED,"
    "page_start UNINDEXED,page_end UNINDEXED,chunk_index UNINDEXED,tokenize='trigram')"
)

HASH_DDL = """
CREATE TABLE IF NOT EXISTS chunk_hashes (
    hash TEXT PRIMARY KEY,
    course TEXT,
    source TEXT,
    chunk_index INTEGER
);
CREATE TABLE IF NOT EXISTS build_meta(key TEXT PRIMARY KEY, value TEXT);
"""

_WS = re.compile(r"\s+")


def chunk_hash(course: str, text: str) -> str:
    """按 (课程, 归一化文本) 取哈希。

    只按文本会让两门课里偶然相同的一段被当成重复；而查询本来就按 course 过滤，
    跨课程的"重复"不会互相压排名，没有去重的必要。归一化只压空白 ——
    再激进（去标点、转小写）会把"同一句话的两种排版"和"真的两段话"混在一起。

    分隔符用 `|`：course_id 的取值域是 [a-z0-9_-]，不含它，所以拼出来不会歧义。
    """
    normalized = _WS.sub(" ", (text or "")).strip()
    return hashlib.sha256(f"{course}|{normalized}".encode("utf-8")).hexdigest()


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute(FTS_DDL)
    conn.executescript(HASH_DDL)
    return conn


def backfill_hashes(conn: sqlite3.Connection) -> int:
    """给建于去重之前的索引库补哈希。返回补了多少条。

    只在 chunks 有数据而 chunk_hashes 为空时做 —— 那正是"老索引第一次遇上新代码"
    的形状。不做的话，第一次增量入库会把已经在库里的内容当成新内容再写一遍。
    """
    if conn.execute("SELECT 1 FROM chunk_hashes LIMIT 1").fetchone():
        return 0
    rows = conn.execute("SELECT rowid, text, course, source, chunk_index FROM chunks").fetchall()
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO chunk_hashes(hash,course,source,chunk_index) VALUES(?,?,?,?)",
        [(chunk_hash(r["course"] or "", r["text"] or ""), r["course"] or "",
          r["source"] or "", r["chunk_index"]) for r in rows])
    conn.commit()
    return len(rows)


def _row(c: Chunk) -> tuple:
    m = c.to_metadata()
    return (c.text or "", m.get("course", ""), m.get("doc_type", ""),
            m.get("source", "未知来源"), m.get("title_path", ""),
            int(m.get("page_start", 0) or 0), int(m.get("page_end", 0) or 0),
            int(m.get("chunk_index", -1) or -1))


def write_chunks(conn: sqlite3.Connection, chunks: list[Chunk]) -> dict:
    """把分块写进索引，按内容哈希跳过重复。返回 {"written","duplicate"}。

    同一批里的重复也要挡：一份文件内部就可能有重复页（扫描件的空白页、
    课件的过渡页），它们在同一次调用里还没进库，光查库是查不到的。
    """
    seen_in_batch: set[str] = set()
    rows, hashes, duplicate = [], [], 0
    for c in chunks:
        m = c.to_metadata()
        course = m.get("course", "")
        h = chunk_hash(course, c.text or "")
        if h in seen_in_batch:
            duplicate += 1
            continue
        if conn.execute("SELECT 1 FROM chunk_hashes WHERE hash=?", (h,)).fetchone():
            duplicate += 1
            continue
        seen_in_batch.add(h)
        rows.append(_row(c))
        hashes.append((h, course, m.get("source", "未知来源"),
                       int(m.get("chunk_index", -1) or -1)))
    if rows:
        conn.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT OR IGNORE INTO chunk_hashes(hash,course,source,chunk_index) VALUES(?,?,?,?)",
            hashes)
        conn.commit()
    return {"written": len(rows), "duplicate": duplicate}


def index_one(conn: sqlite3.Connection, item: dict, raw_dir: str | Path) -> dict:
    """解析并索引一份文件。item 形状与 ingest.discover_corpus 一致。

    解析失败**不吞异常**，交给调用方决定怎么记 —— 上传接口要把失败原因原样
    显示给教务（"这份 PDF 没有文本层"和"这份文件损坏"是两种不同的处置）。
    """
    from backend.rag.ingest import parse_one

    chunks = parse_one(item, Path(raw_dir))
    stat = write_chunks(conn, chunks)
    stat["parsed"] = len(chunks)
    stat["source"] = item.get("name", "")
    return stat


def stamp_meta(conn: sqlite3.Connection, builder: str, source: str) -> None:
    import time
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.execute("DELETE FROM build_meta")
    conn.executemany("INSERT INTO build_meta VALUES(?,?)",
                     [("source", str(source)), ("builder", builder),
                      ("documents", str(total)),
                      ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))])
    conn.commit()


def delete_source(conn: sqlite3.Connection, course: str, doc_type: str,
                  source: str) -> dict:
    """把一份资料的分块从索引里删干净。返回 {"deleted"}。

    定位用 (course, doc_type, source) 三元组而不是只用 source：source 是不带
    后缀的文件名，同一门课下 `courseware/第3章.pdf` 与 `textbook/第3章.pdf`
    会撞名，只按 source 删会顺带删掉另一份。

    哈希按**被删分块的实际内容**反算，不按 (course, source) 去 `chunk_hashes`
    里匹配 —— 那张表存的 source 是"最先写进这块内容的那份文件"，与本次要删的
    文件不一定是同一个。

    一个已知的边界：A、B 两份文件含同一段内容时，只有 A 的那份进了 chunks
    （B 的被判重跳过）。删掉 A，这段内容就一并从索引里消失，尽管 B 还在盘上。
    补救是重传 B —— 这比"删了却还检索得到、且出处指着一份不存在的文件"要好。
    """
    rows = conn.execute(
        "SELECT rowid, text FROM chunks WHERE course=? AND doc_type=? AND source=?",
        (course, doc_type, source)).fetchall()
    if not rows:
        return {"deleted": 0}
    hashes = [(chunk_hash(course, r["text"] or ""),) for r in rows]
    conn.executemany("DELETE FROM chunks WHERE rowid=?", [(r["rowid"],) for r in rows])
    conn.executemany("DELETE FROM chunk_hashes WHERE hash=?", hashes)
    conn.commit()
    return {"deleted": len(rows)}

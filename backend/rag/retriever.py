"""检索器：bge-m3 向量召回 -> bge-reranker 精排 -> 带页码引用。

    engine = RagEngine()
    hits = engine.retrieve("什么是尾递归")      # 纯检索
    text, hits = engine.answer("...")           # 检索 + LLM（需配置 LLM）
无 LLM 时降级为纯检索；向量库为空时 retrieve 返回 []。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.config import settings
from backend.core import llm as llm_mod
from backend.core.courses import get_course
from backend.core.device import load_with_fallback
from backend.agents.prompts import build_grounded_qa_system


@dataclass
class Hit:
    text: str
    score: float
    source: str
    page_start: int
    page_end: int
    title_path: str
    doc_type: str = ""
    chunk_index: int = -1
    course: str = ""

    def citation(self) -> str:
        """《Hello 算法》 第2章复杂度分析 / 2.1，第 24 页"""
        path = f" {self.title_path}" if self.title_path else ""
        if self.page_start and self.page_end:
            page = (f"，第 {self.page_start} 页" if self.page_start == self.page_end
                    else f"，第 {self.page_start}-{self.page_end} 页")
        else:
            page = ""
        return f"《{self.source}》{path}{page}"

    def to_dict(self) -> dict:
        # 前端只展示足以核对结论的短证据，不把整块（通常约 1,000 字）直接暴露。
        # 检索分（score）是排序用的内部数值，不下发。
        # 压平空白后截断，既便于比赛现场阅读，也避免课件换行把卡片撑散。
        excerpt = " ".join((self.text or "").split())
        if len(excerpt) > 220:
            excerpt = excerpt[:220].rstrip() + "…"
        return {"source": self.source, "title_path": self.title_path,
                "page_start": self.page_start, "page_end": self.page_end,
                "doc_type": self.doc_type, "excerpt": excerpt}


# 检索结果注入 LLM 时的系统提示（教材强溯源，按科目参数化）
def build_rag_system(course) -> str:
    # Keep the local grounded-QA LoRA on exactly the same system contract used
    # for synthesis and held-out evaluation. ``course`` remains in the user
    # message, matching the training distribution.
    return build_grounded_qa_system()


_SYS_PROMPT = build_rag_system(get_course("ds-algo"))  # 兼容：默认数据结构


class _AnswerMixin:
    def build_context(self, hits: list[Hit]) -> str:
        return "\n\n".join(f"[{i}] 来源：{h.citation()}\n{h.text}" for i, h in enumerate(hits, 1))

    def build_grounded_user(self, query: str, hits: list[Hit], course: str) -> str:
        blocks = []
        for index, hit in enumerate(hits, 1):
            blocks.append(f"source_id: {index}\n课程: {course}\n材料：\n{hit.text}")
        material = "\n\n".join(blocks)
        return f"{material}\n\n问题：{query}" if material else f"课程: {course}\n材料：\n\n问题：{query}"

    @staticmethod
    def parse_grounded_answer(raw: str) -> str:
        """Preserve the public text contract while accepting the QA LoRA JSON."""
        text = (raw or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
        candidate = fenced.group(1) if fenced else text
        if not candidate.startswith("{"):
            start, end = candidate.find("{"), candidate.rfind("}")
            candidate = candidate[start:end + 1] if 0 <= start < end else ""
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return text
        if not isinstance(parsed, dict):
            return text
        if parsed.get("abstain") is True:
            return "教材中未提及"
        answer = parsed.get("answer")
        return str(answer).strip() if answer else text

    def answer(self, query: str, doc_types: list[str] | None = None,
               course: str = "ds-algo") -> tuple[str, list[Hit]]:
        hits = self.retrieve(query, doc_types=doc_types, course=course)
        if not settings.text_llm_ready:
            return ("（未配置 LLM，当前为纯检索模式，以下为检索到的教材片段。）", hits)
        messages = [{"role": "system", "content": build_rag_system(get_course(course))},
                    {"role": "user", "content": self.build_grounded_user(query, hits, course)}]
        raw = llm_mod.ask_grounded_qa(messages)
        return self.parse_grounded_answer(raw), hits


class RagEngine(_AnswerMixin):
    def __init__(self):
        self._embed_model = None
        self._index = None
        self._reranker = None
        self._device = settings.device

    @property
    def embed_model(self) -> Any:
        if self._embed_model is None:
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding
            self._embed_model, self._device = load_with_fallback(
                lambda d: HuggingFaceEmbedding(model_name=settings.embed_model_path,
                                               device=d, trust_remote_code=True),
                self._device, label="嵌入模型 bge-m3")
        return self._embed_model

    @property
    def index(self) -> Any:
        if self._index is None:
            import chromadb
            from llama_index.core import VectorStoreIndex
            from llama_index.vector_stores.chroma import ChromaVectorStore
            client = chromadb.PersistentClient(path=settings.chroma_dir)
            collection = client.get_or_create_collection(settings.chroma_collection)
            if collection.count() == 0:
                raise RuntimeError("向量库为空，请先运行：python -m backend.rag.ingest")
            vector_store = ChromaVectorStore(chroma_collection=collection)
            self._index = VectorStoreIndex.from_vector_store(vector_store, embed_model=self.embed_model)
        return self._index

    @property
    def reranker(self) -> Any:
        if self._reranker is None:
            from llama_index.core.postprocessor import SentenceTransformerRerank
            self._reranker, self._device = load_with_fallback(
                lambda d: SentenceTransformerRerank(model=settings.reranker_model_path,
                                                    top_n=settings.rerank_top_n, device=d),
                self._device, label="重排模型 bge-reranker")
        return self._reranker

    def has_corpus(self) -> bool:
        try:
            import chromadb
            client = chromadb.PersistentClient(path=settings.chroma_dir)
            return client.get_or_create_collection(settings.chroma_collection).count() > 0
        except Exception:  # noqa: BLE001
            return False

    def retrieve(self, query: str, doc_types: list[str] | None = None,
                 top_n: int | None = None, course: str | None = None) -> list[Hit]:
        rerank_n = top_n or settings.rerank_top_n
        k = max(settings.top_k, rerank_n * 4) if doc_types else max(settings.top_k, rerank_n * 2)
        nodes = self.index.as_retriever(similarity_top_k=k).retrieve(query)
        if doc_types:
            nodes = [n for n in nodes if n.node.metadata.get("doc_type") in doc_types]
        if course:
            # 过渡期：无 course 字段的旧 chunk 视为匹配（默认到本 course），有则必须一致，杜绝串味
            nodes = [n for n in nodes if n.node.metadata.get("course", course) == course]
        reranker = self.reranker
        reranker.top_n = rerank_n
        nodes = reranker.postprocess_nodes(nodes, query_str=query)
        hits: list[Hit] = []
        for n in nodes:
            m = n.node.metadata
            hits.append(Hit(text=n.node.get_content(),
                            score=float(n.score) if n.score is not None else 0.0,
                            source=m.get("source", "未知来源"),
                            page_start=int(m.get("page_start", 0)),
                            page_end=int(m.get("page_end", 0)),
                            title_path=m.get("title_path", ""),
                            doc_type=m.get("doc_type", ""),
                            chunk_index=int(m.get("chunk_index", -1)),
                            course=m.get("course", "")))
        return hits

class LightRagEngine(_AnswerMixin):
    """SQLite FTS5 trigram 检索器；运行时不加载 Torch、嵌入模型或 Chroma。"""

    def __init__(self, path: str | None = None):
        self.path = Path(path or settings.rag_fts_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def has_corpus(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            with self._connect() as conn:
                return conn.execute("SELECT EXISTS(SELECT 1 FROM chunks LIMIT 1)").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    @staticmethod
    def _match_query(query: str) -> str:
        compact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", query or "")
        if len(compact) < 3:
            return ""
        grams = []
        for i in range(len(compact) - 2):
            gram = compact[i:i + 3]
            if gram not in grams:
                grams.append(gram)
        return " OR ".join(f'"{g}"' for g in grams[:32])

    def retrieve(self, query: str, doc_types: list[str] | None = None,
                 top_n: int | None = None, course: str | None = None) -> list[Hit]:
        if not self.has_corpus() or not (query or "").strip():
            return []
        limit = max(1, min(20, int(top_n or settings.rerank_top_n)))
        clauses, params = [], []
        match = self._match_query(query)
        if match:
            clauses.append("chunks MATCH ?")
            params.append(match)
        else:
            clauses.append("text LIKE ?")
            params.append(f"%{(query or '').strip()}%")
        if course:
            clauses.append("course=?")
            params.append(course)
        if doc_types:
            clauses.append(f"doc_type IN ({','.join('?' * len(doc_types))})")
            params.extend(doc_types)
        rank = "bm25(chunks)" if match else "0"
        sql = ("SELECT text,course,doc_type,source,title_path,page_start,page_end,chunk_index,"
               f"{rank} AS rank FROM chunks WHERE {' AND '.join(clauses)} "
               f"ORDER BY rank, rowid LIMIT ?")
        params.append(limit)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        return [Hit(text=r["text"], score=round(max(0.0, -float(r["rank"] or 0)), 6),
                    source=r["source"] or "未知来源", page_start=int(r["page_start"] or 0),
                    page_end=int(r["page_end"] or 0), title_path=r["title_path"] or "",
                    doc_type=r["doc_type"] or "", chunk_index=int(r["chunk_index"] or -1),
                    course=r["course"] or "") for r in rows]


class DisabledRagEngine(_AnswerMixin):
    """显式关闭检索时的空实现，避免意外触发向量模型加载。"""

    @staticmethod
    def has_corpus() -> bool:
        return False

    @staticmethod
    def retrieve(query: str, doc_types: list[str] | None = None,
                 top_n: int | None = None, course: str | None = None) -> list[Hit]:
        return []


# 进程内缓存（向量模型只加载一次），**按索引路径分桶**。
#
# 这里原先是一个全局单例 `_engine`：第一次构造出来之后，即便 rag_mode 或索引路径
# 变了也照旧返回旧引擎。测试里换 settings.rag_fts_path 指向另一份索引、
# 或者重建索引后想立刻检索，拿到的都还是上一个 —— 不报错，只是检索结果不对。
# 按路径分桶后既解决这个问题，又保留了"同一份索引只构造一次"的加载成本优化。
_engines: dict[tuple[str, str], RagEngine | LightRagEngine | DisabledRagEngine] = {}


def _engine_key(mode: str) -> tuple[str, str]:
    if mode == "fts5":
        return mode, settings.rag_fts_path
    if mode == "off":
        return mode, ""
    return mode, settings.chroma_dir


def get_engine() -> RagEngine | LightRagEngine | DisabledRagEngine:
    mode = (settings.rag_mode or "vector").strip().lower()
    key = _engine_key(mode)
    engine = _engines.get(key)
    if engine is None:
        if mode == "fts5":
            engine = LightRagEngine(path=key[1])
        elif mode == "off":
            engine = DisabledRagEngine()
        else:
            engine = RagEngine()
        _engines[key] = engine
    return engine


def reset_engines() -> None:
    """清空引擎缓存。测试与「重建索引后立刻检索」的场景需要它。"""
    _engines.clear()

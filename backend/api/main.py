"""CourseLoop FastAPI 入口（骨架）。

首版端点：
    GET  /health        健康检查 + LLM 就绪状态
    POST /chat/stream    学习大厅对话（POST-SSE 流式，DS&A 助教）
    GET  /sessions       会话读回（列表 / 详情 / 删除，见 api/session_routes.py）
LLM 未配置时降级为「兜底提示流」，前端仍可完整演示交互与薄荷绿界面。
后续按 docs/02 功能设计逐步加 /explain /solve /diagnose /practice /records /review /teaching。
"""
from __future__ import annotations

import json

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.core.config import settings
from backend.core import llm as llm_mod
from backend.core import db
from backend.core import telemetry
from backend.core import errors
from backend.core.security import current_identity
from backend.api.auth import router as auth_router
from backend.api.core_routes import router as core_router
from backend.api.admin_routes import router as admin_router
from backend.api.session_routes import router as session_router
from backend.agents.prompts import build_hall_rag_system, build_tutor_system
from backend.core.sanitize import clean_answer
from backend.core.courses import get_course

# 交付产品不对外提供接口文档（/docs、/redoc、/openapi.json 会把全部路由和字段名摊开）
app = FastAPI(title="CourseLoop API", version="1.0.0",
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)
app.include_router(auth_router)
app.include_router(core_router)
app.include_router(admin_router)
app.include_router(session_router)


# 没被路由自己接住的异常：此前 FastAPI 回的是裸 500，前端只能说"服务暂时不可用"，
# 运维也没有线索定位是哪一次。现在统一记日志、带错误编号返回（docs/35 P0-2）。
@app.exception_handler(llm_mod.LLMNotConfigured)
async def _llm_not_configured(request: Request, exc: llm_mod.LLMNotConfigured):
    body = errors.report(exc, f"{request.method} {request.url.path}")
    return JSONResponse(status_code=503, content={"detail": body["message"], "error_id": body["error_id"]})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    body = errors.report(exc, f"{request.method} {request.url.path}")
    return JSONResponse(status_code=500, content={"detail": body["message"], "error_id": body["error_id"]})


@app.on_event("startup")
def _startup():
    db.init_db()


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _history_msgs(history: list[dict] | None, limit: int) -> list[dict]:
    """前端带回来的最近几轮对话。助教回答先清洗再回灌：老记录里如果存着
    「回答完毕」这类标记，原样喂回去模型会照着学，下一条回答又带上它。"""
    out = []
    for h in (history or [])[-limit:]:
        role, content = h.get("role"), h.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        if role == "assistant":
            content = clean_answer(content)
            if not content:
                continue
        out.append({"role": role, "content": content})
    return out


@app.get("/health")
def health():
    """公开的存活探针，不需要登录，所以只报"能不能用"。

    模型名、检索库名、提交号这些原先也在这里，任何人一个 curl 就能看到；
    交付产品不对外暴露，挪到了管理员接口 /admin/runtime。
    """
    return {
        "status": "ok",
        "llm_ready": settings.llm_ready and settings.text_llm_ready,
        # 前端拍照批改页据此在挂载时就给降级提示：postSSE 遇到非 2xx 只抛得出
        # "HTTP 503" 这种无意义文案，等学生选完图才报错体验太差。
        "vision_ready": settings.vision_ready,
    }


class ChatReq(BaseModel):
    message: str
    history: list[dict] = []
    course: str = "ds-algo"
    # 续上哪一次会话。0 = 新开一次。
    #
    # 原先每发一条消息就 create_session 一次 —— 一段十来轮的对话在库里散成十来个
    # 只有两行轮次的"会话"，既拼不回原样，也没法在列表里当成一次对话展示。
    # 前端拿到第一个 session 事件后回传这个 id，整段对话才落在同一行上。
    session_id: int = 0


def _rag_hits(query: str, course: str = "ds-algo"):
    """检索教材证据（按 course 隔离）。向量库为空 / 出错时返回 None（调用方降级为普通对话）。"""
    try:
        from backend.rag.retriever import get_engine
        engine = get_engine()
        if not engine.has_corpus():
            return None, None
        hits = engine.retrieve(query, doc_types=["textbook", "courseware"], top_n=3, course=course)
        return hits, engine
    except Exception as e:  # noqa: BLE001
        print(f"[rag] 检索失败，降级为普通对话：{e}")
        return None, None


@app.post("/chat/stream")
def chat_stream(req: ChatReq, ident: dict = Depends(current_identity)):
    """学习大厅对话：优先教材强溯源（RAG）→ 带引用作答；无语料/未配 LLM 时降级。"""

    # 会话留痕：续上或新建 learning_sessions + 写学生问句（无身份则 sid=0，save_turn 自动跳过）。
    # 传来的 session_id 必须验归属：它会经前端、日志、截图流出去，
    # 不校验就等于允许任何人往别人的会话里追加轮次。不归自己的一律当没传，另开一次。
    sid = 0
    if ident["username"]:
        try:
            if req.session_id:
                owned = db.get_session(req.session_id)
                if owned and owned.get("username") == ident["username"]:
                    sid = int(owned["id"])
            if not sid:
                sid = db.create_session(ident["username"], course_id=req.course, intent="chat")
            db.save_turn(sid, "student", req.message)
        except Exception:  # noqa: BLE001
            sid = 0

    def gen():
        # 先把会话 id 发出去：前端要拿它把后续几轮钉在同一次会话上，
        # 也要拿它在刷新后从 /sessions/{id} 把这段对话读回来。第一个事件就发，
        # 是因为 LLM 那一步随时可能失败，而"这次对话叫什么 id"不该跟着一起丢。
        if sid:
            yield _sse({"type": "session", "id": sid})
        course_obj = get_course(req.course)
        if settings.text_llm_ready:
            hits, engine = _rag_hits(req.message, course=req.course)
            # —— 教材强溯源分支 ——
            if hits:
                cites = []
                for i, h in enumerate(hits, 1):
                    c = {"type": "citation", "index": i, **h.to_dict()}
                    cites.append(c)
                    yield _sse(c)
                context = engine.build_context(hits)
                msgs = [{"role": "system", "content": build_hall_rag_system(course_obj)}]
                msgs.extend(_history_msgs(req.history, 6))
                msgs.append({"role": "user",
                             "content": f"教材参考资料：\n{context}\n\n问题：{req.message}\n\n请依据资料作答："})
                answer = ""
                try:
                    for kind, delta in llm_mod.ask_reasoner_stream(msgs):
                        if kind != "reasoning":
                            answer += delta
                        yield _sse({"type": "reasoning" if kind == "reasoning" else "token", "text": delta})
                    answer = clean_answer(answer)
                    yield _sse({"type": "final", "text": answer})
                    yield _sse({"type": "verify", "passed": True})  # 有教材证据支撑
                except Exception as e:  # noqa: BLE001
                    answer = clean_answer(answer)
                    yield _sse({"type": "error", **errors.report(e, "chat_stream", user=ident["username"])})
                # 只有真的答出来了才记"有依据"：半路失败的空回答不该带着 verified 读回来
                db.save_turn(sid, "assistant", answer,
                             structured_data=json.dumps({"citations": cites, "verified": bool(answer)},
                                                        ensure_ascii=False))
                # 埋点。大厅问答是全站最高频的交互，却一直是唯一没有埋点的 LLM 链路 ——
                # 于是 data/logs/llm_calls.jsonl 里看不到任何教材问答样本。
                # verification 记引用条数：有教材证据支撑才算 passed。
                telemetry.log_llm_call(
                    "grounded_qa", msgs, answer, parsed=None,
                    verification={"method": "citation", "passed": bool(cites),
                                  "details": f"命中教材片段 {len(cites)} 条"},
                    model=settings.reasoner_model)
                yield _sse({"type": "done"})
                return
            # —— 无语料命中：普通助教对话 ——
            msgs = [{"role": "system", "content": build_tutor_system(course_obj)}]
            msgs.extend(_history_msgs(req.history, 8))
            msgs.append({"role": "user", "content": req.message})
            answer = ""
            try:
                for kind, delta in llm_mod.ask_reasoner_stream(msgs):
                    if kind != "reasoning":
                        answer += delta
                    yield _sse({"type": "reasoning" if kind == "reasoning" else "token", "text": delta})
                answer = clean_answer(answer)
                yield _sse({"type": "final", "text": answer})
                # 没检索到教材依据的回答，要**明说**依据不足（docs/34 §2.6）。此前这一支
                # 不发 verify，气泡上什么标记都没有，看起来和有教材原文支撑的回答一样。
                if answer:
                    yield _sse({"type": "verify", "passed": False, "reason": "no_evidence"})
            except Exception as e:  # noqa: BLE001
                answer = clean_answer(answer)
                yield _sse({"type": "error", **errors.report(e, "chat_stream", user=ident["username"])})
            db.save_turn(sid, "assistant", answer,
                         structured_data=json.dumps({"verified": False} if answer else {},
                                                    ensure_ascii=False))
            # 无语料命中的普通助教对话：同样埋点，但没有确定性证据可判，
            # verification 留空（telemetry 会把 passed 记成 None，即"无法判定"）。
            telemetry.log_llm_call("grounded_qa", msgs, answer, parsed=None,
                                   model=settings.reasoner_model)
        else:
            # 未开通 AI 时的兜底回复。原先这里教学生去 .env 填 CL_LLM_* —— 那是写给部署者的，
            # 学生看了既看不懂也做不了（docs/34 §2.2）。部署者的线索在 /health 与 preflight 里。
            fallback = (
                f"你好，我是 CourseLoop 的 AI 助手（当前科目：{course_obj.short}）。\n\n"
                "> AI 问答暂未开通，这条是系统的固定回复，不是针对你问题的回答。\n\n"
                "现在可以先去「个性化练习」按知识点做题，判题、错题本和复习计划不依赖 AI 问答；"
                "开通后这里会给出带教材原文的讲解。"
            )
            for ch in fallback:
                yield _sse({"type": "token", "text": ch})
            yield _sse({"type": "verify", "passed": False})
            # 降级回复同样落库：不存的话，未配 LLM 时读回的会话只有学生的问句、
            # 没有任何回答，看着像是"这次对话丢了一半"。
            db.save_turn(sid, "assistant", fallback,
                         structured_data=json.dumps({"verified": False}, ensure_ascii=False))
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")

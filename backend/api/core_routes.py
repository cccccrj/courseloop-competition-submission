"""命题核心端点：解题 / 诊断 / 练习 / 复习 / TutorGraph 闭环。"""
from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.core import errors
from backend.core import llm as llm_mod
from backend.core.config import settings
from backend.core.courses import course_ids, get_course, is_valid_course
from backend.verification.unit_test_runner import run_python_tests
from backend.verification.numeric_checker import canonical_text, check_numeric
from backend.verification.rule_checker import check_concept


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

from backend.agents import solver as solver_mod
from backend.agents import error_diagnoser as diag_mod
from backend.agents import photo_grader as photo_mod
from backend.agents import practice_generator as pg_mod
from backend.agents import tutor_graph as tutor
from backend.agents import weekly_report as report_mod
from backend.core import db
from backend.core.security import current_identity
from backend.learning import classroom
from backend.learning import community
from backend.learning import mastery as mastery_mod
from backend.learning import mock_exam
from backend.learning import placement as placement_mod
from backend.learning import profile as profile_mod
from backend.learning import review_service
from backend.learning import study_budget
from backend.learning.error_taxonomy import (ERROR_TAXONOMY, MISCONCEPTION_TAGS,
                                             enforce_careless_gate, error_type_for_canonical,
                                             follow_up_of, name_of, review_reason_for)

router = APIRouter()


# 各意图能看到的资料面。此前全站写死 ["textbook","courseware"]，于是 data/raw/co/exercise
# 那 22 份习题答案（rag_fts.db 里 174 块）**永远检索不到** —— 而讲错因时，习题答案恰恰
# 比教材正文更对口。大厅问答保持只看教材与课件，避免把答案直接漏给学生。
DOC_TYPES_BY_INTENT = {
    "chat": ["textbook", "courseware"],
    "solve": ["textbook", "courseware"],
    "practice": ["textbook", "courseware"],
    "diagnose": ["textbook", "courseware", "exercise"],
    "exam_review": ["textbook", "courseware", "exercise"],
}


def rag_context(query: str, top_n: int = 3, course: str = "ds-algo",
                intent: str = "solve") -> tuple[str, list[dict]]:
    """检索教材片段（按 course 隔离），返回 (拼接上下文, 引用列表)。无语料/出错返回 ('', [])。"""
    try:
        from backend.rag.retriever import get_engine
        engine = get_engine()
        if not engine.has_corpus():
            return "", []
        doc_types = DOC_TYPES_BY_INTENT.get(intent, DOC_TYPES_BY_INTENT["solve"])
        hits = engine.retrieve(query, doc_types=doc_types, top_n=top_n, course=course)
        return engine.build_context(hits), [h.to_dict() for h in hits]
    except Exception:  # noqa: BLE001
        return "", []


def diagnose_evidence(problem: str, course: str, student_answer: str = "") -> tuple[str, list[dict]]:
    """诊断用的教材证据。

    查询用题干 + 学生作答前段拼成：错答里往往带着被误用的术语（"直接返回合法"），
    只用题干检索会漏掉正好讲这个误区的那一段。
    """
    query = f"{problem} {student_answer[:200]}".strip() if student_answer else problem
    return rag_context(query, course=course, intent="diagnose")


def _with_citations(result: dict, citations: list[dict]) -> dict:
    """把引用挂到诊断结果上；解析失败的结果也照挂，前端仍能给出教材出处。"""
    if isinstance(result, dict) and citations:
        result.setdefault("citations", citations)
    return result


# ---------- 解题 ----------
class SolveReq(BaseModel):
    problem: str
    course: str = "ds-algo"


@router.post("/solve")
def solve(req: SolveReq):
    ctx, cites = rag_context(req.problem, course=req.course)
    data = solver_mod.solve(req.problem, context=ctx, course=req.course)
    return {"solution": data, "citations": cites}


@router.post("/solve/stream")
def solve_stream(req: SolveReq):
    """流式解题：先发引用 → 实时推理(reasoning) → 结构化解析(solution) → 验证。"""
    ctx, cites = rag_context(req.problem, course=req.course)
    msgs = solver_mod.build_messages(req.problem, ctx, course=req.course)

    def gen():
        for i, c in enumerate(cites, 1):
            yield _sse({"type": "citation", "index": i, **c})
        content = ""
        try:
            for kind, delta in llm_mod.ask_reasoner_stream(msgs):
                if kind == "reasoning":
                    yield _sse({"type": "reasoning", "text": delta})
                else:
                    content += delta
        except Exception:  # noqa: BLE001
            data = solver_mod.solve(req.problem, context=ctx, course=req.course)
            yield _sse({"type": "solution", "data": data})
            yield _sse({"type": "done"})
            return
        data = solver_mod.finalize(content, messages=msgs, model=settings.reasoner_model, course=req.course)
        if data.get("error"):
            data = solver_mod.solve(req.problem, context=ctx, course=req.course)   # 解析失败回退非流式
        yield _sse({"type": "solution", "data": data})
        ver = data.get("verification")
        if ver:
            yield _sse({"type": "verify", "passed": ver.get("passed")})
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- 诊断 ----------
class DiagnoseReq(BaseModel):
    problem: str
    student_answer: str = ""
    reference: str = ""
    student_code: str = ""
    func_name: str = ""
    tests: list[dict] | None = None
    course: str = "ds-algo"


@router.post("/diagnose")
def diagnose(req: DiagnoseReq):
    ctx, cites = diagnose_evidence(req.problem, req.course, req.student_answer)
    d = diag_mod.diagnose(req.problem, req.student_answer, reference=req.reference,
                          student_code=req.student_code, func_name=req.func_name,
                          tests=req.tests, course=req.course, context=ctx)
    return _with_citations(_public_diagnosis(d), cites)


def _user_detail(exc: Exception) -> str:
    """把业务层的 ValueError 转成给用户看的 detail。

    业务模块（模考、摸底、复习计划、导出）用 ValueError 带出写好的中文提示，原样透传
    最有用；但 Python 自己也抛 ValueError —— 日期解析的 `Invalid isoformat string`、
    JSON 解析错误（JSONDecodeError 是它的子类）—— 那些原文不能上屏（I-05）。
    判据：类型恰好是 ValueError 且含中文才透传，其余记日志、给一句通用话。
    """
    msg = str(exc)
    if type(exc) is ValueError and re.search(r"[\u4e00-\u9fff]", msg):
        return msg
    logger.warning("non-user ValueError suppressed: %s: %s", type(exc).__name__, msg)
    return "这个请求没能完成，请检查填写的内容后重试。"


def _public_diagnosis(d: dict) -> dict:
    """诊断结果返回页面前去掉内部字段：沙箱原始报错（verification.extra.tech，I-05）
    和解析失败时附带的模型原文（raw）。"""
    d = {k: v for k, v in d.items() if k != "raw"}
    ver = d.get("verification")
    if isinstance(ver, dict) and isinstance(ver.get("extra"), dict) and "tech" in ver["extra"]:
        d["verification"] = {**ver, "extra": {k: v for k, v in ver["extra"].items() if k != "tech"}}
    return d


# ---------- 拍照批改 ----------
# 图片走 data URL 塞在 JSON body 里（与 auth.py 的头像同一套路），不用 multipart：
# 前端已在 canvas 里压过一道，服务端也就不必落盘，转手喂给视觉模型即可。
_IMG_PREFIXES = ("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")
# 手写稿要留得住笔迹，比头像那两道闸（400K/300K）宽得多。
# 前端按最长边 1600px + JPEG q0.85 压完通常在 300KB 上下，离这个上限很远。
_IMG_MAX_CHARS = 3_000_000
_IMG_MAX_BYTES = 2_200_000


class PhotoGradeReq(BaseModel):
    image: str
    course: str = "ds-algo"
    problem: str = ""          # 学生可选填题干；留空则让诊断只针对解题步骤本身
    reference: str = ""
    # 学生订正过的转写。端点原先只让学生"看见"转写读对没有，读错了唯一的出路是
    # 换张照片重拍 —— 可字迹本来就那样，重拍多少次也还是认不出。给了这个字段之后，
    # 学生可以直接把认错的字改掉，带着订正稿重跑诊断，省掉一次视觉调用。
    transcript: str = ""


class PhotoCollectReq(BaseModel):
    course: str = "ds-algo"
    knowledge_point: str = ""
    error_type: str = ""
    diagnosis: str = ""
    problem_stem: str = ""      # 历史字段：前端一直往这里放的是转写稿
    problem: str = ""           # 学生填的题干（选填）
    request_review: bool = False  # 收录的同时请助教看看这次批改


class ReviewRequestReq(BaseModel):
    """学生说"请助教看看"。解题诊断带 error_record_id；大厅问答带问题与 AI 回答。"""
    error_record_id: int = 0
    course: str = "ds-algo"
    source: str = "hall"
    question: str = ""
    ai_answer: str = ""
    note: str = ""
    no_evidence: bool = False


class ReviewReplyReq(BaseModel):
    request_id: int
    verdict: str = "agree"      # agree = AI 说得对；corrected = 助教给了更正
    reply: str = ""


def _validate_image(image: str) -> None:
    """照 auth.py:update_profile 的两道闸：先卡 base64 文本长度，再卡解码后字节数。"""
    if not image.startswith(_IMG_PREFIXES):
        raise HTTPException(status_code=400, detail="图片格式不支持，请用 JPEG / PNG / WebP")
    if len(image) > _IMG_MAX_CHARS:
        raise HTTPException(status_code=400, detail="图片过大，请重新拍摄或压缩后上传")
    try:
        decoded = base64.b64decode(image.split(",", 1)[1], validate=True)
    except (ValueError, binascii.Error, IndexError) as exc:
        raise HTTPException(status_code=400, detail="图片数据无效") from exc
    if len(decoded) > _IMG_MAX_BYTES:
        raise HTTPException(status_code=400, detail="图片过大，请重新拍摄或压缩后上传")


@router.post("/grade/photo/stream")
def grade_photo_stream(req: PhotoGradeReq, ident: dict = Depends(current_identity)):
    """拍照批改：视觉转写 → 错因诊断，分阶段流式推。

    两步 LLM 串起来要几十秒，先把转写文本推上屏，等待感才压得住；
    更要紧的是让学生**看见转写读对没有** —— 读错了他能当场发现，而不是拿着
    一份基于误读的诊断去改题。本端点只诊断不落库，入库走 /grade/photo/collect。
    """
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    # 校验放在生成器外面：一旦返回 StreamingResponse，状态码就钉死 200 了，
    # 前端 postSSE 只认 resp.ok，错误就再也没法以 HTTP 语义传出去。
    _validate_image(req.image)
    if not settings.vision_ready:
        # 话术写给学生：配置名只留在 /health 与 preflight 里给部署者看（docs/34 §2.2）
        raise HTTPException(status_code=503,
                            detail="拍照批改暂未开通，可以先手动输入题目，到「解题纠错」里批改。")
    course = req.course if is_valid_course(req.course) else "ds-algo"

    def gen():
        try:
            # 带着订正稿来的，说明这一步学生已经替模型做完了，不必再认一遍图
            if req.transcript.strip():
                transcript = req.transcript.strip()
                yield _sse({"type": "transcript", "text": transcript, "edited": True})
            else:
                yield _sse({"type": "transcribing"})
                transcript = photo_mod.transcribe(req.image, course=course)
                if not transcript:
                    yield _sse({"type": "error", "kind": "unreadable",
                                "message": "没能从照片里认出内容。可以换一张更清晰的，或者直接手动输入。"})
                    yield _sse({"type": "done"})
                    return
                yield _sse({"type": "transcript", "text": transcript})

            yield _sse({"type": "diagnosing"})
            problem = req.problem or "（学生未提供题干，请仅根据下面的解题步骤判断对错与错因）"
            ctx, cites = diagnose_evidence(problem, course, transcript)
            msgs, verification = diag_mod.build_messages(
                problem, transcript, reference=req.reference, course=course, context=ctx)
            content = ""
            try:
                for kind, delta in llm_mod.ask_reasoner_stream(msgs):
                    if kind == "reasoning":
                        yield _sse({"type": "reasoning", "text": delta})
                    else:
                        content += delta
                d = diag_mod.finalize(content, verification, messages=msgs,
                                      model=settings.reasoner_model)
                if d.get("error"):
                    raise ValueError("parse failed")
            except Exception:  # noqa: BLE001 —— 流式/解析失败回退非流式，与 tutor 一致
                d = diag_mod.diagnose(problem, transcript, reference=req.reference,
                                      course=course, context=ctx)
            yield _sse({"type": "result", "transcript": transcript, **_with_citations(d, cites)})
            yield _sse({"type": "done"})
        except Exception as exc:  # noqa: BLE001
            # kind 让前端按原因给下一步：未开通 → 手动输入；超时 → 重试；其他 → 带编号求助
            yield _sse({"type": "error", **errors.report(exc, "photo_grade", user=ident["username"])})
            yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/grade/photo/collect")
def grade_photo_collect(req: PhotoCollectReq, ident: dict = Depends(current_identity)):
    """学生确认后把这次批改收进错题本。

    刻意与批改分开：转写读错一处，会顺着错题本 → 薄弱画像 → 复习计划整条闭环脏下去，
    所以要过学生这一手。
    """
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    if not is_valid_course(req.course):
        raise HTTPException(status_code=400, detail="课程不存在")
    # knowledge_id 留 0 的话，list_error_records / 薄弱画像那两条
    # `JOIN knowledge_points ON k.id = e.knowledge_id` 会把这条记录**静默**滤掉：
    # 不报错，只是存了等于没存。所以宁可在这里挡住，让学生先选知识点。
    if not req.knowledge_point.strip():
        raise HTTPException(status_code=400, detail="请先选择这道题属于哪个知识点")
    kid = db.get_or_create_knowledge_id(req.knowledge_point.strip(), req.course)
    if not kid:
        raise HTTPException(status_code=400, detail="知识点无效")
    err = req.error_type or "knowledge_gap"
    rid = db.save_error_record(
        ident["username"], error_type=err,
        diagnosis=req.diagnosis, knowledge=req.knowledge_point.strip(),
        problem_stem=req.problem.strip() or req.problem_stem, problem_id=0, knowledge_id=kid,
        # 拍照批改的"学生作答"就是那份转写稿（可能经学生订正）
        student_answer=req.problem_stem,
        review_reason="student_request" if req.request_review else review_reason_for(err))
    # 收进错题本的这一份也是一次作答（做错的）。此前只写错题不记作答，于是助教
    # 总览出现"0 次作答、4 条错题"这种自相矛盾的数（I-03）。记一次 0 分作答之后，
    # 所有入口都满足"错题数 ≤ 作答数"。
    db.save_attempt(ident["username"], (req.problem_stem or "")[:2000], 0.0, knowledge_id=kid)
    if req.request_review:
        db.mark_error_review_requested(rid, ident["username"])
    return {"ok": True, "id": rid, "knowledge_id": kid, "review_requested": req.request_review}


# ---------- 学生申请人工复核（docs/35 P1-2） ----------
# AI 的结论学生觉得不对、或者 AI 自己说依据不足时，要有一个能落到真人手上的出口。
# 此前全站没有：低置信度只存在于提示词里，页面上看不见，学生也无处可诉。

@router.post("/review-requests")
def review_request_create(req: ReviewRequestReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    if req.error_record_id:
        row = db.mark_error_review_requested(req.error_record_id, ident["username"])
        if not row:
            raise HTTPException(status_code=404, detail="这条记录不存在，或者不是你的")
        return {"ok": True, "kind": "error", "id": row["id"],
                "requested_at": row["review_requested_at"]}
    if not is_valid_course(req.course):
        raise HTTPException(status_code=400, detail="课程不存在")
    if not req.question.strip() and not req.ai_answer.strip():
        raise HTTPException(status_code=400, detail="没有可以复核的内容")
    source = req.source if req.source in ("hall", "solve", "photo") else "hall"
    rid = db.create_review_request(ident["username"], req.course, req.question.strip(),
                                   req.ai_answer.strip(), source=source, note=req.note.strip(),
                                   no_evidence=req.no_evidence)
    return {"ok": True, "kind": "qa", "id": rid}


def _request_row(r: dict, for_ta: bool) -> dict:
    out = {"id": r["id"], "course": r["course_id"], "source": r["source"],
           "question": r["question"], "ai_answer": r["ai_answer"], "note": r["note"],
           "no_evidence": bool(r["no_evidence"]), "status": r["status"],
           "ta_verdict": r["ta_verdict"], "ta_reply": r["ta_reply"],
           "created_at": r["created_at"], "answered_at": r["answered_at"] or ""}
    if for_ta:
        out["username"] = r["username"]
    return out


@router.get("/review-requests")
def review_request_mine(course: str = "", ident: dict = Depends(current_identity)):
    """学生自己的申请：提交时间 → 助教结论 → 批注（错题那一类在错题本里看）。"""
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    rows, total = db.list_review_requests(username=ident["username"], course_id=course, limit=50)
    return {"items": [_request_row(r, False) for r in rows], "total": total}


@router.get("/teaching/review-requests")
def teaching_review_requests(course: str = "", status: str = "open", limit: int = 20, offset: int = 0,
                             ident: dict = Depends(current_identity)):
    _require_ta(ident)
    if status not in ("", "open", "answered"):
        raise HTTPException(status_code=400, detail="status 只能是 open / answered / 空")
    rows, total = db.list_review_requests(course_id=course, status=status,
                                          limit=max(1, min(100, limit)), offset=offset)
    return {"items": [_request_row(r, True) for r in rows], "total": total}


@router.post("/teaching/review-requests/reply")
def teaching_review_reply(req: ReviewReplyReq, ident: dict = Depends(current_identity)):
    _require_ta(ident)
    if req.verdict not in ("agree", "corrected"):
        raise HTTPException(status_code=400, detail="结论只能是「AI 说得对」或「需要更正」")
    if req.verdict == "corrected" and not req.reply.strip():
        raise HTTPException(status_code=400, detail="需要更正时请写明正确的说法")
    row = db.answer_review_request(req.request_id, ident["username"], req.verdict, req.reply.strip())
    if not row:
        raise HTTPException(status_code=404, detail="这条申请不存在")
    return _request_row(row, True)


@router.get("/error-tags")
def error_tags():
    """错因码表。原先只在助教端 /teaching/overview 里顺带返回，学生端要用就得
    在前端复制一份码表——那份副本迟早跟 error_taxonomy.py 对不上。"""
    return {"error_tags": [{"code": code, "name": info["name"], "follow_up": info["follow_up"]}
                           for code, info in ERROR_TAXONOMY.items()]}


# ---------- 练习 ----------
class PracticeReq(BaseModel):
    knowledge_point: str
    knowledge_id: int = 0
    error_type: str = ""
    difficulty: int = 0
    course: str = "ds-algo"


def _practice_setup(req: PracticeReq, ident: dict) -> dict:
    """出题前的准备：定位知识点、错因、难度、教材依据、要避开的已有题干。流式与非流式共用。"""
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    point = profile_mod.point_of(ident["username"], req.course,
                                 knowledge_id=req.knowledge_id, name=req.knowledge_point)
    if not point:
        raise HTTPException(status_code=404, detail="知识点不存在或不属于当前课程")
    error_type = req.error_type or point["top_error_type"]
    difficulty = req.difficulty or point["target_difficulty"]
    ctx, cites = rag_context(point["name"], course=req.course)
    # 该知识点已有的题干交给出题器避开：不传的话，同一个知识点连点三次"生成练习"，
    # 实测拿到三道只差一两句话的"请简述数据结构的定义及其与算法的关系"。
    avoid = [p["stem"] for p in db.list_problems(course_id=req.course, knowledge=point["name"],
                                                 limit=500)][-6:]
    return {"point": point, "error_type": error_type, "difficulty": difficulty,
            "ctx": ctx, "cites": cites, "avoid": avoid}


def _practice_gen_kwargs(req: PracticeReq, prep: dict) -> dict:
    return dict(knowledge_point=prep["point"]["name"], context=prep["ctx"],
                error_type=prep["error_type"], difficulty=prep["difficulty"], course=req.course,
                max_retries=2, require_verified=True, avoid=prep["avoid"])


_PRACTICE_PRIVATE = ("verification", "code", "func_name", "test_cases",
                     "final_answer_canonical", "rubric", "raw")


def _practice_finish(req: PracticeReq, prep: dict, data: dict) -> dict:
    """出题后的收尾：通过核对的入库并带上教材依据；没通过的按原因措辞，不入库。"""
    point, error_type = prep["point"], prep["error_type"]
    data["knowledge_id"] = point["knowledge_id"]
    data["knowledge"] = point["name"]
    data["error_type"] = error_type
    data["error_name"] = name_of(error_type) if error_type else ""
    if data.get("verified") is True:
        data["problem_id"] = pg_mod.persist(data, req.course, point["name"], citations=prep["cites"])
        # 返给前端的是**完整命中**（含原文摘录），入库的是裁剪过的定位字段。
        # 两者故意不同：页面上"教材依据"要能展开看原文，而库里存正文会与索引慢慢不一致。
        data["citations"] = prep["cites"]
        # 入库之后再裁：参考代码、测试用例（含隐藏用例）、规范答案 JSON 和自检明细都已
        # 存进题库供判题用，页面用不到；下发就等于把判题依据和内部校验过程交给了浏览器。
        return {k: v for k, v in data.items() if k not in _PRACTICE_PRIVATE}
    # 未验证题不作为正式练习展示，也不写库。按原因措辞：此前一律说"未通过答案验证"，
    # 而多数情况其实是"开放题核对不了"，不是"答案错了"，学生看了会以为系统出了错题。
    if data.get("error"):
        why = "这次没能生成完整的题目。"
    elif data.get("verified") is None:
        why = "这几次生成的都是开放式简答题，答案没法自动核对；为了不给你不可靠的参考答案，没有展示。"
    else:
        why = "生成的题没通过答案自检（参考答案对不上），已丢弃。"
    return {"verified": False, "problem_id": 0, "knowledge_id": point["knowledge_id"],
            "knowledge": point["name"], "error_type": error_type,
            "error_name": name_of(error_type) if error_type else "",
            "error": why + "可以再生成一次，或先做题库里这个知识点的题。"}


@router.post("/practice/generate")
def practice_generate(req: PracticeReq, ident: dict = Depends(current_identity)):
    prep = _practice_setup(req, ident)
    data = pg_mod.generate(**_practice_gen_kwargs(req, prep))
    return _practice_finish(req, prep, data)


# 进度条上给学生看的话：只说"这题不合格、换一道"，不描述内部流程（格式解析、
# 自测、规范答案比对这些是实现细节）。原因码只进日志。
_RETRY_LABEL = {
    "parse": "这一题没写完整",
    "tests_failed": "这一题的参考答案没通过检验",
    "unverifiable": "这一题答案没法自动核对",
    "mismatch": "这一题的参考答案前后不一致",
}


@router.post("/practice/generate/stream")
def practice_generate_stream(req: PracticeReq, ident: dict = Depends(current_identity)):
    """同 /practice/generate，但把真实进度推给页面：检索 → 第 n 次出题（题目边写边显示）
    → 核对答案 →（不行就说明原因、重出）→ 结果。

    出题这一步没有可展示的推理过程；这里展示的是系统实际在做的每一步，
    不编造"思考"。准备阶段的错误（未登录、知识点不存在）在返回流之前就抛出，保留 HTTP 语义。
    """
    prep = _practice_setup(req, ident)

    def gen():
        yield _sse({"type": "stage", "stage": "retrieve",
                    "label": f"已检索教材：{len(prep['cites'])} 段依据" if prep["cites"] else "没检索到教材片段，按知识点出题"})
        data: dict = {}
        try:
            for kind, payload in pg_mod.generate_events(**_practice_gen_kwargs(req, prep), stream=True):
                if kind == "attempt":
                    yield _sse({"type": "stage", "stage": "generate", "attempt": payload,
                                "label": "出题中" if payload == 1 else f"第 {payload} 次出题"})
                elif kind == "token":
                    yield _sse({"type": "token", "text": payload})
                elif kind == "verify":
                    yield _sse({"type": "stage", "stage": "verify", "label": "核对答案"})
                elif kind == "retry":
                    logger.info("practice regenerate: reason=%s", payload)
                    yield _sse({"type": "stage", "stage": "retry",
                                "label": f"{_RETRY_LABEL.get(payload, '这一题不合格')}，换一道"})
                elif kind == "result":
                    data = payload
            # 题目放在 problem 里，不能摊平：题目自带 "type"（题型），摊平会把事件类型覆盖掉
            yield _sse({"type": "result", "problem": _practice_finish(req, prep, data)})
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", **errors.report(exc, "practice_generate", user=ident["username"])})
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


def _recommend_reason(point: dict) -> str:
    parts = []
    if point["error_freq"]:
        parts.append(f"有 {point['error_freq']} 条未解决错题")
    if point["top_error_name"]:
        parts.append(f"主要错因：{point['top_error_name']}")
    if point["days_since"] is not None and point["days_since"] >= 7:
        parts.append(f"已 {int(point['days_since'])} 天未练")
    if not point["attempts"]:
        parts.append("尚未摸底")
    return "；".join(parts) or "按当前掌握度建议巩固"


@router.get("/practice/recommend")
def practice_recommend(course: str = "ds-algo", limit: int = 5, knowledge_id: int = 0,
                       per_point: int = 3, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    with db.connection_scope():
        return _practice_recommend(course, limit, knowledge_id, per_point, ident)


def _practice_recommend(course: str, limit: int, knowledge_id: int, per_point: int, ident: dict):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    limit = max(1, min(10, limit))
    per_point = max(1, min(5, per_point))
    points = profile_mod.build_profile(ident["username"], course, limit=0)
    if knowledge_id:
        points = [p for p in points if p["knowledge_id"] == knowledge_id]
    else:
        points = points[:limit]
    explored = any(p["attempts"] or p["error_total"] for p in points)
    items: list[dict] = []
    for point in points:
        reason = _recommend_reason(point)
        bank = db.pick_practice_problems(ident["username"], course, point["knowledge_id"],
                                         point["target_difficulty"], per_point)
        for row in bank:
            row.update(source="bank", knowledge_id=point["knowledge_id"],
                       error_type=point["top_error_type"], error_name=point["top_error_name"],
                       reason=reason)
            items.append(row)
        if len(bank) < per_point:
            items.append({"source": "generate", "knowledge_id": point["knowledge_id"],
                          "knowledge": point["name"], "difficulty": point["target_difficulty"],
                          "error_type": point["top_error_type"],
                          "error_name": point["top_error_name"], "reason": reason})
    return {"course": course, "cold_start": not explored,
            "weak_points": points, "items": items}


# ---------- 学情画像 ----------
@router.get("/profile/weak")
def profile_weak(course: str = "ds-algo", limit: int = 10, touched_only: bool = False,
                 ident: dict = Depends(current_identity)):
    """薄弱知识点排序。个性化练习与复习计划取的是同一份，前端也直接用它渲染。

    未登录返回空表并置 cold_start —— 不编演示数据。复习计划原先在没数据时会
    落回 4 条硬编码知识点，那让人分不清"系统在按我的情况排"还是"在放样例"。
    """
    if not ident["username"]:
        return {"course": course, "cold_start": True, "items": []}
    items = profile_mod.build_profile(ident["username"], course,
                                      include_untouched=not touched_only, limit=limit)
    explored = [r for r in items if r["attempts"] or r["error_total"]]
    return {"course": course, "cold_start": not explored, "items": items}


@router.get("/community/standings")
def community_standings(course: str = "ds-algo", offset: int = 0,
                        ident: dict = Depends(current_identity)):
    """周榜 + 同伴匿名对比 + 最近两周的学习态势。

    榜上除自己外不下发任何身份字段（见 backend/learning/community.py 的开头）——
    匿名在这里是功能本身的一部分，不是可配置项。
    """
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    if not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    return community.standings(ident["username"], course, max(0, min(52, int(offset or 0))))


@router.get("/learning/center")
def learning_center(course: str = "ds-algo", ident: dict = Depends(current_identity)):
    """个人学习中心的一次性快照；所有推荐与进度都来自已有真实学习记录。"""
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    with db.connection_scope():
        return _learning_center(course, ident)


def _learning_center(course: str, ident: dict):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    get_course(course)
    points = profile_mod.build_profile(ident["username"], course)
    activity = db.learning_activity_summary(ident["username"], course)
    plan = review_service.get_active(ident["username"], course)
    # 「今日待办」= 今天的必做 + 还没还完的补做，**不再**是"所有过期未完成的任务"。
    # 原先那个口径会让落后三天的人在首页看到二十几项，而它们里绝大多数是
    # 第二三轮巩固 —— 真正该顶在最前面的那一两件反而被淹掉了。
    today_view = (plan or {}).get("today") or {}
    due = [t for t in (today_view.get("core", []) + today_view.get("catchup", []))
           if not t.get("completed")]
    summary = profile_mod.summarize_mastery(points)
    explored = [p for p in points if p["attempts"] or p["error_total"]]
    activity["correct_rate"] = round(
        activity["correct"] / activity["attempts"], 4) if activity["attempts"] else 0.0
    activity["streak_days"] = community.learning_streak(
        activity.pop("activity_days"),
        study_budget.load(ident["username"])["buffer_weekday"])
    return {
        "course": course,
        "cold_start": not explored,
        "overview": {
            **activity,
            # 口径见 profile.summarize_mastery：练过的点、带遗忘衰减；没练过时为 None
            "average_mastery": summary["average"],
            "practiced_points": summary["practiced"],
            "mastered_points": summary["mastered"],
            "total_points": summary["total"],
        },
        "weak_points": points[:5],
        "plan": plan,
        "due_tasks": due[:8],
        # 这个学生每道题的实际用时（最近 60 次、剔掉没计时的 0 的中位数，分钟）。
        # 首页拿它 × 剩余题数估"约多久"（docs/35 §2.2）。没有样本就给 null ——
        # 宁可不显示用时，也不拿一个全局默认值冒充这个人的节奏。
        "minutes_per_item": (round(sec / 60, 1) if (sec := db.attempt_duration_median(ident["username"]))
                             else None),
    }


# ---------- 入学摸底 ----------
class PlacementStartReq(BaseModel):
    course: str = "ds-algo"
    length: int = placement_mod.DEFAULT_LENGTH
    restart: bool = False


class PlacementAnswerReq(BaseModel):
    session_id: int
    answer: str = ""
    duration: int = 0


class PlacementIdReq(BaseModel):
    session_id: int


def _placement_course(course: str) -> str:
    # 不吃 get_course 的静默回退：摸底会按这门课写掌握度初值，
    # 未知 course 回退到数据结构等于把初值写到别门去，而那正是串课 bug 的成因。
    if not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    return course


@router.get("/placement/status")
def placement_status(course: str = "ds-algo", ident: dict = Depends(current_identity)):
    """摸底可用性 + 未答完的会话 + 上一次的报告。页面开局一次问清楚。"""
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    _placement_course(course)
    payload = placement_mod.status(ident["username"], course)
    payload["last"] = placement_mod.last_finished(ident["username"], course)
    return payload


@router.post("/placement/start")
def placement_start(req: PlacementStartReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    _placement_course(req.course)
    try:
        return placement_mod.start(ident["username"], req.course,
                                   length=req.length, restart=req.restart)
    except ValueError as exc:
        # 题库不足是"这个功能现在没法用"，不是请求错了 —— 与拍照批改未配视觉模型同一档。
        raise HTTPException(status_code=503, detail=_user_detail(exc)) from exc


@router.post("/placement/answer")
def placement_answer(req: PlacementAnswerReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        return placement_mod.answer(ident["username"], req.session_id,
                                    req.answer, duration=req.duration)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_user_detail(exc)) from exc


@router.post("/placement/abandon")
def placement_abandon(req: PlacementIdReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        return {"ok": placement_mod.abandon(ident["username"], req.session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_user_detail(exc)) from exc


# ---------- 真题模考 ----------
class ExamIdReq(BaseModel):
    session_id: int


class ExamSaveReq(BaseModel):
    session_id: int
    answers: dict[str, str] = {}


@router.get("/exam/preview")
def exam_preview(ident: dict = Depends(current_identity)):
    """开考前的一次性快照：这张卷子能不能组出来、缺多少、有没有没考完的场次。"""
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    paper = mock_exam.build_paper(ident["username"])
    # 题面不发：还没开考就把题给出去，等于把限时模考变成开卷预习
    paper.pop("items", None)
    return {**paper,
            "pending": mock_exam.pending(ident["username"]),
            # 交了卷但没阅完的那场。不给出来的话，考生刷新后看到的是开考前的页面，
            # 而他刚考完的那张卷子在库里、错题本也记了 —— 只是没有任何入口回得去。
            "grading_pending": mock_exam.unfinished_grading(ident["username"])}


@router.post("/exam/start")
def exam_start(ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        return mock_exam.start(ident["username"])
    except ValueError as exc:
        # 组不出卷是"这个功能现在没内容可考"，与摸底题库不足同一档
        raise HTTPException(status_code=503, detail=_user_detail(exc)) from exc


@router.post("/exam/save")
def exam_save(req: ExamSaveReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        return {"saved": mock_exam.save_answers(ident["username"], req.session_id, req.answers)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_user_detail(exc)) from exc


@router.get("/exam/result/{session_id}")
def exam_result(session_id: int, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        report = mock_exam.build_report(ident["username"], session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_user_detail(exc)) from exc
    # 讲评带教材出处：答错的题补检索。客观题走确定性判定、没有 LLM 诊断，
    # 恰恰是最需要"教材哪一页这么说"的地方。检索走本地 FTS5，不额外花 token。
    for item in report.get("items") or []:
        if item.get("passed") is False and not item.get("citations"):
            _, cites = rag_context(item.get("stem", ""), top_n=2,
                                   course=item.get("course") or "ds-algo", intent="exam_review")
            if cites:
                item["citations"] = cites
    return report


@router.get("/exam/history")
def exam_history(limit: int = 10, ident: dict = Depends(current_identity)):
    """历次模考。成绩单一直存着，这是它们唯一的列表入口。"""
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    return {"items": mock_exam.history(ident["username"], limit=max(1, min(int(limit or 10), 50)))}


@router.post("/exam/abandon")
def exam_abandon(req: ExamIdReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        mock_exam.owned(ident["username"], req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_user_detail(exc)) from exc
    return {"ok": db.finish_session(req.session_id, state="abandoned")}


def _reference_of(full: dict) -> str:
    """给人看的参考答案。numeric 题存的是 canonical JSON，直接印出来没人读得懂。"""
    if full.get("verification_type") == "numeric":
        return canonical_text(_canonical_of(full))
    return str(full.get("answer") or "")


def _grade_one(item: dict, answer: str, username: str) -> dict:
    """批一道题：判定 → 按分值折算得分 → 走判题那条写回。

    空题不判：不调模型、不记作答、不写错题。**没作答和做错不是一回事** ——
    把没来得及做的题记成错题，错题本会被交白卷的那次模考灌满，
    而错题本是薄弱画像和复习计划的输入。
    """
    base = {"seq": item["seq"], "problem_id": item["problem_id"],
            "full": item["score"], "score": 0.0, "passed": False,
            "skipped": False, "details": "", "diagnosis": "", "ratio": None, "reference": ""}
    full = db.get_problem(item["problem_id"], with_solution=True)
    if not full:
        return {**base, "skipped": True, "details": "题目已不在库中"}
    # 参考答案照样给：没作答的题也要能在回顾里看到正确答案，那才是模考的用处
    base["reference"] = _reference_of(full)
    if not (answer or "").strip():
        return {**base, "skipped": True, "details": "未作答"}
    is_code = item.get("verification_type") == "unit_test"
    try:
        result, err_type, err_detail = judge_answer(
            full, answer="" if is_code else answer, code=answer if is_code else "",
            course=item["course"])
    except Exception as exc:  # noqa: BLE001 —— 一道题批不动不该让整张卷子作废
        err = errors.report(exc, "exam_grade", user=username, problem_id=item["problem_id"])
        return {**base, "skipped": True,
                "details": f"这道题没能批出来，稍后可以重新阅卷（错误编号 {err['error_id']}）"}

    passed = bool(result.get("passed"))
    ratio = result.get("ratio")
    if ratio is None:
        ratio = 1.0 if passed else 0.0
    # 综合题按得分点比例给分，客观题只有 0/1。分值是整数，得分保留一位小数。
    _writeback(username, full, result, err_type, err_detail,
               problem_id=item["problem_id"], answer_text=answer,
               tech_detail=result.pop("_tech", ""))
    return {**base, "score": round(item["score"] * ratio, 1), "passed": passed,
            "ratio": ratio, "details": result.get("details", ""),
            "diagnosis": result.get("diagnosis", ""),
            "error_tag_names": result.get("error_tag_names", []),
            "remediation": result.get("remediation", "")}


@router.post("/exam/submit/stream")
def exam_submit_stream(req: ExamIdReq, ident: dict = Depends(current_identity)):
    """交卷阅卷，逐题推进度。

    走 SSE 是因为综合题可能一道要等一次模型：47 道题闷头批完再返回，
    考生面对的是一分多钟的空白页。校验放在生成器外 —— 一旦返回
    StreamingResponse，状态码就钉死 200，错误再也没法以 HTTP 语义传出去。
    """
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        items, answers, meta = mock_exam.grading_input(ident["username"], req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_user_detail(exc)) from exc

    # finish 是关闸：交卷只能发生一次。抢不到的那次分两种，得分开处理 ——
    # 已经阅完的，把存下来的成绩单原样给它（重复交卷、刷新重进都走这条）；
    # 阅到一半断了的，接着批没批过的题。早先这里一律 409，于是"交卷后断线"
    # 这一种把整场模考锁死：题批了一半、错题本记了一半，成绩单永远停在 0 分。
    fresh = mock_exam.finish(req.session_id)
    done = {} if fresh else {g["seq"]: g for g in mock_exam.graded_rows(req.session_id)}
    ready = None if fresh else mock_exam.stored_report(req.session_id)

    def gen():
        if ready is not None:
            yield _sse({"type": "report", "report": ready, "resumed": True})
            yield _sse({"type": "done"})
            return
        graded = dict(done)
        total = len(items)
        # 进度从已批过的题数起算：续阅时进度条从 0 重新爬一遍，
        # 看起来就像刚才那几分钟白批了。
        if graded:
            yield _sse({"type": "item", "done": len(graded), "total": total, "resumed": True})
        for item in items:
            if item["seq"] in graded:
                continue
            yield _sse({"type": "grading", "seq": item["seq"], "done": len(graded), "total": total})
            row = _grade_one(item, answers.get(item["seq"], ""), ident["username"])
            graded[row["seq"]] = row
            # 每道题批完就落库。中断的代价从"整场作废"降到"最后那道题重批一次"。
            mock_exam.save_progress(req.session_id, [row])
            yield _sse({"type": "item", "done": len(graded), "total": total,
                        "seq": row["seq"], "score": row["score"], "full": row["full"],
                        "passed": row["passed"], "skipped": row["skipped"]})
        rows = [graded[it["seq"]] for it in items if it["seq"] in graded]
        report = mock_exam.score_paper(items, rows, meta)
        mock_exam.save_graded(req.session_id, rows, report)
        yield _sse({"type": "report", "report": report})
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/report/weekly")
def report_weekly(course: str = "ds-algo", offset: int = 0,
                  with_comment: bool = True, ident: dict = Depends(current_identity)):
    """学情周报。offset=0 本周、1 上周，以此类推（只往回看，不接受未来周）。

    数字由确定性聚合出，LLM 只写点评 —— 分工与理由见 backend/agents/weekly_report.py。
    """
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    # 这里不用 get_course 的静默回退：周报会把科目名印在卡面上，未知 course 回退到
    # 数据结构就等于生成一张标错科目的报告，而它是要被截图转发出去的。
    if not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    # 上限 52：再往回也没有数据，敞开则是让人用一个 URL 参数把库扫穿。
    offset = max(0, min(52, int(offset or 0)))
    return report_mod.build_report(ident["username"], course, offset, with_comment=with_comment)


# ---------- 备考时间预算 ----------
class BudgetException(BaseModel):
    date: str
    minutes: int = 0
    note: str = ""


class BudgetReq(BaseModel):
    weekly_minutes: list[int] = []       # 7 个整数，索引 0=周一 … 6=周日
    intensity: str = "medium"            # light / medium / sprint
    buffer_weekday: int = 6              # 机动日 0..6；-1 = 不留
    exam_date: str = ""
    exceptions: list[BudgetException] = []


@router.get("/study/budget")
def study_budget_get(ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    budget = study_budget.load(ident["username"])
    # 顺带把课程占比表下发，前端才能就地显示"本门约占 30%"而不用自己抄一份分值。
    budget["course_shares"] = {cid: round(study_budget.course_share(cid), 4)
                               for cid in course_ids()}
    budget["weekly_total"] = study_budget.weekly_total(budget)
    return budget


@router.post("/study/budget")
def study_budget_save(req: BudgetReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    weekly = list(req.weekly_minutes or study_budget.DEFAULT_WEEKLY)
    if len(weekly) != 7:
        raise HTTPException(status_code=400, detail="每周可用时间必须是 7 个数字（周一到周日）")
    # 例外日期按 ISO 校验后再落库：它会直接参与日期比较，脏值会让排期静默漏排一天。
    exceptions = []
    for e in req.exceptions[:120]:
        try:
            date.fromisoformat(e.date)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"例外日期格式错误：{e.date}") from exc
        exceptions.append({"date": e.date, "minutes": e.minutes, "note": e.note})
    if req.exam_date:
        try:
            date.fromisoformat(req.exam_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="初试日期格式错误") from exc

    normalized = study_budget.normalize({
        "weekly_minutes": json.dumps(weekly), "intensity": req.intensity,
        "buffer_weekday": req.buffer_weekday, "exam_date": req.exam_date,
        "exceptions": exceptions,
    })
    db.save_study_budget(ident["username"], json.dumps(normalized["weekly_minutes"]),
                         normalized["intensity"], normalized["buffer_weekday"],
                         normalized["exam_date"], exceptions)
    return study_budget_get(ident)


# ---------- 复习计划 ----------
class ReviewReq(BaseModel):
    knowledge_states: list[dict] | None = None
    days_to_exam: int = 14
    daily_minutes: int = 60
    goal: str = ""
    course: str = "ds-algo"
    start_date: str = ""
    exam_date: str = ""
    max_points: int = 20
    # False = 老的定额排期（每天等量、填满到考试日）。默认走时间预算。
    use_budget: bool = True


@router.post("/review/plan")
def review_plan(req: ReviewReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    start = req.start_date or date.today().isoformat()
    # 考试日期的取值顺序：请求 > 预算里存的初试日期 > 按 days_to_exam 推算。
    # 预算那份是全局的，四门课共用，避免每门重填一次。
    exam = req.exam_date or study_budget.load(ident["username"]).get("exam_date") or ""
    if not exam:
        exam = (date.fromisoformat(start) +
                timedelta(days=max(1, req.days_to_exam) - 1)).isoformat()
    try:
        return review_service.create_plan(
            ident["username"], req.course, start_date=start, exam_date=exam,
            daily_minutes=req.daily_minutes, goal=req.goal, max_points=req.max_points,
            use_budget=req.use_budget,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_user_detail(exc)) from exc


@router.get("/review/plan/active")
def review_plan_active(course: str = "ds-algo", ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    return {"plan": review_service.get_active(ident["username"], course)}


class ReviewCheckinReq(BaseModel):
    task_id: int


@router.post("/review/checkin")
def review_checkin(req: ReviewCheckinReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        return review_service.manual_checkin(ident["username"], req.task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_user_detail(exc)) from exc


@router.get("/review/progress")
def review_progress(course: str = "ds-algo", ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    return review_service.progress(ident["username"], course) or {
        "completion_rate": 0, "error_resolve_rate": 0, "mastery_delta": 0,
        "completed_tasks": 0, "total_tasks": 0,
    }


# ---------- TutorGraph 闭环 ----------
class TutorStartReq(BaseModel):
    problem: str
    course: str = "ds-algo"


class TutorSubmitReq(BaseModel):
    session_id: str
    student_answer: str = ""
    student_code: str = ""
    func_name: str = ""
    tests: list[dict] | None = None


class TutorIdReq(BaseModel):
    session_id: str


class TutorResumeReq(BaseModel):
    course: str = "ds-algo"
    # 指定要续的那次会话（learning_sessions.id）。0 = 该门最近一次未结束的解题会话。
    db_session_id: int = 0


@router.post("/tutor/start")
def tutor_start(req: TutorStartReq, ident: dict = Depends(current_identity)):
    ctx, cites = rag_context(req.problem, course=req.course)
    r = tutor.start(ident["username"], req.problem, context=ctx, course=req.course)
    r["citations"] = cites
    return r


@router.post("/tutor/resume")
def tutor_resume(req: TutorResumeReq, ident: dict = Depends(current_identity)):
    """读回上一次解题会话（题目 + 对话流 + 状态机），没有可续的返回 {}。

    解题页原先只有 start 一个入口：刷新一下，题目、提示级别、诊断卡全没了，
    而这些其实一直在库里。这个端点把它们接回来。
    """
    if not ident["username"]:
        return {}
    return tutor.resume(ident["username"], course=req.course, db_session_id=req.db_session_id)


@router.post("/tutor/submit")
def tutor_submit(req: TutorSubmitReq, ident: dict = Depends(current_identity)):
    return tutor.submit(req.session_id, req.student_answer, student_code=req.student_code,
                        func_name=req.func_name, tests=req.tests, username=ident["username"])


@router.post("/tutor/submit/stream")
def tutor_submit_stream(req: TutorSubmitReq, ident: dict = Depends(current_identity)):
    """流式纠错：实时展示 deepseek-r1 诊断思考(reasoning) → 诊断结果(result)。"""
    # get_owned 而不是 get：会话 id 会经前端与日志流出去，只有发起人能续。
    sess = tutor.get_owned(req.session_id, ident["username"])

    def gen():
        if not sess:
            yield _sse({"type": "error", "message": "会话不存在"})
            yield _sse({"type": "done"})
            return
        sess.attempts += 1
        ref = sess.solution.get("final_answer", "") if sess.solution else ""
        sess_course = getattr(sess, "course", "ds-algo")
        ctx, cites = diagnose_evidence(sess.problem, sess_course, req.student_answer)
        msgs, verification = diag_mod.build_messages(
            sess.problem, req.student_answer, reference=ref,
            student_code=req.student_code, func_name=req.func_name, tests=req.tests,
            course=sess_course, context=ctx)
        content = ""
        try:
            for kind, delta in llm_mod.ask_reasoner_stream(msgs):
                if kind == "reasoning":
                    yield _sse({"type": "reasoning", "text": delta})
                else:
                    content += delta
            d = diag_mod.finalize(content, verification, messages=msgs, model=settings.reasoner_model)
            if d.get("error"):
                raise ValueError("parse failed")
        except Exception:  # noqa: BLE001 —— 流式/解析失败回退非流式
            d = None
        try:
            if d is None or d.get("error"):
                d = diag_mod.diagnose(sess.problem, req.student_answer, reference=ref,
                                      student_code=req.student_code, func_name=req.func_name,
                                      tests=req.tests, course=sess_course, context=ctx)
            d["_has_evidence"] = bool(cites)   # 入队原因要用：诊断有没有检索到教材依据
            payload = tutor.apply_diagnosis(sess, req.student_answer, d, student_code=req.student_code)
        except Exception as exc:  # noqa: BLE001
            # 非流式回退也失败时，此前异常直接炸断流，前端只看到对话停在半截
            yield _sse({"type": "error", **errors.report(exc, "tutor_submit", user=ident["username"])})
            yield _sse({"type": "done"})
            return
        yield _sse({"type": "result", **_with_citations(payload, cites)})
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/tutor/hint")
def tutor_hint(req: TutorIdReq, ident: dict = Depends(current_identity)):
    return tutor.hint(req.session_id, username=ident["username"])


@router.post("/tutor/variant")
def tutor_variant(req: TutorIdReq, ident: dict = Depends(current_identity)):
    return tutor.variant(req.session_id, username=ident["username"])


# ---------- 课程注册表（前端科目切换器 / 按科目知识点） ----------
@router.get("/courses")
def courses_list():
    from backend.core.courses import all_courses, exam_structure
    return {"exam_structure": exam_structure(),
            "items": [{"id": c.id, "display": c.display, "short": c.short, "exam": c.exam,
                       "score": c.score, "code_enabled": c.code_enabled,
                       "verification_types": c.verification_types,
                       "topics": c.topics, "sample_questions": c.sample_questions}
                      for c in all_courses().values()]}


# ---------- 题库（固定题 + 判题） ----------
@router.get("/knowledge")
def knowledge_list(course: str = "ds-algo"):
    """该门的知识点（带 id 与章节）。

    /profile/weak 也能给出这份名单，但那是"某个人的画像"；下发作业要选的是
    课程本身的知识点，跟谁的学习记录无关 —— 让助教从自己的薄弱画像里挑题
    是个说不通的取数口。
    """
    if not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    return {"course": course, "items": db.list_knowledge_points(course)}


@router.get("/problems")
def problems_list(knowledge: str = "", course: str = "ds-algo"):
    return {"items": db.list_problems(course_id=course, knowledge=knowledge),
            "distribution": db.knowledge_distribution(course_id=course)}


@router.get("/problems/{pid}")
def problem_detail(pid: int):
    p = db.get_problem(pid)
    if not p:
        raise HTTPException(status_code=404, detail="题目不存在")
    return p


@router.get("/problems/{pid}/hint")
def problem_hint(pid: int):
    """按需取一级提示。

    单独开一个端点而不是随题目一起发：提示要"点了才给"，跟着题目发过去就是白送，
    而且前端也就没法诚实地统计用了几次提示 —— 那个次数会进掌握度模型
    （mastery.update_mastery 的 hint_penalty）与间隔复习的进阶判断。
    """
    hint = db.get_problem_hint(pid)
    if not hint:
        raise HTTPException(status_code=404, detail="该题暂无提示")
    return {"problem_id": pid, "hint": hint}


class JudgeReq(BaseModel):
    problem_id: int
    code: str = ""
    func_name: str = ""
    answer: str = ""
    course: str = "ds-algo"
    # 前端埋点。这两个字段建表起就恒为 0，于是 update_mastery 的提示惩罚、
    # next_interval 的"依赖提示则不进阶"两条分支从来没被触发过。
    hints_used: int = 0
    duration: int = 0
    task_id: int = 0


def _days_since(ts: str | None) -> float:
    """距库里那个时间戳过了几天。解析不出来按 0 算（当作刚练过，不凭空扣分）。"""
    if not ts:
        return 0.0
    try:
        delta = datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, delta.total_seconds() / 86400.0)


def _canonical_of(full: dict):
    """numeric 题的 canonical 答案存在 problems.answer（JSON 串）。解析失败返回 None。"""
    raw = full.get("answer")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else None
    except (json.JSONDecodeError, TypeError):
        return None


def _rubric_ratio(rubric: list[dict], hit: list[str]) -> float | None:
    """命中的得分点占总分的比例。rubric 为空返回 None（没有得分点可分，只能全或无）。

    按 rubric_items.score 加权而不是按条数：得分点本来就不等值，
    "写出状态转移方程"和"说明边界"各算一条会把分算歪。
    """
    total = sum(float(r.get("score") or 0) for r in rubric)
    if not rubric or total <= 0:
        return None
    got = sum(float(r.get("score") or 0) for r in rubric
              if (r.get("expected_evidence") or r.get("description")) in hit)
    return round(got / total, 4)


def judge_answer(full: dict, *, answer: str = "", code: str = "", func_name: str = "",
                 course: str = "ds-algo") -> tuple[dict, str, str]:
    """判一道题 → (结果, 错因 code, 错因说明)。不落库、不认身份，纯判定。

    从 /problems/judge 里抽出来是为了让模考阅卷走同一条路：一张卷子七道综合题
    要是另写一套判法，同一份作答在练习页和模考里会得出不同结论 —— 而两处的
    结果都会写进同一个错题本和同一份掌握度。判题口径必须只有一份。

    `ratio` 是给部分给分用的（模考按分值折算）：代码题按通过样例数，
    综合题按命中得分点的分值占比，客观题只有 0/1。判不了时为 None。

    需要 LLM 诊断的分支会先检索教材证据，讲错因时能指出出处；确定性判定的分支不检索。
    """
    vtype = full["verification_type"]

    def _diag(reference: str) -> tuple[dict, list[dict]]:
        ctx, cites = diagnose_evidence(full["stem"], course, answer)
        return diag_mod.diagnose(full["stem"], answer, reference=reference,
                                 course=course, context=ctx), cites

    if vtype == "unit_test" and full.get("test_cases"):
        func = func_name or full["func_name"]
        vr = run_python_tests(code, func, full["test_cases"])
        total = int(vr.extra.get("total") or 0)
        result = {"mode": "unit_test", "passed": vr.passed, "details": vr.details,
                  "func_name": full["func_name"], "answer": full["answer"],
                  "ratio": round(int(vr.extra.get("passed") or 0) / total, 4) if total else None,
                  # 原始报错只给落库用（错题的 tech_detail），调用方 pop 掉再返回给页面
                  "_tech": vr.extra.get("tech") or ""}
        return result, "reasoning", vr.details

    if vtype == "numeric":
        # 数值/结构化答案：确定性 canonical 校验拿 passed；判不了(None)回退 LLM 概念判定
        vr = check_numeric(answer, _canonical_of(full))
        result = {"mode": "numeric", "passed": vr.passed, "details": vr.details,
                  "answer": full["answer"], "ratio": None}
        if vr.passed is None:
            d, cites = _diag(str(full["answer"]))
            result.update(mode="numeric+llm", passed=bool(d.get("correct")),
                          diagnosis=d.get("diagnosis", ""), error_tag_names=d.get("error_tag_names", []),
                          remediation=d.get("remediation", ""), citations=cites)
            tags = d.get("error_tags") or []
            result["ratio"] = 1.0 if result["passed"] else 0.0
            return result, (tags[0] if tags else "incomplete"), d.get("diagnosis", "")
        result["ratio"] = 1.0 if vr.passed else 0.0
        # 确定性判错时没有 LLM 诊断，只能按答案形态推错因。原先一律记
        # "计算错误"，但选择题选错跟算错是两回事 —— 而这个标签会被
        # /practice/generate 当作出题约束，标错了变式题就打偏。
        return result, error_type_for_canonical(_canonical_of(full)), vr.details

    if vtype == "rule":
        # 概念/分析题：得分点覆盖拿确定性判定，LLM 只补讲解/纠正
        rubric = full.get("rubric") or []
        must_include = [(r.get("expected_evidence") or r.get("description"))
                        for r in rubric if (r.get("expected_evidence") or r.get("description"))]
        vr = check_concept(answer, must_include=must_include)
        d, cites = _diag(str(full["answer"]))
        passed = vr.passed if vr.passed is not None else bool(d.get("correct"))
        result = {"mode": "rule", "passed": passed, "details": vr.details,
                  "diagnosis": d.get("diagnosis", ""), "error_tag_names": d.get("error_tag_names", []),
                  "remediation": d.get("remediation", ""), "answer": full["answer"],
                  "citations": cites,
                  "ratio": _rubric_ratio(rubric, vr.extra.get("hit") or [])}
        tags = d.get("error_tags") or []
        return result, (tags[0] if tags else "incomplete"), d.get("diagnosis", "")

    d, cites = _diag(str(full["answer"]))
    passed = bool(d.get("correct"))
    result = {"mode": "concept", "passed": passed, "diagnosis": d.get("diagnosis", ""),
              "error_tag_names": d.get("error_tag_names", []), "citations": cites,
              "remediation": d.get("remediation", ""), "answer": full["answer"],
              "ratio": 1.0 if passed else 0.0}
    tags = d.get("error_tags") or []
    return result, (tags[0] if tags else "incomplete"), d.get("diagnosis", "")


@router.post("/problems/judge")
def problems_judge(req: JudgeReq, ident: dict = Depends(current_identity)):
    full = db.get_problem(req.problem_id, with_solution=True)
    if not full:
        raise HTTPException(status_code=404, detail="题目不存在")
    result, err_type, err_detail = judge_answer(
        full, answer=req.answer, code=req.code, func_name=req.func_name, course=req.course)
    tech = result.pop("_tech", "")

    if ident["username"]:
        task = _writeback(ident["username"], full, result, err_type, err_detail,
                          problem_id=req.problem_id, answer_text=(req.code or req.answer),
                          hints_used=req.hints_used, duration=req.duration, task_id=req.task_id,
                          tech_detail=tech)
        if task:
            result["review_task"] = {"id": task["id"], "completed": bool(task["completed"]),
                                     "completed_count": task["completed_count"],
                                     "target_count": task["target_count"]}
    return result


def _writeback(username: str, full: dict, result: dict, err_type: str, err_detail: str,
               *, problem_id: int, answer_text: str = "", hints_used: int = 0,
               duration: int = 0, task_id: int = 0, tech_detail: str = "") -> dict | None:
    """判题后落库：作答 → 掌握度 → 错题存销。全系统唯一真闭环，错一处全链路脏。

    原先整段包在 `except Exception: pass` 里。串课 bug 能在库里活那么久，一半
    责任在那两行 —— 写库失败连一行日志都没有。现在失败返回503，不能把未保存的判题
    结果伪装成已成功提交，并留下堆栈供排查。
    """
    try:
        # 判题/模型调用在事务外完成；只将短暂的数据回写置于同一写事务。
        with db.transaction():
            return _writeback_transaction(username, full, result, err_type, err_detail,
                                          problem_id=problem_id, answer_text=answer_text,
                                          hints_used=hints_used, duration=duration,
                                          task_id=task_id, tech_detail=tech_detail)
    except Exception:  # noqa: BLE001
        logger.exception("判题写回失败 user=%s problem_id=%s", username, problem_id)
        # 不再把落库失败伪装成200成功；事务已完整回滚，客户端可明确重试。
        raise HTTPException(status_code=503, detail="学习记录暂未保存，请稍后重试。") from None


def _writeback_transaction(username: str, full: dict, result: dict, err_type: str, err_detail: str,
                           *, problem_id: int, answer_text: str, hints_used: int,
                           duration: int, task_id: int, tech_detail: str) -> dict | None:
    # get_conn在db.transaction范围内复用同一个连接，所有读改写共同提交/回滚。
    passed = bool(result.get("passed"))
    # 先走 problem_knowledge 关联表；没关联才按**本题所属课程**建/取知识点。
    # 这里曾漏传 course，把 co/os/net 的记录挂到 ds-algo 重名空壳点上。
    kid, _ = db.knowledge_id_of_problem(problem_id)
    if not kid:
        kid = db.get_or_create_knowledge_id(full["knowledge"], full["course_id"])

    prev = db.get_mastery(username, kid)
    prev_m = prev["mastery"] if prev else 0.0
    prev_c = prev["confidence"] if prev else 0.0
    # 先按距上次练习的天数把旧掌握度衰减掉，再在衰减后的基础上更新。
    # 不这么做的话，三个月没碰的知识点会带着当初的高分继续往上涨。
    days_since = _days_since(prev.get("last_practiced_at") if prev else None)
    prev_eff = mastery_mod.apply_forgetting(prev_m, days_since)

    # 概念性错因意味着理解岔了而非练得不够 —— 该重讲，不是多刷题。
    # 原先这个参数写死 False，misconception 态永远不可达。
    # 但要求**重复犯**：update_mastery 说的是"稳定的错误概念"，一次答错还谈不上，
    # 而它带来的是 0.28 的重罚（普通错误 0.18）加一个 misconception 状态。
    is_mis = (not passed) and err_type in MISCONCEPTION_TAGS \
        and db.count_open_errors(username, kid, err_type) >= 1
    new_m = mastery_mod.update_mastery(prev_eff, passed, difficulty=full.get("difficulty", 2),
                                       hints_used=hints_used, is_misconception=is_mis)
    # 一并记 knowledge_id：题库题本来能按 problem_knowledge 反查，但冗余记一份
    # 让两条作答来源（题库判题 / 自由问答）在统计口径上完全一致。
    db.save_attempt(username, answer_text[:2000],
                    100.0 if passed else 0.0, hints_used=hints_used,
                    duration=duration, problem_id=problem_id, knowledge_id=kid)
    # 真实作答次数，不是原先的 `2 if prev else 1` —— 伪造的次数让 derive_state
    # 永远看不到"已经练了十次还不会"这种信息。+1 是把刚写入的这次算上。
    n_att = db.count_attempts(username, kid)
    state = mastery_mod.derive_state(new_m, n_att, recent_correct=passed,
                                     has_misconception=is_mis, days_since=0.0)
    db.upsert_mastery(username, kid, new_m, round(min(0.95, prev_c + 0.15), 3), state)

    if passed:
        # 错题本里"已解决"一直恒 0，"错题复习率"这个评审指标因此无米下锅。
        # 掌握度跨过 0.7 就把该点的错题整体销掉：不只是这道题会做了，
        # 而是这个知识点站住了。
        db.resolve_error_records(username, kid, problem_id=problem_id,
                                 all_of_point=(new_m >= 0.7))
    else:
        # careless 举证门槛（本次是错的，不影响"之前做对过没有"的判断）
        err_type = enforce_careless_gate([err_type], db.had_correct_attempt(username, kid))[0]
        db.save_error_record(username, error_type=err_type, diagnosis=err_detail,
                             knowledge=full["knowledge"], problem_stem=full["stem"],
                             problem_id=problem_id, knowledge_id=kid,
                             student_answer=answer_text,
                             review_reason=review_reason_for(err_type, result),
                             tech_detail=tech_detail)
    return review_service.record_attempt(username, task_id, kid, passed, hints_used,
                                         problem_id=problem_id)


# ---------- 助教工作台 ----------
# 助教工作台的准入。**admin 也放行**：教务管理员本来就能建号、改角色、
# 传资料、开课，班级学情反而看不了没有道理 —— 而且他遇到的是 403
# "仅助教/教师可操作"，看起来像权限系统坏了，而不是像一条设计决定。
# 写权限（改判、下发作业）仍然记在操作人名下，admin 做的事同样留痕。
_TEACHING_ROLES = ("ta", "teacher", "admin")


def _require_ta(ident: dict) -> None:
    if ident.get("role") not in _TEACHING_ROLES:
        raise HTTPException(status_code=403, detail="仅助教/教师/教务可操作")


REVIEW_REASON_LABEL = {
    "student_request": "学生申请复核", "verify_failed": "验证未通过",
    "no_evidence": "未检索到教材依据", "careless": "判为粗心（需核实）", "manual": "普通错题",
}

# 演示库里残留的占位作答，不当成学生写的东西给助教看
_PLACEHOLDER_ANSWERS = {"演示作答"}


def _error_row(r: dict) -> dict:
    """一条错题在助教侧的形状。复核队列与总览共用，两处分叉的话
    改判按钮在一处能用、另一处对不上字段。

    复核要做判断，就得看到判断的依据：题目、学生写了什么、AI 判了什么、为什么在队列里
    （docs/34 §3.6）。此前只给一句截到 160 字的诊断，题干被切掉，学生答案根本没有。"""
    stem, diag = db.split_error_text(r)
    answer = (r.get("student_answer") or "").strip()
    if answer in _PLACEHOLDER_ANSWERS:
        answer = ""
    reason = r.get("review_reason") or "manual"
    return {"id": r["id"], "username": r["username"], "error_type": r["error_type"],
            "error_name": name_of(r["error_type"]),
            "follow_up": follow_up_of(r["error_type"]),
            "problem_stem": r.get("problem_stem") or stem,
            "student_answer": answer,
            "reference": r.get("reference") or "",
            "knowledge": r.get("knowledge") or "",
            "diagnosis": diag,
            "review_reason": reason, "review_reason_label": REVIEW_REASON_LABEL.get(reason, reason),
            "review_requested_at": r.get("review_requested_at") or "",
            "created_at": r["created_at"],
            "ta_reviewed": bool(r["ta_reviewed"]), "ta_verdict": r["ta_verdict"],
            "ta_note": r["ta_note"], "ta_username": r["ta_username"],
            "ai_error_name": name_of(r["ai_error_type"]) if r["ai_error_type"] else "",
            # 代码运行失败的原始报错（I-05）。只在助教侧下发，复核卡片里默认折叠
            "tech_detail": r.get("tech_detail") or ""}


@router.get("/teaching/today")
def teaching_today(course: str = "ds-algo", class_name: str = "",
                   ident: dict = Depends(current_identity)):
    """助教登录后的第一屏：今天先做什么（docs/35 P1-6）。"""
    _require_ta(ident)
    if not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    return classroom.today(course, class_name.strip())


@router.get("/teaching/overview")
def teaching_overview(course: str = "", class_name: str = "",
                      ident: dict = Depends(current_identity)):
    """班级总览。**按课、按班算**——不传就是全体（保留旧行为）。

    原先四个数是全库 COUNT：助教切到"计算机网络"看到的仍是四门之和，
    而"活跃学生"是历史上做过任何一题的人，开课两个月后永远满格。
    """
    _require_ta(ident)
    if course and not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    class_name = class_name.strip()
    stats = db.class_stats(course, class_name)
    common = [{"error_type": r["error_type"], "error_name": name_of(r["error_type"]),
               "count": r["cnt"]}
              for r in db.aggregate_error_types(course_id=course, class_name=class_name)]
    rows, pending = db.recent_errors(15, course_id=course, class_name=class_name)
    _, unreviewed = db.recent_errors(1, course_id=course, class_name=class_name, reviewed="no")
    error_tags = [{"code": code, "name": info["name"], "follow_up": info["follow_up"]}
                  for code, info in ERROR_TAXONOMY.items()]
    return {"stats": stats, "common_errors": common,
            "recent": [_error_row(r) for r in rows],
            "classes": db.list_classes(), "course": course, "class_name": class_name,
            "error_total": pending, "unreviewed_total": unreviewed,
            "error_tags": error_tags}


@router.get("/teaching/errors")
def teaching_errors(course: str = "", class_name: str = "", reviewed: str = "",
                    limit: int = 20, offset: int = 0, reason: str = "",
                    ident: dict = Depends(current_identity)):
    """错题复核队列：能筛未复核、能按班按课、能翻页。

    此前助教只能复核总览里那固定 15 条（`recent_errors` 的 SQL 一个 WHERE 都没有），
    想系统性复核一个班的错题做不到。

    总数与这一页一起返回：只给"下一页"而不给"还剩多少"的话，助教不知道这个班
    还有几条要处理，而那正是他要的数。
    """
    _require_ta(ident)
    if course and not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    if reviewed not in ("", "yes", "no"):
        raise HTTPException(status_code=400, detail="reviewed 只能是 yes / no / 空")
    limit = max(1, min(100, limit))
    if reason and reason not in REVIEW_REASON_LABEL:
        raise HTTPException(status_code=400, detail="未知的复核原因")
    rows, total = db.recent_errors(limit, course_id=course, class_name=class_name.strip(),
                                   reviewed=reviewed, offset=max(0, offset), reason=reason)
    return {"items": [_error_row(r) for r in rows], "total": total,
            "limit": limit, "offset": max(0, offset)}


@router.get("/teaching/heatmap")
def teaching_heatmap(course: str = "ds-algo", class_name: str = "",
                     ident: dict = Depends(current_identity)):
    """班级 × 知识点掌握度热力图 + 该门的共性错因。

    与 /teaching/overview 分开是因为它按科目取数、数据量也大得多：
    总览要秒开，热力图是点进去才看的那一屏。
    """
    _require_ta(ident)
    if not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    data = classroom.heatmap(course, class_name.strip())
    data["common_errors"] = classroom.common_errors(course, class_name=class_name.strip())
    return data


@router.get("/teaching/student/{username}")
def teaching_student(username: str, course: str = "ds-algo",
                     ident: dict = Depends(current_identity)):
    """一个学生的完整画像。热力图上点开某一行看到的就是这个。

    在这之前，助教能看出"张三整行都暗"却**点不进去** —— 全站学生数据端点一律从
    `ident["username"]` 取人，没有任何一个接受"要看谁"。于是从"发现问题"到
    "采取行动"之间是断的：看不到他错了哪几道题、摸底什么水平、复习计划排到哪。

    三条纪律：

    1. **每次访问都留痕。** 现在任何 ta/teacher 能看任何课任何班（没有 enrollment
       模型，见 docs/27）。这个端点把一个具体的人摊开，在有权限模型之前，
       至少要能事后查"谁在什么时候看了谁" —— 记一条日志，而不是假装有权限模型。
    2. **不给密码材料、不给头像。** `db.list_users` 已经立了这个规矩，这里沿用：
       payload 只取展示用的四个字段。
    3. **错题带上复核结论**，否则两个助教会重复复核同一条。
    """
    _require_ta(ident)
    if not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    profile = db.get_user_profile(username)
    if not profile or profile.get("role") != "student":
        raise HTTPException(status_code=404, detail=f"学生 {username} 不存在")
    logger.info("teaching_student viewer=%s role=%s target=%s course=%s",
                ident.get("username"), ident.get("role"), username, course)

    act = db.student_activity(course, username=username)
    errors = db.list_error_records(username, limit=20, course=course)
    points = profile_mod.build_profile(username, course)
    placement = None
    sess = db.latest_session(username, course, "placement")
    if sess:
        try:
            rep = placement_mod.build_result(sess["id"])
            placement = {"level": rep.get("level"), "level_name": rep.get("level_name"),
                         "finished_at": sess.get("completed_at") or "",
                         "covered": rep.get("covered")}
        except Exception:  # noqa: BLE001 —— 一次摸底重算不出来不该让整个画像打不开
            logger.exception("摸底报告重算失败 session=%s", sess["id"])

    return {
        "profile": {k: profile.get(k, "") for k in
                    ("username", "name", "student_id", "class_name")},
        "activity": act[0] if act else {"attempts": 0, "correct_n": 0, "last_at": ""},
        # 与学生自己在首页 / 记录页看到的同一口径（profile.summarize_mastery）
        "mastery": profile_mod.practiced_points(points),
        "mastery_summary": profile_mod.summarize_mastery(points),
        "errors": [{"id": e["id"], "error_type": e["error_type"],
                    "error_name": name_of(e.get("error_type", "")),
                    "knowledge": e.get("knowledge") or "",
                    "diagnosis": (e.get("diagnosis") or "").split(" || ")[-1][:160],
                    "resolved": bool(e.get("resolved")),
                    "ta_reviewed": bool(e.get("ta_reviewed")),
                    "ta_note": e.get("ta_note") or "",
                    "created_at": e.get("created_at") or ""} for e in errors],
        "placement": placement,
        "review": review_service.progress(username, course),
    }


@router.get("/teaching/export")
def teaching_export(kind: str = "mastery", course: str = "ds-algo", class_name: str = "",
                    assignment_id: int = 0, ident: dict = Depends(current_identity)):
    """把班级数据导成 CSV。kind: mastery / assignment / errors。

    此前没有任何导出出口，老师看完只能截图 —— 而截出来的数据我们也留不下。

    **用 utf-8-sig（带 BOM）**：不带的话 Excel 在中文 Windows 上按 GBK 猜编码，
    打开就是一屏乱码，而老师看到乱码不会去查编码，只会认为"导出坏了"。
    """
    _require_ta(ident)
    if not is_valid_course(course):
        raise HTTPException(status_code=400, detail="课程不存在")
    try:
        header, rows = classroom.export_rows(kind, course, class_name.strip(), assignment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_user_detail(exc)) from exc

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    stamp = date.today().isoformat()
    label = {"mastery": "掌握度", "assignment": "作业完成情况", "errors": "错题清单"}[kind]
    name = f"CourseLoop-{label}-{class_name or '全体'}-{stamp}.csv"
    # 文件名里有中文，必须走 RFC 5987 的 filename*，否则浏览器存下来是一串乱码
    return Response(
        content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=export-{stamp}.csv; "
                 f"filename*=UTF-8''{quote(name)}"})


class AssignmentReq(BaseModel):
    course: str = "ds-algo"
    title: str = ""
    note: str = ""
    knowledge_ids: list[int] = []
    target_count: int = 3
    class_name: str = ""
    due_date: str = ""
    # 这批知识点是被哪个班级共性错因挑出来的。留空 = 助教自己挑的，
    # 界面上就照实说"助教手动创建"，不给它编一个数据依据。
    source_error_type: str = ""


class AssignmentIdReq(BaseModel):
    assignment_id: int


@router.get("/teaching/assignments")
def teaching_assignments(course: str = "ds-algo", ident: dict = Depends(current_identity)):
    _require_ta(ident)
    return {"course": course, "classes": db.list_classes(),
            "items": [classroom.assignment_view(a) for a in db.list_assignments(course)]}


@router.post("/teaching/assignments")
def teaching_assignment_create(req: AssignmentReq, ident: dict = Depends(current_identity)):
    _require_ta(ident)
    if not is_valid_course(req.course):
        raise HTTPException(status_code=400, detail="课程不存在")
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="作业需要一个标题")
    # 知识点必须属于这门课：挂错门的作业在学生那边一条都匹配不上作答，
    # 完成度会永远是 0，而且看不出是哪里错了。
    valid = {p["id"] for p in db.list_knowledge_points(req.course)}
    ids = [k for k in dict.fromkeys(req.knowledge_ids) if k in valid]
    if not ids:
        raise HTTPException(status_code=400, detail="请至少选择一个属于该课程的知识点")
    # 不在码表里的来源一律按"手动创建"处理而不是报错：来源是说明性的，
    # 不该让一个拼错的标签把整次下发挡掉。
    source = req.source_error_type if req.source_error_type in ERROR_TAXONOMY else ""
    aid = db.create_assignment(ident["username"], req.course, title, ids,
                               note=req.note.strip()[:300],
                               target_count=max(1, min(20, req.target_count)),
                               class_name=req.class_name.strip(),
                               due_date=req.due_date.strip(),
                               source_error_type=source)
    return classroom.assignment_view(db.get_assignment(aid))


@router.post("/teaching/assignments/close")
def teaching_assignment_close(req: AssignmentIdReq, ident: dict = Depends(current_identity)):
    _require_ta(ident)
    if not db.get_assignment(req.assignment_id):
        raise HTTPException(status_code=404, detail="作业不存在")
    return {"ok": db.close_assignment(req.assignment_id)}


@router.get("/assignments")
def my_assignments(course: str = "ds-algo", ident: dict = Depends(current_identity)):
    """学生这边的作业列表。完成度由作答算出来，不需要"领取"这个动作。"""
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    return {"course": course, "items": classroom.student_assignments(ident["username"], course)}


class ReviewErrorReq(BaseModel):
    record_id: int
    verdict: str          # "confirm" | "override"
    error_type: str = ""  # verdict=override 时必填，取 error_taxonomy 的 code
    note: str = ""


@router.post("/teaching/review-error")
def teaching_review_error(req: ReviewErrorReq, ident: dict = Depends(current_identity)):
    _require_ta(ident)
    if req.verdict not in ("confirm", "override"):
        raise HTTPException(status_code=400, detail="verdict 只能是 confirm 或 override")
    if req.verdict == "override" and req.error_type not in ERROR_TAXONOMY:
        raise HTTPException(status_code=400, detail="error_type 不在错因分类表中")
    before = db.get_error_record(req.record_id)
    row = db.review_error_record(req.record_id, ident["username"], req.verdict,
                                 error_type=req.error_type, note=req.note.strip()[:200])
    if not row:
        raise HTTPException(status_code=404, detail="错题记录不存在")
    impact = _override_impact(before, row) if req.verdict == "override" else []
    return {"id": row["id"], "error_type": row["error_type"], "error_name": name_of(row["error_type"]),
            "follow_up": follow_up_of(row["error_type"]),
            "ta_verdict": row["ta_verdict"], "ta_reviewed": bool(row["ta_reviewed"]),
            "impact": impact}


STATE_LABEL = {"not_started": "未开始", "learning": "学习中", "fragile": "不稳固",
               "mastered": "已掌握", "needs_review": "待复习", "misconception": "概念错误"}


def _override_impact(before: dict | None, after: dict) -> list[str]:
    """改判对下游的真实影响，逐条说清（docs/34 §3.6：此前只改一个标签字段）。

    - 掌握度**状态**：概念类错因（MISCONCEPTION_TAGS）会把知识点钉在"概念错误"。
      改进 / 改出这一类时，按该点剩余的未解决错题重算状态，数值本身不动 ——
      罚分当时已经扣了，事后按比例退回是猜，不做。
    - 后续出题：变式题 / 推荐练习读的是最新错因，改判后按新错因出。
    - 复习排期只看掌握度数值与错题条数，不看错因类型 —— 这次改判不改排期，照实说。
    """
    if not before:
        return []
    old, new = before.get("error_type") or "", after.get("error_type") or ""
    if old == new:
        return ["错因没有变化，下游不受影响。"]
    out = [f"后续推荐练习与变式题按「{name_of(new)}」出题（原为「{name_of(old)}」）。"]
    user, kid = after.get("username") or "", int(after.get("knowledge_id") or 0)
    if kid and ((old in MISCONCEPTION_TAGS) != (new in MISCONCEPTION_TAGS)):
        m = db.get_mastery(user, kid)
        if m:
            has_mis = db.has_open_misconception(user, kid, MISCONCEPTION_TAGS)
            days = 0.0
            if m.get("last_practiced_at"):
                try:
                    days = max(0.0, (datetime.now() - datetime.strptime(
                        m["last_practiced_at"][:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400)
                except ValueError:
                    days = 0.0
            state = mastery_mod.derive_state(m["mastery"] or 0.0, db.count_attempts(user, kid),
                                             db.last_attempt_correct(user, kid), has_mis, days)
            if state != m["state"]:
                db.set_mastery_state(user, kid, state)
                out.append(f"该知识点状态：{STATE_LABEL.get(m['state'], m['state'])} → "
                           f"{STATE_LABEL.get(state, state)}。")
            elif state == "misconception":
                out.append("该知识点仍为「概念错误」：这个点上还有其他未解决的概念类错题。")
            else:
                out.append(f"该知识点状态仍为「{STATE_LABEL.get(state, state)}」，不受这次改判影响。")
    out.append("复习排期只看掌握度与错题数量，这次改判不改变排期。")
    return out

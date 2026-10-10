"""教务管理接口（`/admin/*`）：课程、用户、资料入库。

单独成文件而不是塞进 `core_routes.py`（已 1300+ 行）：这批端点的受众、
权限和生命周期都和学生/助教那批不一样 —— 它们是教务在开课、管人时用的，
改动频率也不同。

**权限**：一律 `admin` 角色。公网 `/auth/register` 仍然只能建 student
（`auth.py:RegisterReq` 特意没有 role 字段），管理员由 `scripts/manage_users.py`
或另一个管理员创建 —— 提权路径始终不经过公网表单。
"""
from __future__ import annotations

import logging
import os

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
                     UploadFile)
from pydantic import BaseModel, Field

from backend.core import courses as courses_mod
from backend.core import db
from backend.core.config import settings
from backend.core.courses import Course
from backend.core.security import ROLES, current_identity

def require_admin(ident: dict = Depends(current_identity)) -> dict:
    """管理员守卫（`admin` 角色）。**挂在路由器上**，不是每个端点自己调。

    两个理由：

    1. 挂在路由器上就**不可能漏**。逐个端点自己调的写法漏过一次（新加的那个端点
       忘了写守卫），而漏了没有任何症状 —— 直到有人发现非管理员也读得到。
    2. 依赖在**请求体校验之前**求解。写成端点内的第一行时，一个非管理员发来
       格式不对的 body 会先拿到 422（"你的字段不合法"），这既泄露了本不该看到的
       校验细节，也让"未授权"变成了排在第二位的结论。
    """
    if ident.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅教务管理员可操作")
    return ident


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class CourseReq(BaseModel):
    """一门课的完整定义。

    刻意做成**整体覆盖**而不是部分更新：知识点树、错因示例这些是嵌套结构，
    PATCH 语义下"传了一半的 taxonomy 算什么意思"没有好答案，而答错的后果是
    悄悄丢掉几个章节。整体覆盖让前端必须先读再写，也让每次写入都是一个完整快照。
    """
    id: str = Field(min_length=1, max_length=32)
    display: str = Field(min_length=1, max_length=80)
    short: str = Field(min_length=1, max_length=20)
    exam: str = Field(default="", max_length=40)
    score: int = Field(default=0, ge=0, le=1000)
    code_enabled: bool = False
    verification_types: list[str] = Field(default_factory=list)
    knowledge_taxonomy: list[dict] = Field(default_factory=list)
    error_examples: dict[str, list[str]] = Field(default_factory=dict)
    sample_questions: list[str] = Field(default_factory=list)
    answer_format: str = ""
    sort_order: int | None = None
    enabled: bool = True

    def to_course(self) -> Course:
        return Course(
            id=self.id.strip(), display=self.display.strip(), short=self.short.strip(),
            exam=self.exam.strip(), score=self.score, code_enabled=self.code_enabled,
            verification_types=self.verification_types,
            knowledge_taxonomy=self.knowledge_taxonomy,
            error_examples=self.error_examples,
            sample_questions=self.sample_questions,
            answer_format=self.answer_format,
        )


def _validate(req: CourseReq) -> None:
    """把会安静产生坏数据的输入挡在写库之前。"""
    from backend.verification.base import VERIFICATION_TYPES

    if not req.id.strip().replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="课程 id 只允许字母数字与 - _")
    unknown = [t for t in req.verification_types if t not in VERIFICATION_TYPES]
    if unknown:
        # 这条不是形式主义。上一轮踩过：LLM 自造的 verification_type="choice"
        # 不在词表里，进库后被当概念题按关键词覆盖判分 —— 题量涨了、判分口径悄悄降级。
        raise HTTPException(
            status_code=400,
            detail=f"未知的验证方式 {unknown}；可选：{sorted(VERIFICATION_TYPES)}")
    if req.code_enabled and "unit_test" not in req.verification_types:
        raise HTTPException(status_code=400,
                            detail="code_enabled=true 的课程必须包含 unit_test 验证方式")
    for ch in req.knowledge_taxonomy:
        if not isinstance(ch, dict) or not str(ch.get("chapter", "")).strip():
            raise HTTPException(status_code=400, detail="知识点树的每一项都要有非空 chapter")
        if not isinstance(ch.get("topics"), list) or not ch["topics"]:
            raise HTTPException(status_code=400,
                                detail=f"章节「{ch.get('chapter')}」的 topics 不能为空")


def _knowledge_audit(course_id: str) -> dict:
    """改完大纲后，报出库里已有知识点还有多少对不上。

    改 `knowledge_taxonomy` 会让已有题目的知识点悬空 —— 这不会报错，只会让
    掌握度聚合到一批没有章节归属的孤儿节点上。所以每次写课程都跟一次审计，
    并把"未归类"条数如实返回给管理员，不静默吞掉。
    """
    from scripts.knowledge_align import UNCLASSIFIED, KnowledgeAligner

    aligner = KnowledgeAligner(course_id)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM knowledge_points WHERE course_id=?", (course_id,)).fetchall()
    names = [r["name"] for r in rows]
    unclassified, chapter_fallback = [], []
    for name in names:
        topic, _chapter, _diff = aligner.align(name)
        if topic == UNCLASSIFIED:
            unclassified.append(name)
        elif topic.endswith("·其他"):
            chapter_fallback.append(name)
    return {
        "existing_knowledge_points": len(names),
        "unclassified": len(unclassified),
        "unclassified_samples": unclassified[:10],
        "chapter_fallback": len(chapter_fallback),
        "chapter_fallback_samples": chapter_fallback[:10],
        "hint": ("大纲变更后建议跑 `python -m scripts.seed_knowledge --course "
                 f"{course_id}` 补齐新章节的知识点" if names else ""),
    }


@router.get("/courses")
def admin_list_courses(ident: dict = Depends(require_admin)):
    return {"items": courses_mod.list_all_courses(),
            "exam_structure": courses_mod.exam_structure()}


@router.post("/courses")
def admin_create_course(req: CourseReq, ident: dict = Depends(require_admin)):
    _validate(req)
    cid = req.id.strip()
    if cid in {c["id"] for c in courses_mod.list_all_courses()}:
        raise HTTPException(status_code=400, detail=f"课程 {cid} 已存在，改用 PUT")
    courses_mod.upsert_course(req.to_course(), sort_order=req.sort_order, enabled=req.enabled)
    return {"ok": True, "id": cid, "knowledge_audit": _knowledge_audit(cid)}


@router.put("/courses/{course_id}")
def admin_update_course(course_id: str, req: CourseReq, ident: dict = Depends(require_admin)):
    if course_id != req.id.strip():
        raise HTTPException(status_code=400, detail="路径里的 course_id 与请求体的 id 不一致")
    _validate(req)
    if course_id not in {c["id"] for c in courses_mod.list_all_courses()}:
        raise HTTPException(status_code=404, detail=f"课程 {course_id} 不存在")
    courses_mod.upsert_course(req.to_course(), sort_order=req.sort_order, enabled=req.enabled)
    return {"ok": True, "id": course_id, "knowledge_audit": _knowledge_audit(course_id)}


@router.delete("/courses/{course_id}")
def admin_disable_course(course_id: str, ident: dict = Depends(require_admin)):
    if not courses_mod.set_course_enabled(course_id, False):
        raise HTTPException(status_code=404, detail=f"课程 {course_id} 不存在")
    # 明说是停用而不是删除：题目与掌握度记录都还在，重新启用即原样回来。
    return {"ok": True, "id": course_id, "disabled": True,
            "note": "已停用（题库与学习记录保留，重新启用即恢复）"}


# ---------- 用户管理 ----------
class CreateUserReq(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(default="", max_length=40)
    role: str = "student"
    student_id: str = Field(default="", max_length=40)
    college: str = Field(default="", max_length=80)
    major: str = Field(default="", max_length=80)
    class_name: str = Field(default="", max_length=80)


class UpdateUserReq(BaseModel):
    role: str | None = None
    status: str | None = None


@router.get("/users")
def admin_list_users(role: str = "", status: str = "", q: str = "",
                     ident: dict = Depends(require_admin)):
    items = db.list_users(role=role, status=status, keyword=q)
    by_role: dict[str, int] = {}
    for u in items:
        by_role[u["role"]] = by_role.get(u["role"], 0) + 1
    return {"items": items, "total": len(items), "by_role": by_role,
            "roles": list(ROLES), "statuses": list(db.USER_STATUSES)}


@router.post("/users")
def admin_create_user(req: CreateUserReq, ident: dict = Depends(require_admin)):
    if req.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"未知角色 {req.role}；可选 {list(ROLES)}")
    ok, msg = db.register_user(
        req.username, req.password, req.name, role=req.role,
        student_id=req.student_id, college=req.college,
        major=req.major, class_name=req.class_name)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    # 回读确认角色真的落库了。register_user 对不认识的角色是静默降级为 student 的
    # （那是给公网注册路径留的防线），管理页这条路上静默降级意味着建出一个
    # 打 /admin/* 全 403 的"管理员"，而现场看起来像是权限系统坏了。
    created = db.list_users(keyword=req.username)
    got = next((u for u in created if u["username"] == req.username.strip()), None)
    if not got or got["role"] != req.role:
        raise HTTPException(status_code=500,
                            detail=f"角色未按预期落库：请求 {req.role}，实际 {got and got['role']}")
    return {"ok": True, "user": got}


@router.put("/users/{username}")
def admin_update_user(username: str, req: UpdateUserReq,
                      ident: dict = Depends(require_admin)):
    if db.user_status(username) is None:
        raise HTTPException(status_code=404, detail=f"用户 {username} 不存在")

    # 管理员不能停用或降级自己。不是怕误操作，是怕**把最后一个管理员锁在门外**——
    # 那之后没有任何接口能把权限拿回来，只能登到机器上跑 scripts/manage_users。
    if username == ident["username"] and (req.role not in (None, "admin")
                                          or req.status == "disabled"):
        raise HTTPException(status_code=400, detail="不能停用或降级自己的管理员账号")

    if req.role is not None:
        if req.role not in ROLES:
            raise HTTPException(status_code=400, detail=f"未知角色 {req.role}；可选 {list(ROLES)}")
        if not _other_admin_remains(username, req.role, ident):
            raise HTTPException(status_code=400, detail="至少要保留一个启用中的管理员")
        db.set_user_role(username, req.role)
    if req.status is not None:
        if req.status not in db.USER_STATUSES:
            raise HTTPException(status_code=400, detail=f"未知状态 {req.status}")
        db.set_user_status(username, req.status)

    got = next((u for u in db.list_users(keyword=username) if u["username"] == username), None)
    note = ("角色写在令牌里，该用户重新登录后新角色才生效"
            if req.role is not None else "")
    return {"ok": True, "user": got, "note": note}


class ClassBatchReq(BaseModel):
    usernames: list[str] = Field(default_factory=list, max_length=500)
    class_name: str = Field(default="", max_length=80)


@router.post("/users/class")
def admin_set_class(req: ClassBatchReq, ident: dict = Depends(require_admin)):
    """批量改班。这是纠正历史脏数据的唯一出口。

    班级是学生注册时自己填的自由文本，"计科2101" / "计科 2101" / "计算机2101"
    会变成三个班 —— 而热力图、作业下发全按它精确匹配分组。没有这个出口，
    一个填错班名的学生就永远不在他该在的那一栏里，而助教改不了。

    传空 class_name 是合法的：把人从班里摘出去（转班前的中间态）。
    """
    done, missing = [], []
    for name in req.usernames:
        name = (name or "").strip()
        if not name:
            continue
        (done if db.set_user_class(name, req.class_name) else missing).append(name)
    return {"ok": True, "updated": len(done), "usernames": done,
            "missing": missing, "class_name": req.class_name.strip()}


# CSV 导入认得的列。**role 不在其中**，理由与 `auth.py:RegisterReq` 拿掉 role
# 字段完全相同：一份从邮件里下载来的表格不该成为提权路径。CSV 里写了 role
# 也一律忽略并在结果里说明，而不是静默丢弃 —— 静默会让人以为它生效了。
_IMPORT_COLUMNS = ("username", "name", "student_id", "college", "major",
                   "class_name", "password")


def _import_rows(text: str) -> tuple[list[dict], str]:
    """解析 CSV。返回 (行, 警告)。首行必须是表头，且必须含 username 列。"""
    import csv
    import io

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], "文件是空的"
    header = [(h or "").strip().lstrip("\ufeff") for h in reader.fieldnames]
    if "username" not in header:
        return [], f"缺少 username 列（读到的表头：{'、'.join(header) or '无'}）"
    unknown = [h for h in header if h and h not in _IMPORT_COLUMNS]
    rows = []
    for raw in reader:
        rows.append({k: str(raw.get(k) or "").strip()
                     for k in _IMPORT_COLUMNS if k in header})
    warn = ""
    if unknown:
        warn = f"以下列不认识，已忽略：{'、'.join(unknown)}"
        if "role" in unknown:
            warn += "。role 一律按 student 处理，助教和教务账号要单独创建"
    return rows, warn


def _preflight(username: str, student_id: str) -> tuple[bool, str]:
    """预览时的查重，判据与 `db.register_user` 保持一致（用户名唯一 + 学号唯一）。

    两边分叉的代价很具体：预览说能建、执行时被拦，或者反过来。所以这里不另写
    规则，只是把 register_user 那两条查重提前跑一遍。
    """
    if db.user_status(username) is not None:
        return False, "用户名已存在"
    if student_id and any(u["student_id"] == student_id
                          for u in db.list_users(keyword=student_id)):
        return False, "该学号已注册过，请勿重复注册"
    return True, "将新建"


@router.post("/users/import")
async def admin_import_users(
    file: UploadFile = File(...),
    apply: bool = Form(False),
    default_password: str = Form(""),
    ident: dict = Depends(require_admin),
):
    """CSV 批量建号。**默认只预览，显式 apply=true 才写库。**

    与本仓其他会改库的入口同一套纪律（`bootstrap_demo.py` / `fix_choice_canonicals.py`
    都是先 dry-run）：一次导入 30 个账号，看不到"将建几个、跳过几个、为什么跳过"
    就按下去，出错之后没有干净的回退方式 —— 建出来的账号无法批量撤销。

    **逐行独立**：一行学号重复不该让另外 29 个建不成。每行给出 ok/skip 与原因。

    **一律建成 student**：见 `_IMPORT_COLUMNS` 的说明。

    密码来源三选一：行内 password 列 → default_password → 用学号/用户名兜底生成。
    建成之后**在响应里原样返回**，因为这是教务把账号发给学生的唯一渠道 ——
    系统没有邮件能力（见 docs/27），不返回就等于建了一批没人知道密码的账号。
    """
    payload = await file.read()
    if len(payload) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="名单文件不应超过 2 MB")
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise HTTPException(status_code=400, detail="无法解码，请存成 UTF-8 或 GBK 的 CSV")

    rows, warn = _import_rows(text)
    if not rows:
        raise HTTPException(status_code=400, detail=warn or "没有读到任何数据行")

    results: list[dict] = []
    seen: set[str] = set()
    for idx, row in enumerate(rows, start=2):        # 2 = 表头之后的第一行
        username = row.get("username", "")
        item = {"line": idx, "username": username, "name": row.get("name", ""),
                "class_name": row.get("class_name", ""), "ok": False, "reason": ""}
        if not username:
            item["reason"] = "username 为空"
        elif username in seen:
            # 同一份文件里重复：先建的那个会成功，后一个报"已存在"，看着像库里早有。
            # 说清楚是文件自身重复，教务才知道要去改名单而不是去查库。
            item["reason"] = "本次文件内重复"
        else:
            pw = row.get("password") or default_password or f"cl{row.get('student_id') or username}"
            item["password"] = pw
            if len(pw) < 6:
                item["reason"] = "密码至少 6 位"
            elif not apply:
                # 预览必须**照着 register_user 的判据查一遍重**，不能一律报"将新建"。
                # 不查的话，预览说"将建 12 个"、确认之后实际建出 0 个 —— 而预览的
                # 全部意义就是让人在写库之前看到会发生什么。
                item["ok"], item["reason"] = _preflight(username, row.get("student_id", ""))
            else:
                ok, msg = db.register_user(
                    username, pw, row.get("name", ""), role="student",
                    student_id=row.get("student_id", ""), college=row.get("college", ""),
                    major=row.get("major", ""), class_name=row.get("class_name", ""))
                item["ok"], item["reason"] = ok, ("已建号" if ok else msg)
        seen.add(username)
        results.append(item)

    created = [r for r in results if r["ok"]]
    return {"ok": True, "applied": bool(apply), "total": len(results),
            "created": len(created), "skipped": len(results) - len(created),
            "warning": warn, "items": results,
            "note": "" if apply else "这是预览，没有写库；确认无误后再执行"}


def _other_admin_remains(username: str, new_role: str, ident: dict) -> bool:
    """把某个管理员降级之后，是否还剩至少一个启用中的管理员。"""
    if new_role == "admin":
        return True
    admins = [u for u in db.list_users(role="admin", status="active")
              if u["username"] != username]
    return bool(admins)


# ---------- 资料入库 ----------
# 允许的后缀 → parser。与 rag/ingest.py 的 RAG_DOCTYPE_PARSER 是两把不同的尺子：
# 那把按目录（类别）选，这把按后缀选 —— 上传时后缀是我们唯一能确定的东西。
#
# 收 .docx 与 .txt 不只是"多支持一种格式"：老师手上的讲义、题目解析、勘误
# 大多就是 Word 和记事本文件，只收 PDF/PPT 等于把最常见的一类资料挡在外面。
# 两点代价说在前面（页面上也照实写了）：
#   - Word 和 txt **没有页码**，引用里只能给到标题路径（第3章 / 3.2 CSMA/CD），
#     不会出现"第 87 页"。所以按标题切块而不是固定长度切（chunk_by_heading）。
#   - 老的二进制 .doc 不收：python-docx 读不了，要另存为 .docx。与其收下再
#     失败一次，不如在选文件时就说清楚。
# PDF 仍然只认**数字版**（有文本层）。扫描件收得进来但解析出 0 块，落 failed
# 并写明需要先 OCR —— 这条不改成直接拒收，因为"有没有文本层"在上传时看不出来。
_EXT_PARSER = {".pdf": "structured", ".pptx": "pptx", ".ppt": "pptx",
               ".docx": "docx", ".txt": "text"}
_DOC_TYPES = ("textbook", "exercise", "courseware")
MAX_UPLOAD_BYTES = 80 * 1024 * 1024


def _run_ingest(job_id: int, item: dict, raw_dir: str, fts_path: str) -> None:
    """后台跑一份文件的解析与索引。异常一律记进任务行，不往上抛。

    用 FastAPI 的 BackgroundTasks 而不是引 Celery：这是单进程部署，
    多一个消息队列就多一份要运维的东西，而这里的任务是"几十秒、可重试、
    丢了重传一次即可"，配不上一套队列。代价是进程重启时 running 的任务会
    停在 running —— 列表里如实显示，教务重传即可。
    """
    from backend.rag import indexer

    # 解析出 0 块的原因按格式分开说。PDF 十有八九是扫描件（没有文本层），
    # 处置是先 OCR；Word/txt 解析不出东西则完全是另一回事（空文件、内容全在
    # 图片里、或是被误改了后缀的二进制），套用"请先 OCR"只会把人引到错路上。
    empty_reason = {
        "structured": "解析出 0 个分块（多半是没有文本层的扫描件，需要先 OCR 成数字版 PDF）",
        "pptx": "解析出 0 个分块（幻灯片里没有文字，多半整页都是图片）",
        "docx": "解析出 0 个分块（文档里没有可提取的文字，可能是空文件，或内容都在图片里）",
        "text": "解析出 0 个分块（文件是空的，或内容不是文本）",
    }.get(item.get("parser", ""), "解析出 0 个分块")

    db.update_ingest_job(job_id, status="running")
    conn = None
    try:
        conn = indexer.connect(fts_path)
        indexer.backfill_hashes(conn)
        stat = indexer.index_one(conn, item, raw_dir)
        note = ""
        if not stat["parsed"] and item.get("parser") == "structured":
            stat, note = _ocr_fallback(conn, item, raw_dir, stat)
        indexer.stamp_meta(conn, "admin_upload", raw_dir)
        db.update_ingest_job(
            job_id, status="done" if stat["parsed"] else "failed",
            error=note if stat["parsed"] else (note or empty_reason),
            parsed_chunks=stat["parsed"], indexed_chunks=stat["written"],
            duplicate_chunks=stat["duplicate"], finished_at=db._now())
        # 索引变了，进程内缓存的引擎还指着旧的连接，下一次检索要能看到新内容
        from backend.rag import retriever
        retriever.reset_engines()
    except Exception as exc:  # noqa: BLE001
        # 异常原文（类名、库报错、路径）只进日志；页面上给教务一句能照着处置的话
        logger.exception("ingest job %s failed", job_id)
        db.update_ingest_job(job_id, status="failed",
                             error="入库时出错了。可以重试一次；仍然失败的话，请换一份文件或联系技术支持。",
                             finished_at=db._now())
    finally:
        if conn is not None:
            conn.close()


@router.get("/runtime")
def runtime_status():
    """运行时详情，仅管理员：原先挂在公开的 /health 上。

    scripts/preflight_demo.py 用它核对"正在运行的服务"是哪个提交（docs/34 §2.1）。
    """
    from backend.core.build_info import build_info

    rag_ready = False
    if settings.rag_mode != "off":
        try:
            from backend.rag.retriever import get_engine
            rag_ready = get_engine().has_corpus()
        except Exception:  # noqa: BLE001
            rag_ready = False
    return {
        "status": "ok",
        "llm_ready": settings.llm_ready and settings.text_llm_ready,
        "vision_ready": settings.vision_ready,
        "workhorse_model": settings.workhorse_model or None,
        "chroma_collection": settings.chroma_collection,
        "rag_mode": settings.rag_mode,
        "rag_ready": rag_ready,
        "build": build_info(),
    }


# 任务行里的服务器路径和解析器名只给后端自己用，不下发到页面
_JOB_PRIVATE = ("stored_path", "origin_path", "parser")


def _public_job(job: dict | None) -> dict | None:
    if not job:
        return job
    return {k: v for k, v in job.items() if k not in _JOB_PRIVATE}


def _ocr_fallback(conn, item: dict, raw_dir: str, stat: dict) -> tuple[dict, str]:
    """数字版解析出 0 块 → 当扫描件走 OCR。返回 (新的 stat, 给教务看的说明)。

    只在上传链路里做，不放进 `ingest.parse_one`：那条路是批量重建索引用的，
    `data/raw` 下随便一份扫描件就会让一次重建悄悄烧掉几十块钱。
    **花钱的事必须发生在有人按了按钮之后。**

    没配视觉端点就原样返回，让上层照旧显示"需要先 OCR" —— 那句提示在没有
    OCR 能力时仍然是对的处置，不能因为这里接了一条路就把它改掉。
    """
    from backend.rag import indexer
    from backend.rag import ocr as ocr_mod

    if not settings.vision_ready:
        return stat, ""
    path = os.path.join(raw_dir, item["file"])
    try:
        chunks, ocr_stat = ocr_mod.ocr_chunks(
            path, item["name"], item.get("doc_type", "courseware"), item.get("course", ""))
    except Exception as exc:  # noqa: BLE001 —— OCR 失败退回原来的失败原因，不吞
        logger.exception("ocr fallback failed: %s", item.get("file"))
        return stat, "扫描件文字识别失败，请稍后重试，或先转成可复制文字的 PDF 再上传"
    if not chunks:
        return stat, "扫描件没能识别出文字（可能整页都是图片），请先转成可复制文字的 PDF 再上传"
    written = indexer.write_chunks(conn, chunks)
    written["parsed"] = len(chunks)
    # 这句会显示在资料列表上。**必须说出"这是 OCR 转写的"**：OCR 结果与原件
    # 逐字对照过的人才知道它有多少误差，检索引到它时人得有机会怀疑一下。
    note = f"扫描件已 OCR 转写入库（{ocr_stat['ocr_pages']} 页"
    if ocr_stat["truncated"]:
        note += f"，原件共 {ocr_stat['total_pages']} 页，只转写了前 {ocr_stat['ocr_pages']} 页"
    if ocr_stat["failed_pages"]:
        note += f"，{ocr_stat['failed_pages']} 页转写失败已跳过"
    return written, note + "）"


@router.post("/materials")
async def admin_upload_material(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    course_id: str = Form(...),
    doc_type: str = Form("courseware"),
    ident: dict = Depends(require_admin),
):
    """上传一份课件/教材，落盘后台解析建索引。

    走 multipart 而不是 data URL：本项目对头像和拍照批改用的是 data URL
    （见 core_routes 的说明），那是几百 KB 的图片；课件动辄几十 MB，
    base64 会再胖三分之一，且整份要先进内存。
    """
    if not courses_mod.is_valid_course(course_id):
        raise HTTPException(status_code=400, detail=f"未知课程 {course_id}")
    if doc_type not in _DOC_TYPES:
        raise HTTPException(status_code=400, detail=f"doc_type 只能是 {list(_DOC_TYPES)}")

    name = os.path.basename(file.filename or "").strip()
    ext = os.path.splitext(name)[1].lower()
    if ext not in _EXT_PARSER:
        hint = ("；老的 .doc 请另存为 .docx" if ext == ".doc" else
                "；扫描件请先 OCR 成数字版 PDF 再传")
        raise HTTPException(status_code=400,
                            detail=f"只支持 {'、'.join(sorted(_EXT_PARSER))}{hint}")
    # 文件名直接进路径，必须先剥掉目录成分 —— 上面的 basename 已经做了，
    # 这里再挡一次空名与隐藏文件，避免写出 data/raw/<course>/<type>/.（一个目录）
    if not name or name.startswith("."):
        raise HTTPException(status_code=400, detail="文件名不合法")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB")

    raw_dir = settings.raw_dir
    target_dir = os.path.join(raw_dir, course_id, doc_type)
    os.makedirs(target_dir, exist_ok=True)
    stored = os.path.join(target_dir, name)
    if os.path.exists(stored):
        # 不静默覆盖：同名文件很可能是"改了一版又传一次"，而旧版的分块还在索引里，
        # 覆盖之后两版内容会同时被检索到，且都标着同一个出处。
        raise HTTPException(status_code=409,
                            detail="这门课的同类资料里已有同名文件，请先改名或撤下旧版")
    with open(stored, "wb") as f:
        f.write(payload)

    item = {"file": os.path.relpath(stored, raw_dir), "name": os.path.splitext(name)[0],
            "doc_type": doc_type, "parser": _EXT_PARSER[ext], "course": course_id}
    job_id = db.create_ingest_job(name, stored, course_id, doc_type, _EXT_PARSER[ext],
                                  len(payload), ident["username"])
    background.add_task(_run_ingest, job_id, item, raw_dir, settings.rag_fts_path)
    return {"ok": True, "job": _public_job(db.get_ingest_job(job_id))}


@router.get("/materials")
def admin_list_materials(course_id: str = "", limit: int = 50, withdrawn: bool = False,
                         ident: dict = Depends(require_admin)):
    """入库任务列表。失败的也列出来，并带上原样的失败原因。

    默认不含已撤下的；`withdrawn=true` 单独查那一批（文件还在服务器上）。
    """
    jobs = db.list_ingest_jobs(course_id=course_id, limit=limit, withdrawn=withdrawn)
    by_status: dict[str, int] = {}
    for j in jobs:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1
    return {"items": [_public_job(j) for j in jobs], "by_status": by_status}


@router.get("/materials/{job_id}")
def admin_get_material(job_id: int, ident: dict = Depends(require_admin)):
    job = db.get_ingest_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 {job_id} 不存在")
    return _public_job(job)


@router.delete("/materials/{job_id}")
def admin_withdraw_material(job_id: int, ident: dict = Depends(require_admin)):
    """撤下一份资料：断开检索，从列表里隐去，**但文件留在服务器上**。

    三层各做各的：
      索引 —— 删掉它的分块，学生立刻检索不到；
      磁盘 —— 文件从 data/raw 移到 data/withdrawn，不删；
      列表 —— 任务行标成 withdrawn，默认列表里不再出现。

    为什么不删文件：教务撤下多半是"这版不对/这门课不开了"，不是"这份东西
    要销毁"。删了就不可逆 —— 而恰恰是这类操作最容易点错。留着的成本只是磁盘，
    换来的是随时能放回去。

    为什么必须**移出 data/raw**：`scripts/build_fts_from_raw` 整轮重建是扫
    data/raw 重新解析的。留在原地的话，下一次重建会把撤下的资料原样索引回去，
    而这中间不会有任何提示 —— 撤下就成了只在页面上生效的假动作。
    移走还顺带把文件名腾了出来，改一版重传不会撞上 409。
    """
    job = db.get_ingest_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 {job_id} 不存在")
    if job["status"] == "withdrawn":
        raise HTTPException(status_code=409, detail="这份资料已经撤下了")
    if job["status"] in ("pending", "running"):
        # 后台任务正在写这份文件的索引，此刻动它会删到一半又被写回来
        raise HTTPException(status_code=409, detail="这份资料还在入库中，等它跑完再撤下")

    from backend.rag import indexer, retriever

    source = os.path.splitext(job["filename"] or "")[0]
    conn = indexer.connect(settings.rag_fts_path)
    try:
        stat = indexer.delete_source(conn, job["course_id"], job["doc_type"], source)
        indexer.stamp_meta(conn, "admin_withdraw", settings.raw_dir)
    finally:
        conn.close()
    retriever.reset_engines()

    origin = _inside_raw(job["stored_path"])
    parked = ""
    if origin:
        parked = _park(origin, job["course_id"], job["doc_type"], job["filename"])
    db.withdraw_ingest_job(job_id, parked, origin)
    return {"ok": True, "deleted_chunks": stat["deleted"], "file_kept": bool(parked),
            "note": ("索引里没有这份资料的分块（多半当初就解析失败了）"
                     if not stat["deleted"] else "")}


@router.post("/materials/{job_id}/restore")
def admin_restore_material(job_id: int, background: BackgroundTasks,
                           ident: dict = Depends(require_admin)):
    """把撤下的资料放回去：文件搬回 data/raw，重新解析入库。

    走和上传同一条后台任务，而不是"把之前的分块恢复回来"—— 分块早就删了，
    而且语料期间可能变了（同样的内容也许已经由别的文件提供）。重跑一遍解析，
    得到的计数才是此刻真实的样子。
    """
    job = db.get_ingest_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 {job_id} 不存在")
    if job["status"] != "withdrawn":
        raise HTTPException(status_code=409, detail="这份资料没有被撤下，不需要放回")
    if not courses_mod.is_valid_course(job["course_id"]):
        raise HTTPException(status_code=400,
                            detail=f"课程 {job['course_id']} 已不存在，无法放回")

    parked = job["stored_path"] or ""
    if not os.path.isfile(parked):
        raise HTTPException(status_code=410,
                            detail="文件已不在服务器上（被手工清理过），只能重新上传")

    target = job["origin_path"] or os.path.join(
        settings.raw_dir, job["course_id"], job["doc_type"], job["filename"])
    if os.path.exists(target):
        # 撤下会腾出文件名，这段时间里可能已经传了新版同名文件。
        # 直接覆盖会让新版无声消失，所以在这里停下来让人来判断。
        raise HTTPException(status_code=409,
                            detail=f"{job['course_id']}/{job['doc_type']}/ 下已有同名文件，"
                                   "请先把那一份撤下或改名")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.replace(parked, target)

    item = {"file": os.path.relpath(target, settings.raw_dir),
            "name": os.path.splitext(job["filename"])[0],
            "doc_type": job["doc_type"], "parser": job["parser"],
            "course": job["course_id"]}
    db.restore_ingest_job(job_id, target)
    background.add_task(_run_ingest, job_id, item, settings.raw_dir, settings.rag_fts_path)
    return {"ok": True, "job": _public_job(db.get_ingest_job(job_id))}


def _inside_raw(path: str) -> str:
    """把任务行里的 stored_path 收敛到 data/raw 之内，越界的一律当作不存在。

    这一行不是防上传时的路径穿越（写入前已经 basename 过了），是防这条路径
    将来被别的入口复用时，把任务行里的值当成任意路径来搬动。
    """
    stored = os.path.abspath(path or "")
    root = os.path.abspath(settings.raw_dir)
    return stored if stored.startswith(root + os.sep) and os.path.isfile(stored) else ""


def _park(origin: str, course_id: str, doc_type: str, filename: str) -> str:
    """把文件挪到 data/withdrawn/<course>/<doc_type>/ 下，同名则加时间戳后缀。

    同名加后缀而不是覆盖：同一个文件名被撤下两次（改了一版、又撤了）是常事，
    覆盖会让先撤下的那一份无声消失 —— 而"撤下不会丢东西"正是这个机制的全部承诺。
    """
    park_dir = os.path.join(settings.withdrawn_dir, course_id, doc_type)
    os.makedirs(park_dir, exist_ok=True)
    target = os.path.join(park_dir, filename)
    if os.path.exists(target):
        stem, ext = os.path.splitext(filename)
        target = os.path.join(park_dir, f"{stem}.{db._now().replace(':', '')}{ext}")
    os.replace(origin, target)
    return target


class SettingsReq(BaseModel):
    exam_structure: str = Field(default="", max_length=200)


@router.get("/settings")
def admin_get_settings(ident: dict = Depends(require_admin)):
    return {"exam_structure": courses_mod.exam_structure()}


@router.put("/settings")
def admin_put_settings(req: SettingsReq, ident: dict = Depends(require_admin)):
    courses_mod.set_setting("exam_structure", req.exam_structure.strip())
    return {"ok": True, "exam_structure": courses_mod.exam_structure()}

"""鉴权、用户资料与学习记录端点。"""
from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.core import db
from backend.core.courses import is_valid_course
from backend.core.security import current_identity, issue_token
from backend.learning.error_taxonomy import name_of

router = APIRouter()


def _enrich_error(r: dict) -> dict:
    """一条错题记录 → 学生看到的形状。

    **助教的复核结论要带上。** `ta_reviewed` / `ta_verdict` / `ta_note` 一直写在库里
    （`db.review_error_record`），但这里没往外给 —— 助教认真写的批注等于扔进黑洞，
    学生永远看不到，而"人工复核过"恰恰是这条错题最值钱的部分。

    只给结论、备注和"复核过"这个事实，**不给复核人的用户名**：谁改的对学生没有
    意义，而把助教的账号名摊到学生面前是白送的信息。AI 原判 `ai_error_type` 同理
    不给 —— 学生该看到的是订正后的那个错因，两个并排只会让人不知道信哪个。
    """
    stem, diag = db.split_error_text(r)
    reviewed = bool(r.get("ta_reviewed"))
    # 学生自己点过"请助教看看"：错题本上要看得到"已提交、在等"，不然他会反复点
    requested = (r.get("review_reason") == "student_request")
    return {"id": r.get("id"), "error_type": r.get("error_type"),
            "error_name": name_of(r.get("error_type", "")),
            "knowledge_id": r.get("knowledge_id"),
            "stem": stem, "diagnosis": diag,
            "resolved": r.get("resolved"), "created_at": r.get("created_at"),
            "ta_reviewed": reviewed,
            "ta_verdict": (r.get("ta_verdict") or "") if reviewed else "",
            "ta_note": (r.get("ta_note") or "") if reviewed else "",
            "ta_reviewed_at": (r.get("ta_reviewed_at") or "") if reviewed else "",
            "review_requested": requested,
            "review_requested_at": (r.get("review_requested_at") or "") if requested else ""}


class RegisterReq(BaseModel):
    """公网注册表单。

    注意这里**没有 role 字段**：原先有，且默认值只是默认值 —— 任何人 POST
    role="ta" 就能给自己开一个助教账号，直接读到全班错题与班级统计。
    公网注册一律落成 student，助教账号由 scripts 批量建。
    """
    username: str
    password: str
    name: str = ""
    student_id: str = ""
    college: str = ""
    major: str = ""
    class_name: str = ""
    email: str = ""
    phone: str = ""


class LoginReq(BaseModel):
    username: str
    password: str


class ProfileReq(BaseModel):
    name: str = ""
    college: str = ""
    major: str = ""
    class_name: str = ""
    email: str = ""
    phone: str = ""
    avatar: str = ""


@router.post("/auth/register")
def register(req: RegisterReq):
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    ok, msg = db.register_user(
        req.username, req.password, req.name, role="student",
        student_id=req.student_id, college=req.college, major=req.major,
        class_name=req.class_name, email=req.email, phone=req.phone,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    # 回读而不是就地拼装：注册后自动登录拿到的用户对象，与之后重新登录拿到的
    # 必须逐字段一致，否则前端 localStorage 里的身份会随登录方式而异。
    user = db.verify_user(req.username, req.password)
    return {"ok": True, "user": user, "token": issue_token(user["username"], user["role"])}


@router.post("/auth/login")
def login(req: LoginReq):
    user = db.verify_user(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    # 角色取自库里回读的 user，不取客户端任何输入 —— 令牌里的 role 就是授权依据。
    return {"ok": True, "user": user, "token": issue_token(user["username"], user["role"])}


@router.get("/auth/profile")
def profile(ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    user = db.get_user_profile(ident["username"])
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"user": user}


@router.put("/auth/profile")
def update_profile(req: ProfileReq, ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    fields = req.model_dump()
    limits = {"name": 40, "college": 80, "major": 80, "class_name": 80,
              "email": 120, "phone": 30}
    labels = {"name": "姓名", "college": "学院", "major": "专业", "class_name": "班级",
              "email": "邮箱", "phone": "手机"}
    for key, limit in limits.items():
        if len(fields[key].strip()) > limit:
            raise HTTPException(status_code=400, detail=f"{labels[key]}太长了，最多 {limit} 个字")
    if fields["email"].strip() and "@" not in fields["email"]:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    avatar = fields["avatar"].strip()
    allowed = ("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")
    if avatar and (not avatar.startswith(allowed) or len(avatar) > 400_000):
        raise HTTPException(status_code=400, detail="头像格式不支持或文件过大")
    if avatar:
        try:
            decoded = base64.b64decode(avatar.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error, IndexError) as exc:
            raise HTTPException(status_code=400, detail="头像数据无效") from exc
        if len(decoded) > 300_000:
            raise HTTPException(status_code=400, detail="头像文件过大")
    user = db.update_user_profile(ident["username"], **fields)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "user": user}


@router.get("/classes")
def list_classes():
    """已有的班级名。**公开只读**，注册页要用它把班级做成受控输入。

    班级至今是学生自己填的自由文本，"计科2101" / "计科 2101" / "计算机2101"
    会变成三个班，而热力图与作业下发按它精确匹配分组 —— 一个错字就让这个人
    从此不在他该在的那一栏里。让注册时能从已有的里挑，是最便宜的一道防线。

    只回班级名，不回人数、不回名单：这是**登录前**就能打的接口，
    多回一个字段就是白送一份学校的花名册结构。
    """
    return {"items": db.list_classes()}


@router.get("/records/errors")
def records_errors(course: str = "", ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    items = [_enrich_error(r) for r in db.list_error_records(ident["username"], course=course)]
    out = {"items": items}
    if course and is_valid_course(course):
        # 列表最多 50 条，统计数不能拿 len(items) 算 —— 错题一过 50 条，
        # 记录页的"错题总数"就和首页的"待解决错题"对不上了。计数走首页同一个查询。
        out["counts"] = db.error_counts(ident["username"], course)
    return out


@router.get("/records/mastery")
def records_mastery(course: str = "", ident: dict = Depends(current_identity)):
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    if not course or not is_valid_course(course):
        return {"items": db.list_mastery(ident["username"], course=course)}
    # 与学习首页同一口径（profile.summarize_mastery）：练过的点、带遗忘衰减的有效值
    from backend.learning import profile as profile_mod
    points = profile_mod.build_profile(ident["username"], course)
    return {"items": profile_mod.practiced_points(points),
            "summary": profile_mod.summarize_mastery(points)}

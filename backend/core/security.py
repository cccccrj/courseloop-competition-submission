"""身份与安全辅助：HMAC 签名令牌。

## 为什么不再用 X-User / X-Role header

原先 `current_identity` 直接把明文 header 当身份用：

    x_user: str | None = Header(default=None)
    x_role: str | None = Header(default=None)

任何人发一个 `X-Role: ta` 就能读到全班错题与班级统计 —— 连账号都不用有。
注册端点特意去掉了 role 字段防提权（见 backend/api/auth.py:RegisterReq），
但 header 这一层没防，等于前功尽弃。

这既是安全问题，也是**数据问题**：多人同时体验时，伪造或串号会直接把
学习记录写到别人名下，而学习记录正是这个项目全部推荐逻辑的输入。

## 现在的机制

登录/注册时签发令牌，之后经 `Authorization: Bearer <token>` 携带：

    <base64url(payload_json)>.<base64url(hmac_sha256(secret, payload_b64))>

payload = {"u": 用户名, "r": 角色, "iat": 签发时间, "exp": 过期时间}

**角色来自令牌而非客户端**，所以伪造不了。代价是角色变更要重新登录才生效 ——
这个方向的取舍是对的：宁可晚一点授权，不可以被人自称助教。

不引第三方 JWT 库：HMAC-SHA256 + base64 已经够用，少一个依赖少一份供应链风险。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Header, HTTPException

from backend.core.config import settings

ALGO = "HS256"
DEFAULT_TTL_SECONDS = 30 * 24 * 3600      # 30 天，学习类应用不必频繁重登

# 角色词表的**唯一**出处。db.register_user 与 current_identity 都从这里取。
#
# 曾经是两份各自写死的字面量，加 admin 时只改了一处，于是
# `register_user(role="admin")` 静默落成 student —— 不报错，只是建出来的
# 管理员打 /admin/* 一律 403，而看起来像是权限系统坏了。
# 未知角色一律降级为 student（而不是抛错）是有意的：这个函数也服务公网注册路径，
# 那里宁可少给权限也不要因为一个脏值把注册流程打断。
ROLES = ("student", "ta", "teacher", "admin")
DEFAULT_ROLE = "student"


def normalize_role(role: str | None) -> str:
    return role if role in ROLES else DEFAULT_ROLE


def _b64e(raw: bytes) -> str:
    """base64url 无填充 —— '=' 在 URL / header 里容易被中间件吃掉。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _secret() -> bytes:
    """签名密钥。优先读 .env 的 CL_SECRET_KEY；没配就生成一个并落盘复用。

    落盘而不是每次进程内随机：随机密钥会让服务一重启全体用户被登出，
    开发时很烦；而固定的硬编码默认值等于没有密钥。折中是首次运行生成，
    存到 data/processed/.secret_key（data/ 整个不进版本控制）。
    """
    configured = (settings.secret_key or "").strip()
    if configured:
        return configured.encode("utf-8")

    path = os.path.join(os.path.dirname(settings.db_path), ".secret_key")
    try:
        if os.path.exists(path):
            saved = open(path, encoding="utf-8").read().strip()
            if saved:
                return saved.encode("utf-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        generated = os.urandom(32).hex()
        with open(path, "w", encoding="utf-8") as f:
            f.write(generated)
        os.chmod(path, 0o600)
        return generated.encode("utf-8")
    except OSError:
        # 磁盘不可写时退化为进程内密钥：服务能起来，重启会失效，但绝不是固定值。
        return _PROCESS_FALLBACK_SECRET


_PROCESS_FALLBACK_SECRET = os.urandom(32)


def _sign(payload_b64: str) -> str:
    return _b64e(hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest())


def issue_token(username: str, role: str = "student", ttl: int = DEFAULT_TTL_SECONDS) -> str:
    """签发令牌。role 由调用方（登录端点）从数据库读出，不接受客户端输入。"""
    now = int(time.time())
    payload = {"u": username, "r": role, "iat": now, "exp": now + max(60, int(ttl))}
    payload_b64 = _b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_token(token: str) -> dict | None:
    """验签并返回 payload；任何一步不对都返回 None（不区分原因，避免给攻击者线索）。"""
    if not token or token.count(".") != 1:
        return None
    payload_b64, sig = token.split(".", 1)
    # compare_digest 而不是 == ：常数时间比较，不泄露前缀匹配长度
    if not hmac.compare_digest(sig, _sign(payload_b64)):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("u"):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def current_identity(authorization: str | None = Header(default=None)) -> dict:
    """从 Authorization: Bearer <token> 解析身份。

    **没带令牌**返回空身份（username=""）而不抛 401 —— 学习大厅问答、TutorGraph
    起会话都允许匿名试用，带个人数据的端点自己检查 username 为空再 401。

    **带了令牌但无效**（过期 / 被改 / 换了密钥）则直接 401。这两种情况必须分开：
    都当匿名的话，令牌过期的用户看到的是一片 cold_start 空页面 ——
    像是"系统没数据"，而不是"你该重新登录了"，而前端正是靠 401 清会话回登录页的。
    """
    if not authorization:
        return {"username": "", "role": "student"}

    scheme, _, value = authorization.partition(" ")
    token = value.strip() if scheme.lower() == "bearer" else authorization.strip()
    payload = verify_token(token) if token else None
    if not payload:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")

    username = str(payload["u"])
    # 停用状态**每次请求都查库**，而不是只在登录时挡。
    #
    # 角色变更可以等重新登录才生效（见上文的取舍），但停用不行：令牌有效期 30 天，
    # 而停用一个账号通常是安全动作（人离校了、号被人拿到了）。"最多再放行 30 天"
    # 对这类动作是不可接受的。代价是每个已登录请求多一次主键查询 ——
    # SQLite 本地读，与这一次请求后面要做的事相比可以忽略。
    from backend.core import db
    if db.user_status(username) == "disabled":
        raise HTTPException(status_code=401, detail="账号已被停用，请联系管理员")
    return {"username": username, "role": normalize_role(payload.get("r"))}

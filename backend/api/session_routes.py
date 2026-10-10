"""会话读回：把已经落库的对话重新交回前端。

`learning_sessions` + `dialog_turns` 从建表起就一直在写 —— 大厅问答、TutorGraph
解题、摸底、模考，每一轮都进了库，但除了摸底续答之外没有任何读回的入口。
于是出现一个很难向用户解释的现象：**对话明明存着，刷新一下就再也看不到了**。
前端的 keep-alive 只能保住"这次页面加载"，刷新 / 重登 / 换设备一律清零。

这一组端点补的就是那个缺口：
    GET    /sessions            我的会话列表（按科目、意图过滤）
    GET    /sessions/{id}       某次会话的完整轮次
    DELETE /sessions/{id}       删掉一次会话

三条都严格按 username 归属校验，别人的会话一律当作不存在（404 而不是 403：
403 等于确认"这条会话存在，只是不归你"，对一个连续 id 来说那是一份可枚举的名单）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from backend.core import db
from backend.core.security import current_identity

router = APIRouter()

# 列表里那句摘要的长度。够认出"哪一次是问栈和队列的"，又不至于把整段问题铺进侧栏。
TITLE_MAX = 40


def _require_login(ident: dict) -> str:
    if not ident["username"]:
        raise HTTPException(status_code=401, detail="未登录")
    return ident["username"]


def _owned_session(session_id: int, username: str) -> dict:
    sess = db.get_session(session_id)
    if sess is None or sess.get("username") != username:
        raise HTTPException(status_code=404, detail="会话不存在")
    return sess


def _title(text: str) -> str:
    one = " ".join((text or "").split())
    return one[:TITLE_MAX] + ("…" if len(one) > TITLE_MAX else "")


def _turn_out(t: dict) -> dict:
    """把一条轮次整理成前端消息。

    structured_data 是历史包袱最重的一列：大厅问答往里塞 {"citations": [...]}，
    别的链路塞的是别的结构，早期还有塞空串的。所以这里只挑认得的键往外露，
    认不出来的原样放进 data，交给调用方自己看着办 —— 解析失败绝不能让整次
    会话读不回来，那正是我们要修的毛病。
    """
    out = {"role": t.get("role", ""), "content": t.get("content", ""),
           "created_at": t.get("created_at", "")}
    raw = t.get("structured_data") or ""
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return out
        if isinstance(data, dict) and data.get("citations"):
            out["citations"] = data["citations"]
        if isinstance(data, dict) and data.get("verified") is not None:
            out["verified"] = data["verified"]
        out["data"] = data
    return out


@router.get("/sessions")
def list_sessions(course: str = "", intent: str = "", limit: int = 20,
                  ident: dict = Depends(current_identity)):
    """我的会话列表，最近的在前。

    前端进大厅时用 `?course=ds-algo&intent=chat&limit=1` 取最近一条直接续上；
    点「历史」时把 limit 放大铺成列表。
    """
    username = _require_login(ident)
    items = [{**s, "title": _title(s.get("title") or "")}
             for s in db.list_sessions(username, course_id=course, intent=intent, limit=limit)]
    return {"items": items}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, ident: dict = Depends(current_identity)):
    """一次会话的完整轮次（按时间顺序）。"""
    username = _require_login(ident)
    sess = _owned_session(session_id, username)
    turns = [_turn_out(t) for t in db.list_turns(session_id)]
    return {"id": sess["id"], "course_id": sess.get("course_id", ""),
            "intent": sess.get("intent", ""), "state": sess.get("state", ""),
            "started_at": sess.get("started_at", ""), "completed_at": sess.get("completed_at"),
            "turns": turns}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, ident: dict = Depends(current_identity)):
    """删掉一次会话（连同轮次）。不是本人的一律 404。"""
    username = _require_login(ident)
    _owned_session(session_id, username)
    return {"deleted": db.delete_session(session_id, username)}

"""演示机启动前自检：数据、轻量检索、模考试卷与可选 API 连通性。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

# README 里写的是 `python scripts/preflight_demo.py`：直接跑脚本时仓库根不在 sys.path 上，
# 此前这样跑一律 ModuleNotFoundError（只有 `python -m scripts.preflight_demo` 能跑）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.build_info import build_info, repo_head  # noqa: E402
from backend.core.config import settings  # noqa: E402
from backend.learning import mock_exam  # noqa: E402
from backend.rag.retriever import get_engine  # noqa: E402


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def main() -> None:
    parser = argparse.ArgumentParser(description="检查无卡演示环境是否完整")
    parser.add_argument("--check-api", action="store_true", help="额外发送一次极小文本 API 请求")
    parser.add_argument("--runtime-url", default="",
                        help="正在运行的后端 /admin/runtime 地址（如 http://127.0.0.1:8000/admin/runtime），"
                             "给了就把它报的版本也拿来比对。本机用 .env 里的密钥签一枚临时管理员令牌去读")
    parser.add_argument("--dist", default=str(Path(__file__).resolve().parents[1] / "frontend" / "dist"),
                        help="要上线的前端产物目录")
    args = parser.parse_args()

    failures: list[str] = []
    db_path = Path(settings.db_path)
    counts: dict[str, int] = {}
    if not db_path.is_file():
        failures.append(f"业务库不存在：{db_path}")
    else:
        with sqlite3.connect(db_path) as conn:
            for table in ("users", "problems", "practice_attempts", "error_records", "assignments"):
                counts[table] = _count(conn, table)
            showcase = int(conn.execute(
                "SELECT count(*) FROM users WHERE username LIKE 'showcase_s%'"
            ).fetchone()[0])
            counts["showcase_students"] = showcase
            if showcase < 12:
                failures.append(f"演示学生不足：{showcase}/12")

    rag = get_engine()
    hits = rag.retrieve("二叉树中序遍历", course="ds-algo", top_n=3) if rag.has_corpus() else []
    if not hits:
        failures.append("轻量 RAG 未检索到数据结构教材")

    paper = mock_exam.build_paper("showcase_s01")
    if paper["full_score"] != 150 or paper["shortfall"]:
        failures.append(f"模考试卷不完整：{paper['full_score']}/150，缺口 {paper['shortfall']}")

    api_result = "skipped"
    if args.check_api:
        if not settings.text_llm_ready:
            failures.append("文本 API 未配置")
            api_result = "not_configured"
        else:
            try:
                from backend.core.llm import ask_workhorse
                answer = ask_workhorse(
                    [{"role": "user", "content": "只回复 OK"}], max_tokens=8
                ).strip()
                api_result = "ok" if answer else "empty"
                if not answer:
                    failures.append("文本 API 返回空内容")
            except Exception as exc:  # noqa: BLE001
                api_result = type(exc).__name__
                failures.append(f"文本 API 调用失败：{type(exc).__name__}")

    # ---- 版本：仓库 HEAD / 后端 / 前端产物 / 正在运行的服务，必须是同一个提交 ----
    # 起因见 docs/34 §2.1：此前线上跑哪一版无从证明，评审拿旧构建的截图下了结论。
    head = repo_head()
    backend = build_info()
    versions = {"repo_head": head, "backend": backend["commit"], "backend_dirty": backend["dirty"]}
    build_json = Path(args.dist) / "build.json"
    if build_json.is_file():
        web = json.loads(build_json.read_text(encoding="utf-8"))
        versions.update(frontend=web.get("commit", "unknown"), frontend_dirty=web.get("dirty", False),
                        frontend_built_at=web.get("built_at", ""))
    else:
        versions["frontend"] = "missing"
        failures.append(f"前端产物没有版本戳：{build_json} 不存在，请重新 npm run build")
    if args.runtime_url:
        try:
            # 版本号不再挂在公开的 /health 上；preflight 跑在服务器本机，和后端读同一份 .env，
            # 直接签一枚短期管理员令牌去读 /admin/runtime
            from backend.core.security import issue_token
            req = urllib.request.Request(args.runtime_url, headers={
                "Authorization": f"Bearer {issue_token('preflight', 'admin', ttl=120)}"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                live = json.loads(resp.read().decode("utf-8")).get("build") or {}
            versions["running"] = live.get("commit", "unknown")
        except Exception as exc:  # noqa: BLE001
            versions["running"] = f"unreachable ({type(exc).__name__})"
            failures.append(f"连不上正在运行的后端：{args.runtime_url}")
    # 基准：有 .git 就以仓库 HEAD 为准；用压缩包部署、没有 .git 时（HEAD=unknown），
    # 以后端报告的版本（来自 CL_BUILD_COMMIT）为准，前端产物与运行中的服务都要和它一致。
    base_key, base = ("repo_head", head) if head != "unknown" else ("backend", versions["backend"])
    if base == "unknown":
        failures.append("拿不到版本号：既没有 .git，也没有配置 CL_BUILD_COMMIT")
    for key in ("backend", "frontend", "running"):
        v = versions.get(key)
        if key == base_key or not v or v == "missing" or str(v).startswith("unreachable"):
            continue
        if v != base:
            failures.append(f"版本不一致：{key}={v}，{base_key}={base}")
    if versions.get("backend_dirty") or versions.get("frontend_dirty"):
        failures.append("工作区或前端产物含未提交改动：commit 号不能代表实际运行的代码")

    # 拍照批改是演示路径的一环，视觉模型没配上时要在演示前知道，而不是在台上
    if not settings.vision_ready:
        failures.append("视觉模型未配置：拍照批改不可用")

    result = {
        "version": versions,
        "ok": not failures,
        "database": str(db_path),
        "counts": counts,
        "rag": {"mode": settings.rag_mode, "ready": bool(hits),
                "hits": len(hits), "first_source": hits[0].source if hits else ""},
        "exam": {"items": len(paper["items"]), "full_score": paper["full_score"],
                 "standard_score": paper["standard_score"], "shortfall": paper["shortfall"]},
        "llm": {"configured": settings.text_llm_ready, "api_check": api_result},
        "vision_configured": settings.vision_ready,
        "failures": failures,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""这一份后端是从哪个提交跑起来的。

起因见 docs/34 §2.1：演示环境跑的是哪一版，此前**无法证明**——`/health` 不带版本，
前端产物也不带。于是有人拿旧部署的截图判断"某个问题还在"，另一个人拿源码说"早修了"，
两边都没错，也都没法说服对方。

取值顺序：环境变量 `CL_BUILD_COMMIT`（部署时由发布脚本注入，服务器上可能没有 .git）
→ 仓库里的 `git rev-parse`。两者都拿不到时写 "unknown"，而不是编一个。
`dirty` 表示工作区有未提交的改动：那种状态下 commit 号并不能代表实际运行的代码。
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                             text=True, timeout=5, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@lru_cache(maxsize=1)
def build_info() -> dict:
    # 进程环境变量优先（systemd Environment= / 发布脚本注入），其次 .env 里的 CL_BUILD_COMMIT ——
    # 后者由 pydantic-settings 读进 settings，不会出现在 os.environ 里，只查 environ 会漏掉。
    from backend.core.config import settings
    env_commit = (os.environ.get("CL_BUILD_COMMIT", "") or settings.build_commit).strip()
    if env_commit:
        return {"commit": env_commit[:12], "dirty": False, "source": "env",
                "started_at": STARTED_AT}
    commit = _git("rev-parse", "--short=7", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no")) if commit else False
    return {"commit": commit or "unknown", "dirty": dirty,
            "source": "git" if commit else "none", "started_at": STARTED_AT}


def repo_head() -> str:
    """仓库当前 HEAD（不看环境变量）。preflight 拿它和前后端各自报的版本比。"""
    return _git("rev-parse", "--short=7", "HEAD") or "unknown"

"""单进程回环演示：同一端口提供前端 SPA 与 /api 后端，不替代生产 Nginx。"""
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from backend.api.main import app as api_app
from backend.core import db

# 独立 ASGI 应用，避免导入演示服务器时给原始 API 全局实例追加中间件。
@asynccontextmanager
async def lifespan(app):
    db.init_db()
    yield


app = FastAPI(title="CourseLoop submission demo", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.router.routes.extend(api_app.router.routes)
app.exception_handlers.update(api_app.exception_handlers)

DIST = Path(__file__).resolve().parents[1] / "frontend/dist"


@app.middleware("http")
async def api_prefix(request, call_next):
    if request.scope["path"].startswith("/api/"):
        request.scope["path"] = request.scope["path"][4:]
        request.scope["raw_path"] = request.scope["path"].encode()
        request.scope["submission_api"] = True
    return await call_next(request)


@app.get("/{asset_path:path}", include_in_schema=False)
def spa(asset_path: str, request: Request):
    # 未找到的 /api 不能误返回 HTML；静态文件严防目录穿越。
    if request.scope.get("submission_api"):
        raise HTTPException(404)
    candidate = (DIST / asset_path).resolve()
    if not candidate.is_relative_to(DIST.resolve()):
        raise HTTPException(404)
    if candidate.is_file():
        return FileResponse(candidate)
    if asset_path not in {"", "login", "register", "center", "hall", "solve", "practice", "records",
                           "review", "map", "exam", "placement", "photo-grade", "report", "community",
                           "profile", "teaching", "materials", "users"}:
        raise HTTPException(404)
    if not (DIST / "index.html").exists():
        raise HTTPException(503, "请先执行 frontend/npm run build")
    return FileResponse(DIST / "index.html")

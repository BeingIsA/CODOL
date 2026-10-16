"""FastAPI 应用装配：中间件、错误映射、鉴权依赖与静态托管。

装配顺序（Starlette 按注册顺序匹配）：先注册全部 /api 路由，最后挂载
ui/ 静态目录到 "/"（html 模式提供 index.html），API 永远优先命中。
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from ..domain.errors import ControlError
from ..infrastructure.logging_setup import MergingLoggerAdapter

logger = logging.getLogger("codol_control")


class NoCacheStaticFiles(StaticFiles):
    """管理页静态文件禁用缓存。

    UI 迭代频繁而文件极小，no-cache（可缓存但每次校验）可避免浏览器
    执行旧版 app.js 与新后端混跑；校验命中时仍返回 304，无带宽损失。
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(container) -> FastAPI:
    """按容器构建应用；容器字段见 codol_control.container.Container。"""
    app = FastAPI(
        title="CODOL 控制服务",
        version=container.version,
        # 内网管理服务不对外暴露文档端点，接口契约以代码与 README 为准。
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.container = container
    # 每条访问日志带上 boot_id（与 setup_logging 的 JSONL 字段对齐）。
    log = MergingLoggerAdapter(logger, {"boot_id": container.boot_id})

    @app.middleware("http")
    async def observability(request: Request, call_next):
        """每请求分配 request_id 并记录访问日志（方法/路径/状态/耗时）。"""
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        log.info(
            "http.request",
            extra={
                "request_id": request_id,
                "detail": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                },
            },
        )
        return response

    @app.exception_handler(ControlError)
    async def domain_error(_: Request, exc: ControlError):
        """领域错误 → 统一 JSON：{error:{code,message}, request_id}。"""
        request_id = getattr(_.state, "request_id", "")
        log.warning("domain.error %s %s", exc.code, exc.message,
                    extra={"detail": exc.detail})
        return JSONResponse(
            status_code=exc.http_status,
            headers={"X-Request-Id": request_id} if request_id else {},
            content={"error": {"code": exc.code, "message": exc.message},
                     "request_id": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        """请求体校验同样使用稳定错误契约；不回显口令、令牌或原始输入值。"""
        fields = sorted({str(error["loc"][-1]) for error in exc.errors()})
        return JSONResponse(status_code=422, content={
            "error": {"code": "INVALID_ARGUMENT", "message": "请求字段不合法：" + "、".join(fields)},
            "request_id": getattr(request.state, "request_id", "")})

    @app.exception_handler(Exception)
    async def unknown_error(request: Request, exc: Exception):
        """未分类异常：对外只报 INTERNAL 与 request_id，细节留在服务日志。"""
        request_id = getattr(request.state, "request_id", "")
        log.exception("internal.error", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL", "message": "服务内部错误，请查看诊断页日志。"},
                     "request_id": request_id},
        )

    from . import routes  # 延迟导入避免循环依赖
    app.include_router(routes.router, prefix="/api")
    from .player_routes import router as player_router
    app.include_router(player_router, prefix="/api")

    # 静态管理页最后挂载（禁缓存，见 NoCacheStaticFiles）；
    # ui 目录随源码分发，不存在即安装损坏。
    app.mount("/", NoCacheStaticFiles(directory=str(container.config.ui_dir), html=True), name="ui")
    return app

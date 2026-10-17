"""FastAPI 入口：当前提供环境存活、就绪检查及本机 API 文档。"""

from contextlib import asynccontextmanager
from dataclasses import asdict
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.ad_daily import router as ad_daily_router
from app.analytics import router as analytics_router
from app.catalog import router as catalog_router
from app.competitors import router as competitor_router
from app.core.config import Settings, get_settings
from app.core.db import DatabaseProbe
from app.core.errors import AppError
from app.daily_data import router as daily_router
from app.identity import init_rate_limit, router
from app.imports import router as import_router
from app.product_daily import router as product_daily_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建应用；测试可注入独立配置，避免依赖开发者的真实凭据。"""
    settings = settings or get_settings()
    database = DatabaseProbe(settings)

    # 启动时不自动迁移数据库，退出时回收连接池。
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        database.close()

    app = FastAPI(
        title="电商运营智能助手 API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.app_env in {"dev", "test"} else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.app_env in {"dev", "test"} else None,
    )
    app.state.settings = settings
    app.state.database = database
    init_rate_limit(app)
    app.include_router(router)
    app.include_router(catalog_router)
    app.include_router(competitor_router)
    app.include_router(daily_router)
    app.include_router(product_daily_router)
    app.include_router(ad_daily_router)
    app.include_router(import_router)
    app.include_router(analytics_router)

    @app.exception_handler(AppError)
    async def business_error(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "field_errors": exc.fields,
                    "retryable": False,
                },
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # FastAPI 默认会回显 input；密码接口必须去除原值和上下文。
        fields = [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
        ]
        return await business_error(
            request, AppError(422, "DATA_INVALID", "输入不符合要求", fields)
        )

    @app.exception_handler(IntegrityError)
    async def conflict_error(request: Request, _: IntegrityError):
        return await business_error(
            request, AppError(409, "DATA_DUPLICATE_KEY", "编码或账号已存在，或关联数据无效")
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, _: SQLAlchemyError):
        return await business_error(
            request, AppError(503, "SYS_DB_UNAVAILABLE", "数据库暂不可用，请稍后重试")
        )

    # 为请求生成关联编号，响应禁止缓存，便于排查且避免复用旧状态。
    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/health/live", tags=["environment"])
    def live(request: Request):
        """存活检查只证明 API 进程可响应，不代表数据库可用。"""
        return {
            "data": {"status": "ok", "service": "ecommerce-operation-agent-api"},
            "request_id": request.state.request_id,
        }

    @app.get("/api/v1/health/ready", tags=["environment"])
    def ready(request: Request):
        """就绪检查验证数据库和迁移；未就绪明确返回 HTTP 503。"""
        result = database.check()
        return JSONResponse(
            status_code=200 if result.ready else 503,
            content={"data": asdict(result), "request_id": request.state.request_id},
        )

    return app


app = create_app()

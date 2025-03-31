import sentry_sdk
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware
from app.core.scheduler import init_scheduler
import logging
import asyncio
from starlette.routing import Mount
import os

from app.api.main import api_router
from app.core.config import settings
from app.api.middleware import XRouteRateLimitMiddleware
from app.core.mcp_client import mcp_client

logger = logging.getLogger(__name__)

def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
    swagger_ui_init_oauth={},
    swagger_ui_parameters={"persistAuthorization": True}
)

# Set all CORS enabled origins
if settings.all_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.all_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 添加中间件
app.add_middleware(XRouteRateLimitMiddleware)

@app.on_event("startup")
async def startup_event():
    try:
        init_scheduler()
        logger.info("Scheduler initialized successfully")
        
        # 初始化MCP客户端（不指定服务器路径，让它使用默认值）
        await mcp_client.initialize()
    except Exception as e:
        logger.error(f"Failed to initialize services: {str(e)}", exc_info=True)
        # 这里不要抛出异常，让应用继续启动，即使MCP初始化失败
        # 这样API其他功能仍然可以使用

@app.on_event("shutdown")
async def shutdown_event():
    try:
        # 清理MCP客户端资源
        if hasattr(mcp_client, "cleanup"):
            await mcp_client.cleanup()
    except Exception as e:
        logger.error(f"Error during shutdown: {str(e)}", exc_info=True)

# 挂载 API 路由
app.include_router(api_router, prefix=settings.API_V1_STR)

# 挂载 MCP SSE 服务
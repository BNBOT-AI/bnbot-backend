from fastapi import APIRouter

from app.api.routes import items, login, users, utils, x, ai, payments, mcp

api_router = APIRouter()
api_router.include_router(login.router, tags=["login"])
api_router.include_router(utils.router, prefix="/utils", tags=["utils"])
api_router.include_router(x.router, prefix="/x", tags=["x"])
api_router.include_router(ai.router, prefix="/ai", tags=["ai"])
api_router.include_router(payments.router, prefix="/payments", tags=["payments"])
api_router.include_router(mcp.router, prefix="/mcp", tags=["mcp"])
# api_router.include_router(users.router, prefix="/users", tags=["users"])
# api_router.include_router(items.router, prefix="/items", tags=["items"])

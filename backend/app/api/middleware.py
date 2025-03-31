from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response, JSONResponse
import json

class XRouteRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 只处理 /api/v1/x/ 路径下的请求
        if not request.url.path.startswith("/api/v1/x/"):
            return await call_next(request)
            
        response = await call_next(request)
        
        if isinstance(response, Response):
            try:
                body = response.body
                if isinstance(body, bytes):
                    body = body.decode()
                data = json.loads(body)
                
                if isinstance(data, dict) and "message" in data:
                    if "You have exceeded the MONTHLY quota for Requests" in data["message"]:
                        return JSONResponse(
                            status_code=429,
                            content={
                                "detail": "You have exceeded the MONTHLY quota for Requests on your current plan"
                            }
                        )
            except:
                pass
                
        return response 
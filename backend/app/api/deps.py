from collections.abc import Generator
from typing import Annotated, Any, Callable

import jwt
from fastapi import Depends, HTTPException, status, Request, Response
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from sqlmodel import Session, select
import json
from datetime import datetime
import secrets

from app.core import security
from app.core.config import settings
from app.core.db import engine
from app.models import TokenPayload, User, APIKey


reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/login/access-token"
)


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def get_db() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db)]
TokenDep = Annotated[str, Depends(reusable_oauth2)]


def get_current_user(session: SessionDep, token: TokenDep) -> User:
    print(f"Debug - Token: {token}")
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
    except (InvalidTokenError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )
    user = session.get(User, token_data.sub)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user

CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_active_superuser(current_user: CurrentUser) -> User:
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="The user doesn't have enough privileges"
        )
    return current_user


async def verify_api_key(
    request: Request,
    session: Session = Depends(get_db)
) -> None:
    # Get API key from headers
    api_key = request.headers.get('x-api-key')

    # Check if API key is provided
    if not api_key:
        raise HTTPException(
            status_code=403,
            detail="API key is required"
        )

    # Validate API key
    api_key_obj = session.exec(
        select(APIKey).where(
            APIKey.key == api_key,
            APIKey.is_active == True
        )
    ).first()

    if not api_key_obj:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key"
        )

    # Check remaining requests
    if api_key_obj.remaining_requests <= 0:
        raise HTTPException(
            status_code=429,
            detail="API key has exceeded its request limit"
        )

    # Update usage statistics
    api_key_obj.last_used_at = datetime.now()
    api_key_obj.total_requests += 1
    api_key_obj.remaining_requests -= 1
    session.add(api_key_obj)
    session.commit()


VerifyApiKeyDep = Depends(verify_api_key)


def check_rate_limit(call_next: Callable) -> Callable:
    async def rate_limit_middleware(response: Response) -> Response:
        # 获取响应数据
        response_data = response.body
        if isinstance(response_data, str):
            try:
                response_data = json.loads(response_data)
            except json.JSONDecodeError:
                pass

        # 检查速率限制错误
        if isinstance(response_data, dict) and "message" in response_data:
            if "You have exceeded the MONTHLY quota for Requests" in response_data["message"]:
                raise HTTPException(
                    status_code=429,
                    detail="You have exceeded the MONTHLY quota for Requests on your current plan"
                )

        return response

    return rate_limit_middleware


# 创建依赖项
RateLimitDep = Depends(check_rate_limit)

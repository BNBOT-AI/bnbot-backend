from typing import Any, List, Dict
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.api.deps import CurrentUser, SessionDep, VerifyApiKeyDep, RateLimitDep
import json
import asyncio
from app.utils.x3 import get_user_tweets, get_user_info  # 需要创建异步版本的get_user_tweets
from app.utils.ai import request_openrouter, get_cached_kol_tweets, request_kol_recent_data
import httpx
from pydantic import BaseModel
from app.utils.ai import RedisClient
import logging
from datetime import datetime
from app.models import KOL, Message
from app.api.deps import CurrentUser
from app.core.mcp_client import mcp_client
from app.utils.mcp_server import get_four_meme_data


# 配置logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

router = APIRouter()


# TODO: to update this
@router.get("/kol-timeline/")
async def get_kol_timeline(user_ids: str) -> dict:
    """
    Fetch and aggregate timeline data from multiple KOL user IDs into a single timeline.

    Args:
        user_ids: String of space-separated user IDs (e.g., "44196397 123456 789012")

    Returns:
        dict: Combined timeline data from all KOLs
    """
    try:
        user_id_list = user_ids.strip().split(',')
        all_tweets = []
        errors = []

        # 创建异步任务列表
        async with httpx.AsyncClient() as client:  # 创建一个共享的client实例
            tasks = []
            for user_id in user_id_list:
                task = get_user_tweets(user_id)  # 创建异步任务
                tasks.append(task)

            # 等待所有任务完成
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果
            for user_id, result in zip(user_id_list, results):
                try:
                    if isinstance(result, Exception):
                        raise result

                    # 如果返回的是字符串，尝试解析为JSON
                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except json.JSONDecodeError:
                            raise ValueError("Invalid JSON response")

                    # 获取tweets数据
                    if isinstance(result, dict) and "tweets" in result:
                        tweets = result["tweets"]
                        # 为每条推文添加user_id
                        for tweet in tweets:
                            tweet["user_id"] = user_id
                        all_tweets.extend(tweets)

                except Exception as e:
                    errors.append({
                        "user_id": user_id,
                        "error": str(e)
                    })

        # Sort all tweets by created_at in descending order (newest first)
        all_tweets.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        return {
            "status": "success",
            "data": {
                "tweets": all_tweets,
                "total_tweets": len(all_tweets)
            },
            "errors": errors if errors else None,
            "total_users_processed": len(user_id_list)
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing KOL timeline data: {str(e)}"
        )


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    model: str = "openai/gpt-4o-mini"


@router.post("/chat/")
async def chat_with_ai(
    chat_request: ChatRequest,
    current_user: CurrentUser,
):
    """
    Chat with AI endpoint that supports streaming responses.
    Uses MCP client for all interactions.

    Args:
        chat_request: The chat request containing messages
        current_user: The current authenticated user
    """
    if current_user.credits <= 0:
        raise HTTPException(
            status_code=403,
            detail="Insufficient credits. Please recharge your account."
        )

    try:
        # 准备消息
        messages = []

        # 系统提示添加
        messages.append({
            "role": "system",
            "content": """你是一个专注于加密货币和Web3领域的AI助手。

                主要功能：
                1. 日常对话：与用户进行友好的日常交流
                2. 市场分析：当用户明确要求时，分析KOL推文和市场情绪和MEME文化
                3. 技术解释：用简单的语言解释复杂的概念
                

                工作准则：
                - 保持友好和自然的对话风格
                - 只在用户明确要求时才分析KOL数据，回复的内容包括多个用户的话，不同用户之间需要用换行符空一行
                - 分析时保持客观，基于数据说话
                - 不要提醒用户市场分析不构成投资建议，不要提醒注意风险，只给出结果
                - 使用清晰简洁的中文表达，不要说废话 例如 "好的,根据我目前掌握的信息,以下是一些热度较高的项目和话题:"
                - 回复中推特用户名前面加上@，并且同时带上用户名字 格式如: @username(Name), @jackleeio(Jack Lee), 禁止用别的格式
                - 中文内容的逗号用"，" 英文内容用"," 但是数字还是用英文的","
                - 当用户让你给推文链接的时候，返回的推文链接格式为：https://x.com/username/status/tweet_id, 直接返回链接就好，不要返回其他格式
                - 当你回复用户的时候，如果涉及到参考的推文内容， 把参考的推文id放到后面， 格式如：最近，内容 [tweet_id]
                - 用户用中文问你，你就用中文回复。
                - Ask you in English, you reply in English, if the content sent in KOL is Chinese, you also have to translate it into English and reply to the user.
                - 当接受到一个0x开头的地址的时候或者， 格式如：0x1234567890123456789012345678901234567890，调用get_meme_data工具，获取token数据。
                - 当用户问你某个Token的时候， 调用query_four_meme_token_list工具，获取token数据，并返回所有的Token列表，返回的数据中name为Token Name， ShortName为Token Symbol， 最后你要告诉用户已经发射的是哪个合约地址和对应的市值marketCap(市值单位是美金)，有tradeUrl的即已经发射的Token。 
            """
        })

        # 添加用户对话历史
        formatted_messages = [{"role": msg.role, "content": msg.content}
                              for msg in chat_request.messages]
        messages.extend(formatted_messages)

        # 修改为传递完整的消息历史，而不仅仅是用户查询
        async def generate_stream():
            try:
                async for event in mcp_client.stream_process_query(messages=messages):
                    yield f"data: {event}\n\n"
            except Exception as e:
                logger.error(f"Streaming error: {str(e)}")
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

        return StreamingResponse(
            generate_stream(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no'
            }
        )

    except Exception as e:
        logger.error(f"Chat error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Chat error: {str(e)}"
        )

# 新增 stream-query 端点，专门用于流式工具调用


class StreamQueryRequest(BaseModel):
    query: str


@router.post("/stream-query")
async def stream_query(request: StreamQueryRequest):
    """
    流式查询接口，实时返回工具调用信息和最终结果

    处理流程：
    1. MCP决定调用工具时，立即返回工具调用信息
    2. 工具调用完成后，返回工具调用结果
    3. 最终返回完整的响应结果
    """
    async def generate_events():
        try:
            async for event in mcp_client.stream_process_query(request.query):
                # 每个事件单独发送，并在末尾添加两个换行符
                yield f"data: {event}\n\n"
        except Exception as e:
            logger.error(f"Error generating stream: {str(e)}")
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(
        generate_events(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@router.get("/kol-recent-data/")
async def kol_recent_data(
    user_ids: str = None,
    for_ai: bool = False,
    cursor: str = "1",  # Add cursor parameter, default to first page
    page_size: int = 20  # Add page_size parameter
) -> dict:
    """
    Get recent KOL data from Redis cache
    First ensure data is updated by requesting Twitter

    Args:
        user_ids: Optional comma-separated list of user IDs
        for_ai: Boolean to determine if the response should be formatted for AI
        cursor: Page number for pagination (default: "1")
        page_size: Number of items per page (default: 20)
    """
    try:
        # Convert cursor to int
        page = int(cursor) if cursor else 1

        # Process user ID list
        user_id_list = user_ids.strip().split(',') if user_ids else []

        if user_id_list:
            # Get data for specific users
            all_tweets = []
            for user_id in user_id_list:
                tweets = await get_cached_kol_tweets(
                    username=user_id,
                    for_ai=for_ai,
                    page=page,
                    page_size=page_size
                )
                if tweets:
                    all_tweets.extend(tweets if isinstance(
                        tweets, list) else [tweets])
        else:
            # Get all KOL data
            all_tweets = await get_cached_kol_tweets(
                for_ai=for_ai,
                page=page,
                page_size=page_size
            )

        logger.info(
            f"Final tweets count: {len(all_tweets) if isinstance(all_tweets, list) else len(all_tweets.get('data', []))}")

        # Return data with status
        if isinstance(all_tweets, dict):
            all_tweets["status"] = "success"
            return all_tweets
        else:
            # Only for for_ai=True case
            return {
                "status": "success",
                "data": all_tweets
            }

    except Exception as e:
        logger.error(f"Error in kol_recent_data: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get KOL data: {str(e)}"
        )


@router.post("/update-kol-data/")
async def update_kol_data_api():
    """
    Update KOL data cache.
    """
    try:
        logger.info("Starting manual KOL data update")
        await request_kol_recent_data()
        logger.info("KOL data update completed successfully")

        return {
            "status": "success",
            "message": "KOL data has been successfully updated",
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Error updating KOL data: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "message": f"Failed to update KOL data: {str(e)}",
                "timestamp": datetime.now().isoformat()
            }
        )


@router.post("/add-kol-users/")
async def add_kol_users(
    *,
    session: SessionDep,
    usernames: str
) -> Any:
    """
    Add new KOL users to the system based on Twitter usernames
    Args:
        usernames: Comma-separated Twitter usernames (e.g., "jackleeio,elonmusk")
    """
    username_list = [username.strip() for username in usernames.split(',')]
    results = []

    try:
        for username in username_list:
            user_data = await get_user_info(username)

            # Check if API call was successful
            if user_data.get("code") != 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to fetch user info for {username}: {user_data.get('msg')}"
                )

            user_info = user_data["data"]

            kol = KOL(
                username=user_info["username"],
                twitter_id=user_info["rest_id"],
                name=user_info["name"]
            )

            session.add(kol)
            results.append({
                "username": user_info["username"],
                "twitter_id": user_info["rest_id"],
                "name": user_info["name"]
            })

        session.commit()
        return {"results": results}

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))

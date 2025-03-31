import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from app.core import security
from app.core.config import settings

import os

from web3 import Web3
from eth_account.messages import encode_defunct

from app.models import SignedAssetSignature, SignedRewardSignature, SignedSignature

import httpx
import json

from fastapi import HTTPException

from sqlmodel import Session, select
from app.models import KOL, User
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

import redis
import asyncio

from app.utils.x3 import get_user_tweets

# 配置logger
logger = logging.getLogger(__name__)

X_API_1_ENDPOINT = os.getenv('X_API_1_ENDPOINT')
X_API_1_HOST = os.getenv("X_API_1_HOST")
X_API_2_ENDPOINT = os.getenv('X_API_2_ENDPOINT')
X_API_2_HOST = os.getenv("X_API_2_HOST")
X_API_2_API_KEY = os.getenv("X_API_2_API_KEY")
X_API_KEY = os.getenv("X_API_KEY")

# Get signer private key from env variable
signer_private_key = os.getenv("SIGNER_PRIVATE_KEY")

# Add OpenRouter environment variables
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Create engine and sessionmaker
engine = create_engine(str(settings.SQLALCHEMY_DATABASE_URI))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 创建一个单例的 Redis 客户端


class RedisClient:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = redis.Redis(
                # 使用 docker-compose 中的服务名
                host=os.getenv('REDIS_HOST', 'redis'),
                port=int(os.getenv('REDIS_PORT', 6379)),
                db=0,
                decode_responses=True
            )
        return cls._instance


async def request_openrouter(
    messages: list[dict],
    model: str = "google/gemini-2.0-flash-001",
    max_tokens: int = 100000,
    stream: bool = False,
    user_id: int = None,
) -> dict | AsyncGenerator:
    """
    Send a request to OpenRouter API.

    Args:
        messages: List of message dictionaries with 'role' and 'content'
        model: Model identifier (default: deepseek/deepseek-chat)
        max_tokens: Maximum tokens in response (default: 100000)
        stream: Whether to stream the response (default: False)
        user_id: User ID for credit update

    Returns:
        Dictionary containing the API response or AsyncGenerator if stream=True
    """
    if not OPENROUTER_API_KEY:
        raise HTTPException(
            status_code=500, detail="OpenRouter API key not configured")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "X-Title": "XID-AI",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }

    if stream:
        return stream_openrouter_response(headers, payload, user_id)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                OPENROUTER_API_URL,
                headers=headers,
                json=payload,
                timeout=30.0
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=500, detail=f"OpenRouter API error: {str(e)}")


async def stream_openrouter_response(headers: dict, payload: dict, user_id: int = None) -> AsyncGenerator:
    """
    Stream response from OpenRouter API.

    Args:
        headers: Request headers
        payload: Request payload
        user_id: User ID for credit update

    Yields:
        Streamed response chunks
    """
    async with httpx.AsyncClient() as client:
        try:
            async with client.stream(
                'POST',
                OPENROUTER_API_URL,
                headers=headers,
                json=payload,
                timeout=30.0
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.strip():
                        if line.startswith('data: '):
                            line = line[6:]  # Remove 'data: ' prefix
                        if line != '[DONE]':
                            try:
                                chunk = json.loads(line)

                                # 检查是否是最后一个包含usage信息的chunk
                                if 'usage' in chunk and user_id:
                                    usage = chunk.get('usage', {})
                                    print(usage)

                                    prompt_tokens = usage.get(
                                        'prompt_tokens', 0)
                                    completion_tokens = usage.get(
                                        'completion_tokens', 0)
                                    total_tokens = usage.get('total_tokens', 0)

                                    # 计算credit消耗 (Gemini 2.0 Flash 价格)
                                    # $0.1/M input tokens = $0.0001/1K tokens
                                    # $0.4/M output tokens = $0.0004/1K tokens
                                    PROMPT_PRICE_PER_1K = 0.0001
                                    COMPLETION_PRICE_PER_1K = 0.0004

                                    prompt_cost = (
                                        prompt_tokens / 1000) * PROMPT_PRICE_PER_1K
                                    completion_cost = (
                                        completion_tokens / 1000) * COMPLETION_PRICE_PER_1K
                                    total_cost = prompt_cost + completion_cost

                                    logger.info(
                                        f"Usage Stats:\n"
                                        f"Tokens - Prompt: {prompt_tokens}, Completion: {completion_tokens}, Total: {total_tokens}\n"
                                        f"Credits - Prompt: ${prompt_cost:.7f}, Completion: ${completion_cost:.7f}, Total: ${total_cost:.7f}"
                                    )

                                    # 更新用户的credit
                                    db = SessionLocal()
                                    try:
                                        # 获取用户当前credit
                                        user = db.execute(
                                            select(User).where(
                                                User.id == user_id)
                                        ).scalar_one_or_none()

                                        if user:
                                            # 扣除使用的credit
                                            user.credits = user.credits - total_cost
                                            db.commit()
                                    except Exception as e:
                                        logger.error(
                                            f"Error updating user credit: {str(e)}")
                                        db.rollback()
                                    finally:
                                        db.close()

                                # 提取并格式化内容
                                if 'choices' in chunk:
                                    content = chunk['choices'][0].get(
                                        'delta', {}).get('content', '')
                                    if content:
                                        yield f"data: {json.dumps({'content': content})}\n\n"

                            except json.JSONDecodeError:
                                continue
        except httpx.HTTPError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"


# TODO: 1.对于for_ai的数据不保存点赞转推引用 2.找到节省token 的方案
async def request_kol_recent_data():
    """
    Fetch recent tweets from all KOLs and store them in Redis cache.
    Optimized for large scale (7000+ KOLs) with 15-min update frequency.
    """
    try:
        redis_client = RedisClient.get_instance()
        db = SessionLocal()
        now = datetime.now(timezone.utc)

        # 获取需要更新的KOL列表
        statement = select(KOL)
        kols = db.execute(statement).scalars().all()

        if not kols:
            return

        # 创建任务列表
        tasks = []
        for kol in kols:
            if not kol.twitter_id:
                continue

            # 检查是否需要更新
            last_update_key = f"kol_tweets:{kol.username}:last_update"
            last_update = redis_client.get(last_update_key)

            if last_update:
                # 确保last_update_time也是UTC时区
                last_update_time = datetime.fromisoformat(
                    last_update).replace(tzinfo=timezone.utc)
                if (now - last_update_time).total_seconds() < 10:
                    continue

            # 添加到任务列表
            tasks.append(update_kol_data(kol, redis_client))

        # 并发执行所有任务
        if tasks:
            await asyncio.gather(*tasks)

    except Exception as e:
        logger.error(f"Error in request_kol_recent_data: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch KOL data: {str(e)}"
        )
    finally:
        db.close()


async def update_kol_data(kol, redis_client):
    print("updating_kol_data...")
    """更新KOL数据的函数"""
    try:
        tweets_data = await get_user_tweets(kol.twitter_id)
        if not tweets_data:
            return

        now = datetime.now(timezone.utc)

        # 获取用户基础信息
        user_info = tweets_data.get('user', {})
        author_info = {
            "username": kol.username,
            "twitter_id": kol.twitter_id,
            "name": user_info.get('name', kol.name),
            "avatar": user_info.get('avatar', ''),
            "description": user_info.get('description', '')
        }

        filtered_tweets = []
        ai_tweets = []
        seen_tweet_ids = set()

        for tweet in tweets_data.get("tweets", []):
            tweet_id = tweet.get('id_str')
            if tweet_id in seen_tweet_ids:
                continue

            # Twitter的时间戳已经是UTC格式，直接解析
            tweet_time = datetime.strptime(
                tweet['created_at'], '%a %b %d %H:%M:%S +0000 %Y').replace(tzinfo=timezone.utc)
            time_diff = now - tweet_time

            # 只保留最近36小时的推文
            if time_diff.total_seconds() <= 36 * 3600:
                seen_tweet_ids.add(tweet_id)

                # AI版本 - 只包含原创推文
                if not tweet.get('retweeted_status_id') and not tweet.get('quoted_status_id'):
                    ai_tweet = {
                        'id': tweet_id,
                        'text': tweet.get('text'),
                        'view': tweet.get('view_count', 0),
                        'time': tweet_time.strftime('%y/%m/%d %H:%M')
                    }
                    ai_tweets.append(ai_tweet)

                # 展示版本 - 包含所有推文及媒体和引用内容
                timeline_tweet = {
                    **tweet,
                    'user': author_info,
                    'media': tweet.get('media'),  # 添加媒体资源
                    'quoted_tweet': tweet.get('quoted_tweet')  # 添加引用推文内容
                }
                filtered_tweets.append((timeline_tweet, tweet_time))

        if filtered_tweets:
            # Sort by timestamp and take the most recent
            filtered_tweets.sort(key=lambda x: x[1], reverse=True)
            filtered_tweets = filtered_tweets[:20]  # 展示版本保留更多推文
            ai_tweets = ai_tweets[:5]  # AI版本只保留最近5条

            pipe = redis_client.pipeline()

            # 更新展示版本的timeline
            timeline_key = f"kol_tweets:timeline:{kol.username}"
            pipe.delete(timeline_key)  # 清除旧数据
            for tweet, tweet_time in filtered_tweets:
                pipe.zadd(
                    timeline_key,
                    {json.dumps(tweet): tweet_time.timestamp()}
                )

            # 更新AI版本
            ai_context = {
                "author": {
                    "username": kol.username,
                    "name": author_info['name'],
                },
                "tweets": ai_tweets
            }

            pipe.setex(
                f"kol_tweets:ai_context:{kol.username}",
                3600,
                json.dumps(ai_context)
            )

            # 维护活跃KOL列表
            pipe.sadd("kol_tweets:active_kols", kol.username)

            # 设置过期时间
            pipe.expire(timeline_key, 3600)
            pipe.expire("kol_tweets:active_kols", 3600)

            # 更新最后更新时间
            pipe.setex(
                f"kol_tweets:{kol.username}:last_update",
                3600,
                now.isoformat()
            )

            pipe.execute()

    except Exception as e:
        logger.error(f"Error updating KOL {kol.username}: {str(e)}")


async def get_cached_kol_tweets(
    username: str = None,
    for_ai: bool = False,
    page: int = 1,
    page_size: int = 20
) -> dict:
    """获取缓存的推文"""
    try:
        redis_client = RedisClient.get_instance()

        if for_ai:
            if username:
                # 获取单个KOL的AI上下文
                cache_key = f"kol_tweets:ai_context:{username}"
                cached_data = redis_client.get(cache_key)
                return json.loads(cached_data) if cached_data else None
            else:
                # 获取所有KOL的AI上下文
                kol_usernames = redis_client.smembers("kol_tweets:active_kols")
                all_contexts = []

                for kol_username in kol_usernames:
                    cache_key = f"kol_tweets:ai_context:{kol_username}"
                    cached_data = redis_client.get(cache_key)
                    if cached_data:
                        all_contexts.append(json.loads(cached_data))

                return all_contexts
        else:
            # 获取展示版本的timeline
            if username:
                # 获取单个KOL的timeline
                timeline_key = f"kol_tweets:timeline:{username}"
                start_idx = (page - 1) * page_size
                end_idx = start_idx + page_size - 1
                total_tweets = redis_client.zcard(timeline_key)

                tweet_data = redis_client.zrevrange(
                    timeline_key,
                    start_idx,
                    end_idx,
                    withscores=True
                )

                # 计算下一页的cursor
                has_next = (start_idx + len(tweet_data)) < total_tweets
                next_cursor = str(page + 1) if has_next else None

                return {
                    "data": [json.loads(tweet_json) for tweet_json, _ in tweet_data],
                    "total_tweets": total_tweets,
                    "cursor": next_cursor
                }
            else:
                # 获取所有KOL的timeline并按时间排序
                pipe = redis_client.pipeline()
                kol_usernames = redis_client.smembers("kol_tweets:active_kols")

                # 使用Redis的ZUNIONSTORE来合并所有KOL的timeline
                merged_key = "temp_merged_timeline"
                timeline_keys = [
                    f"kol_tweets:timeline:{username}" for username in kol_usernames]

                if timeline_keys:
                    # 删除旧的临时key
                    pipe.delete(merged_key)
                    # 合并所有timeline
                    pipe.zunionstore(merged_key, timeline_keys)
                    # 获取合并后的总数
                    pipe.zcard(merged_key)
                    # 获取分页数据
                    start_idx = (page - 1) * page_size
                    end_idx = start_idx + page_size - 1
                    pipe.zrevrange(merged_key, start_idx,
                                   end_idx, withscores=True)
                    # 设置临时key的过期时间
                    pipe.expire(merged_key, 3600)  # 3600秒后过期

                    # 执行所有命令
                    results = pipe.execute()
                    total_tweets = results[2]  # zcard的结果
                    tweet_data = results[3]    # zrevrange的结果

                    # 计算下一页的cursor
                    has_next = (start_idx + len(tweet_data)) < total_tweets
                    next_cursor = str(page + 1) if has_next else None

                    return {
                        "data": [json.loads(tweet_json) for tweet_json, _ in tweet_data],
                        "total_tweets": total_tweets,
                        "cursor": next_cursor
                    }
                else:
                    return {
                        "data": [],
                        "total_tweets": 0,
                        "cursor": None
                    }

    except Exception as e:
        logger.error(f"Error getting cached KOL tweets: {str(e)}")
        return None



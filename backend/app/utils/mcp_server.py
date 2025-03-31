from app.utils.x3 import parse_search_result, parse_tweets_result
import logging

import os
import sys
import json
import redis
import httpx
from typing import Dict, Optional
from pydantic import BaseModel
from datetime import datetime
import aiohttp
from web3 import Web3
from eth_account.messages import encode_defunct

# 配置logger
logger = logging.getLogger(__name__)

sys.path.append(os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../..")))

X_API_KEY = ""
X_API_3_ENDPOINT = ""
X_API_3_HOST = ""

AGENT_PRIVATE_KEY = ""


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


async def get_tweet_info(tweet_id: str) -> dict:
    """
    Retrieves information about a specific tweet using its ID.

    Args:
        tweet_id (str): The ID of the tweet to retrieve information for.

    Returns:
        dict: A dictionary containing:
            {
                "code": 1 for success, 0 for failure,
                "data": tweet data if successful,
                "result": None,
                "msg": "SUCCESS" or error message
            }
    """

    print(X_API_3_ENDPOINT)
    print(os.getenv("X_API_3_HOST"))
    print('calling get_tweet_info')

    querystring = {"resFormat": "json", "tweet_id": tweet_id}
    headers = {
        "x-api-key": X_API_KEY,
        "x-api-host": X_API_3_HOST
    }
    url = X_API_3_ENDPOINT + '/TweetDetailv2'

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=querystring)
        raw_data = response.json()

    print(raw_data)
    # Check if data exists in the response
    if "data" in raw_data:
        return {
            "code": 1,
            "data": raw_data,
            "result": None,
            "msg": "SUCCESS"
        }
    else:
        return {
            "code": 0,
            "data": {},
            "result": None,
            "msg": "Failed to get tweet info"
        }

# ✅checked


async def get_user_info(username: str) -> dict:
    """
    Get X user info
    Args:
        user_id (str): The user's screen name or ID
    Returns:
        dict: Response with status code and data
            {
                "code": 1 for success, 0 for failure,
                "data": user data if successful,
                "result": None,
                "msg": "SUCCESS" or error message
            }
    """
    url = X_API_3_ENDPOINT + "/UserResultByScreenName"
    querystring = {
        "username": username,
        "resFormat": "json"
    }

    headers = {
        "x-api-key": X_API_KEY,
        "x-api-host": X_API_3_HOST
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=querystring)
        raw_data = response.json()

    # Check if data exists in the response
    if "data" in raw_data:
        result = raw_data["data"]["user_results"]["result"]
        return {
            "code": 1,
            "data": {
                "rest_id": result["rest_id"],
                "name": result["core"]["name"],
                "username": result["core"]["screen_name"],
                "avatar": result["avatar"]["image_url"],
                "banner": result.get("banner", {}).get("image_url"),
                "is_verified": result.get("verification", {}).get("is_blue_verified", False),
                "followers_count": result["relationship_counts"]["followers"],
                "following_count": result["relationship_counts"]["following"],
                "tweet_count": result["tweet_counts"]["tweets"],
                "description": result.get("profile_bio", {}).get("description", ""),
                "location": result.get("location", {}).get("location", ""),
                "created_at": result["core"]["created_at"]
            },
            "msg": "SUCCESS"
        }
    else:
        return {
            "code": 0,
            "data": {},
            "msg": "Failed to get user info"
        }
# ✅checked


async def get_user_tweets(user_id: str):
    """
    异步获取用户推文
    Args:
        user_id (str): 用户ID
    Returns:
        dict: 解析后的推文数据
    """
    url = X_API_3_ENDPOINT + "/UserTweets"
    querystring = {
        "user_id": user_id,
    }

    headers = {
        "x-api-key": X_API_KEY,
        "x-api-host": X_API_3_HOST
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=querystring)
        raw_data = response.json()
        return parse_tweets_result(raw_data)


async def get_search_result(q: str, type: str = "Top", cursor: str = "") -> dict:
    """
    Get X search result asynchronously

    Args:
        q (str): Search keywords
        type (str, optional): Search type (Top, Latest, People, Photos, Videos)
        cursor (str, optional): Pagination cursor

    Returns:
        dict: Search results with status code
    """
    url = X_API_3_ENDPOINT + "/Search"

    querystring = {
        "q": q,
        "type": type,
        "cursor": cursor or "",
        "resFormat": "json"
    }

    headers = {
        "x-api-key": X_API_KEY,
        "x-api-host": X_API_3_HOST
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=querystring)
            # Check if data exists and has valid search results
            return parse_search_result(response.json())
    except Exception as e:
        logger.error(f"Error in get_search_result: {str(e)}")
        return {
            "code": 0,
            "data": {},
            "result": None,
            "msg": f"Error: {str(e)}"
        }


async def get_four_meme_data(address: str) -> dict:
    """Get Token Data from FourMeme API by token address and its holders"""
    base_url = "https://four.meme"

    try:
        async with httpx.AsyncClient() as client:
            # First API call to get token data
            token_response = await client.get(
                f"{base_url}/meme-api/v1/private/token/get/v2",
                params={"address": address}
            )
            token_data = token_response.json()

            # 如果第一个请求失败，直接返回错误
            if token_data.get("code") != 0:
                return {
                    "code": 1,
                    "data": {},
                    "msg": f"Failed to get token data: {token_data}"
                }

            try:
                # 如果有token id，获取holders数据
                if "data" in token_data and "id" in token_data["data"]:
                    token_id = token_data["data"]["id"]
                    holders_response = await client.get(
                        f"{base_url}/meme-api/v1/private/token/holder",
                        params={"tokenId": token_id}
                    )
                    holders_data = holders_response.json()
                    # 如果holders数据获取成功，添加到token数据中
                    if holders_data.get("code") == 0:
                        token_data["data"]["holders"] = holders_data.get(
                            "data", [])
            except Exception as e:
                logger.warning(f"Failed to get holders data: {e}")
                # 如果获取holders失败，仍然返回token数据
                token_data["data"]["holders"] = []
            logger.info(token_data)
            return token_data

    except Exception as e:
        logger.error(f"Error fetching data from FourMeme API: {str(e)}")
        return {
            "code": 1,
            "data": {},
            "msg": f"Error: {str(e)}"
        }


async def query_four_meme_token_list(
    token_name: str,
    order_by: str = "OrderDesc",
    listed_pancake: bool = False,
    page_index: int = 1,
    page_size: int = 30,
    symbol: str = ""
) -> dict:
    """
    Query token list from FourMeme API

    Args:
        token_name (str): Name of the token to search
        order_by (str, optional): Sort order. Defaults to "OrderDesc"
        listed_pancake (bool, optional): Filter by PancakeSwap listing. Defaults to False
        page_index (int, optional): Page number. Defaults to 1
        page_size (int, optional): Items per page. Defaults to 30
        symbol (str, optional): Token symbol. Defaults to ""

    Returns:
        dict: Response containing token list data
            {
                "code": int,
                "msg": str,
                "data": List[dict] containing token information
            }
    """
    base_url = "https://four.meme"

    try:
        params = {
            "orderBy": order_by,
            "tokenName": token_name,
            "listedPancake": str(listed_pancake).lower(),
            "pageIndex": page_index,
            "pageSize": page_size,
            "symbol": symbol,
            "labels": ""
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{base_url}/meme-api/v1/private/token/query",
                params=params
            )
            return response.json()

    except Exception as e:
        logger.error(f"Error querying FourMeme token list: {str(e)}")
        return {
            "code": 1,
            "msg": f"Error: {str(e)}",
            "data": []
        }


async def upload_image(image: bytes, filename: str, access_token: str = None) -> dict:
    """
    上传图片到FourMeme API

    Args:
        image: 图片的二进制数据
        filename: 图片文件名
        access_token: FourMeme平台的访问令牌，如不提供则尝试从设置中获取

    Returns:
        dict: 上传结果，包含图片URL等信息
    """
    base_url = "https://four.meme"
    url = f"{base_url}/meme-api/v1/private/token/upload"

    try:
        # 如果没有提供access_token，尝试从配置获取
        if not access_token:
            try:
                from app.core.config import settings
                if hasattr(settings, 'MEME_WEB_ACCESS_TOKEN'):
                    access_token = settings.MEME_WEB_ACCESS_TOKEN
            except ImportError:
                pass

        if not access_token:
            raise ValueError("未提供访问令牌且无法从配置中获取")

        # 准备headers
        headers = {
            "accept": "application/json, text/plain, */*",
            "meme-web-access": access_token,
            "origin": "https://four.meme",
            "referer": "https://four.meme/create-token"
        }

        # 准备multipart/form-data
        import aiohttp
        from aiohttp import FormData

        form = FormData()
        form.add_field('file',
                       image,
                       filename=filename,
                       content_type='image/png')  # 根据实际文件类型调整

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=form) as response:
                if response.status == 200:
                    result = await response.json()
                    return result
                else:
                    error_text = await response.text()
                    raise ValueError(
                        f"上传失败，状态码: {response.status}, 错误: {error_text}")

    except Exception as e:
        import logging
        logging.error(f"上传图片失败: {str(e)}")
        return {
            "status": "error",
            "message": f"上传图片失败: {str(e)}"
        }


class RaisedToken(BaseModel):
    symbol: str = "BNB"
    nativeSymbol: str = "BNB"
    symbolAddress: str = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    deployCost: str = "0"
    buyFee: str = "0.01"
    sellFee: str = "0.01"
    minTradeFee: str = "0"
    b0Amount: str = "8"
    totalBAmount: str = "24"
    totalAmount: str = "1000000000"
    logoUrl: str = "https://static.four.meme/market/68b871b6-96f7-408c-b8d0-388d804b34275092658264263839640.png"
    tradeLevel: list[str] = ["0.1", "0.5", "1"]
    status: str = "PUBLISH"
    buyTokenLink: str = "https://pancakeswap.finance/swap"
    reservedNumber: int = 10
    saleRate: str = "0.8"
    networkCode: str = "BSC"
    platform: str = "MEME"


class CreateTokenRequest(BaseModel):
    name: str
    shortName: str
    desc: str
    imgUrl: str
    totalSupply: int = 1000000000
    raisedAmount: float = 24
    saleRate: float = 0.8
    reserveRate: float = 0
    raisedToken: RaisedToken = RaisedToken()
    launchTime: int = None  # 将在创建时自动设置
    funGroup: bool = False
    preSale: str = "0"
    clickFun: bool = False
    symbol: str = "BNB"
    label: str = "Meme"
    lpTradingFee: float = 0.0025

    def __init__(self, **data):
        # 如果没有提供launchTime，设置为当前时间戳
        if 'launchTime' not in data:
            data['launchTime'] = int(datetime.now().timestamp() * 1000)
        super().__init__(**data)


async def create_token(
    name: str,
    short_name: str,
    description: str,
    image_url: str,
    access_token: str = None
) -> Dict:
    """
    简化的创建Token函数，只需要提供基本信息，拿到签名后用于调用创建Token合约

    Args:
        name: Token名称
        short_name: Token简称
        description: Token描述
        image_url: Token图片URL
        access_token: FourMeme平台的访问令牌，如不提供则尝试从设置中获取

    Returns:
        Dict: 包含创建结果的响应数据
    """
    try:
        # 创建请求数据
        token_data = CreateTokenRequest(
            name=name,
            shortName=short_name,
            desc=description,
            imgUrl=image_url
        )

        # 这几个参数通过合约的方式帮我生成签名

        # 如果没有提供access_token，尝试从配置获取
        if not access_token:
            try:
                from app.core.config import settings
                if hasattr(settings, 'MEME_WEB_ACCESS_TOKEN'):
                    access_token = settings.MEME_WEB_ACCESS_TOKEN
            except ImportError:
                pass

        if not access_token:
            raise ValueError("未提供访问令牌且无法从配置中获取")

        # 准备请求
        base_url = "https://four.meme"
        url = f"{base_url}/meme-api/v1/private/token/create"

        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "meme-web-access": access_token,
            "origin": "https://four.meme",
            "referer": "https://four.meme/create-token"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=token_data.dict()
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("code") == 0:
                        return {
                            "status": "success",
                            "data": result["data"]
                        }
                    else:
                        raise ValueError(f"创建Token失败: {result.get('msg')}")
                else:
                    error_text = await response.text()
                    raise ValueError(
                        f"请求失败，状态码: {response.status}, 错误: {error_text}")

    except Exception as e:
        import logging
        logging.error(f"创建Token失败: {str(e)}")
        return {
            "status": "error",
            "message": f"创建Token失败: {str(e)}"
        }

async def call_four_meme_contract(
    create_arg: str,
    signature: str,
) -> Dict:
    """
    调用合约部署FourMeme Token

    Args:
        create_arg: FourMeme API返回的createArg参数
        signature: FourMeme API返回的signature参数
        contract_address: FourMeme工厂合约地址
        access_token: FourMeme平台的访问令牌（可选）

    Returns:
        Dict: 合约部署结果，包含交易哈希等信息
    """
    try:

        private_key = AGENT_PRIVATE_KEY

        if not private_key:
            raise ValueError("未提供私钥，无法进行合约交互")

        # 默认使用BSC主网RPC
        rpc_url = "https://bsc-dataseed.binance.org/"

        # 初始化Web3连接
        web3 = Web3(Web3.HTTPProvider(rpc_url))

        if not web3.is_connected():
            raise ValueError(f"无法连接到RPC节点: {rpc_url}")

        # 获取账户信息
        account = web3.eth.account.from_key(private_key)
        wallet_address = account.address

        logger.info(f"使用地址 {wallet_address} 部署FourMeme Token")

        # 合约ABI - 只包含createToken方法
        abi = [
            {
                "inputs": [
                    {
                        "internalType": "bytes",
                        "name": "createArg",
                        "type": "bytes"
                    },
                    {
                        "internalType": "bytes",
                        "name": "signature",
                        "type": "bytes"
                    }
                ],
                "name": "createToken",
                "outputs": [
                    {
                        "internalType": "address",
                        "name": "",
                        "type": "address"
                    }
                ],
                "stateMutability": "nonpayable",
                "type": "function"
            }
        ]

        contract_address = "0x5c952063c7fc8610ffdb798152d69f0b9550762b" 
        # 创建合约实例
        contract = web3.eth.contract(
            address=web3.to_checksum_address(contract_address), abi=abi)

        # 准备交易数据
        # 检查create_arg和signature格式
        if not create_arg.startswith("0x"):
            create_arg = "0x" + create_arg

        if not signature.startswith("0x"):
            signature = "0x" + signature

        # 获取nonce
        nonce = web3.eth.get_transaction_count(wallet_address)

        # 估算gas
        gas_price = web3.eth.gas_price

        # 构建交易
        try:
            # 估算gas限制
            gas_estimate = contract.functions.createToken(
                create_arg,
                signature
            ).estimate_gas({"from": wallet_address})

            # 添加20%的buffer
            gas_limit = int(gas_estimate * 1.2)
        except Exception as e:
            logger.warning(f"估算gas失败，使用默认值: {str(e)}")
            gas_limit = 3000000  # 默认gas限制

        # 构建交易
        transaction = contract.functions.createToken(
            create_arg,
            signature
        ).build_transaction({
            'chainId': 56,  # BSC主网chainId
            'gas': gas_limit,
            'gasPrice': gas_price,
            'nonce': nonce,
        })

        # 签名交易
        signed_txn = web3.eth.account.sign_transaction(
            transaction, private_key)

        # 发送交易
        tx_hash = web3.eth.send_raw_transaction(signed_txn.rawTransaction)

        # 返回交易哈希
        tx_hash_hex = web3.to_hex(tx_hash)
        logger.info(f"交易已发送，交易哈希: {tx_hash_hex}")

        # 等待交易收据
        logger.info("等待交易确认...")
        tx_receipt = web3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=180)

        # 获取部署的token地址
        token_address = None
        if tx_receipt.status == 1:  # 交易成功
            # 获取第一个日志的地址，这就是创建的Token地址
            if tx_receipt.logs and len(tx_receipt.logs) > 0:
                token_address = web3.to_checksum_address(tx_receipt.logs[0]['address'])
                logger.info(f"找到Token地址: {token_address}")

            return {
                "status": "success",
                "tx_hash": tx_hash_hex,
                "token_address": token_address,
                "block_number": tx_receipt.blockNumber,
                "gas_used": tx_receipt.gasUsed,
                "message": "FourMeme Token已成功部署"
            }
        else:
            raise ValueError(f"交易失败，状态码: {tx_receipt.status}")

    except Exception as e:
        logger.error(f"部署FourMeme Token失败: {str(e)}")
        return {
            "status": "error",
            "message": f"部署FourMeme Token失败: {str(e)}"
        }


# 完整流程：创建Token信息并部署合约
async def create_four_meme_token(
    name: str,
    short_name: str,
    description: str,
    image_url: str,
    access_token: str = None
) -> Dict:
    """
    完整流程：先创建Token信息获取签名，然后部署合约

    Args:
        name: Token名称
        short_name: Token简称
        description: Token描述
        image_url: Token图片URL
        access_token: FourMeme平台的访问令牌

    Returns:
        Dict: 完整的部署结果
    """
    try:
        # 步骤1：创建Token信息并获取签名
        create_result = await create_token(
            name=name,
            short_name=short_name,
            description=description,
            image_url=image_url,
            access_token=access_token
        )

        if create_result.get("status") != "success":
            return create_result

        # 从API返回中获取createArg和signature
        token_data = create_result.get("data", {})
        create_arg = token_data.get("createArg")
        signature = token_data.get("signature")

        if not create_arg or not signature:
            raise ValueError("创建Token成功但未返回所需的合约参数")

        # 步骤2：部署合约
        deploy_result = await call_four_meme_contract(
            create_arg=create_arg,
            signature=signature
        )

        # 合并结果
        if deploy_result.get("status") == "success":
            return {
                "status": "success",
                "token_id": token_data.get("tokenId"),
                "tx_hash": deploy_result.get("tx_hash"),
                "token_address": deploy_result.get("token_address"),
                "message": "FourMeme Token已成功创建并部署"
            }
        else:
            return deploy_result

    except Exception as e:
        logger.error(f"创建并部署Token失败: {str(e)}")
        return {
            "status": "error",
            "message": f"创建并部署Token失败: {str(e)}"
        }


class NonceRequest(BaseModel):
    accountAddress: str
    verifyType: str = "LOGIN"
    networkCode: str = "BSC"


async def generate_nonce(account_address: str) -> str:
    """
    生成Four.meme平台登录所需的Nonce值

    Args:
        account_address: 用户的钱包地址

    Returns:
        str: 生成的Nonce值
    """
    try:
        base_url = "https://four.meme"
        url = f"{base_url}/meme-api/v1/private/user/nonce/generate"

        # 准备请求数据
        payload = NonceRequest(
            accountAddress=account_address
        )

        # 准备headers
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://four.meme",
            "referer": "https://four.meme/create-token",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=payload.dict()
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("code") == 0:  # 成功状态检查
                        return result["data"]
                    else:
                        raise ValueError(f"获取Nonce失败: {result.get('msg')}")
                else:
                    error_text = await response.text()
                    raise ValueError(
                        f"请求失败，状态码: {response.status}, 错误: {error_text}")

    except Exception as e:
        logger.error(f"获取Nonce失败: {str(e)}")
        raise ValueError(f"获取Nonce失败: {str(e)}")


def sign_meme_message(nonce: str) -> Dict:
    """
    对Four.meme平台登录消息进行签名

    Args:
        nonce: 从服务器获取的Nonce值

    Returns:
        Dict: 包含签名、钱包地址和原始消息的字典
    """
    try:
        # 使用环境变量中的私钥
        private_key = AGENT_PRIVATE_KEY

        if not private_key:
            raise ValueError("未提供签名私钥")

        # 创建签名消息
        message = f"You are sign in Meme {nonce}"

        web3 = Web3()

        # 获取钱包地址
        account = web3.eth.account.from_key(private_key)
        wallet_address = account.address

        # 创建签名
        eth_message = encode_defunct(text=message)
        signed_message = web3.eth.account.sign_message(
            eth_message, private_key=private_key
        )

        # 返回签名信息
        return {
            "signature": signed_message.signature.hex(),
            "address": wallet_address,
            "message": message
        }

    except Exception as e:
        logger.error(f"签名消息失败: {str(e)}")
        raise ValueError(f"签名消息失败: {str(e)}")


class VerifyInfo(BaseModel):
    address: str
    networkCode: str = "BSC"
    signature: str
    verifyType: str = "LOGIN"


class LoginRequest(BaseModel):
    region: str = "WEB"
    langType: str = "EN"
    loginIp: str = ""
    inviteCode: str = ""
    verifyInfo: VerifyInfo
    walletName: str = "OKX Wallet"


async def login_with_signature(wallet_address: str, signature: str) -> Dict:
    """
    使用签名登录Four.meme平台

    Args:
        wallet_address: 用户钱包地址
        signature: 消息签名
        network_code: 网络代码，默认为BSC

    Returns:
        Dict: 登录结果，包含token和其他用户信息
    """
    try:
        base_url = "https://four.meme"
        url = f"{base_url}/meme-api/v1/private/user/login/dex"

        # 创建验证信息对象
        verify_info = VerifyInfo(
            address=wallet_address,
            networkCode="BSC",
            signature=signature,
            verifyType="LOGIN"
        )

        # 准备登录请求数据
        payload = LoginRequest(
            verifyInfo=verify_info
        )

        # 准备headers
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://four.meme",
            "referer": "https://four.meme/create-token"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=payload.dict()
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("code") == 0:  # 成功状态检查
                        # 保存返回的token
                        token = result["data"]

                        return {
                            "status": "success",
                            "token": token,
                            "message": "登录成功"
                        }
                    else:
                        raise ValueError(f"登录失败: {result.get('msg')}")
                else:
                    error_text = await response.text()
                    raise ValueError(
                        f"请求失败，状态码: {response.status}, 错误: {error_text}")

    except Exception as e:
        logger.error(f"登录失败: {str(e)}")
        return {
            "status": "error",
            "message": f"登录失败: {str(e)}"
        }

# 整合函数，实现一键登录


async def login_to_meme() -> Dict:
    """
    一键登录到Four.meme平台

    Args:
        private_key: 签名用的私钥，不提供则使用环境变量中的私钥

    Returns:
        Dict: 登录结果
    """
    try:
        # 使用提供的私钥或环境变量中的私钥
        private_key = AGENT_PRIVATE_KEY

        if not private_key:
            raise ValueError("未提供签名私钥")

        # 获取钱包地址
        from web3 import Web3
        from eth_account.messages import encode_defunct

        web3 = Web3()
        account = web3.eth.account.from_key(private_key)
        wallet_address = account.address

        # 第一步：获取Nonce
        nonce = await generate_nonce(wallet_address)

        # 第二步：创建签名消息
        message = f"You are sign in Meme {nonce}"

        # 第三步：签名消息
        eth_message = encode_defunct(text=message)
        signed_message = web3.eth.account.sign_message(
            eth_message, private_key=private_key
        )
        signature = signed_message.signature.hex()

        # 第四步：使用签名登录
        login_result = await login_with_signature(
            wallet_address=wallet_address,
            signature=signature
        )

        return login_result

    except Exception as e:
        logger.error(f"一键登录失败: {str(e)}")
        return {
            "status": "error",
            "message": f"一键登录失败: {str(e)}"
        }


async def create_complete_four_meme_token(
    name: str,
    symbol: str,
    description: str,
    image_url: str
) -> Dict:
    """
    完整的FourMeme Token创建流程：
    1. 登录FourMeme平台获取访问令牌
    2. 上传Token图片
    3. 创建Token信息获取签名
    4. 调用合约部署Token
    
    Args:
        name: Token名称
        symbol: Token符号/简称
        description: Token描述
        image_url: Token图片URL
        
    Returns:
        Dict: 包含创建结果的详细信息
    """
    try:
        logger.info(f"开始创建FourMeme Token: {name} ({symbol})")
        
        # 步骤1：登录FourMeme平台
        logger.info("步骤1: 登录FourMeme平台")
        login_result = await login_to_meme()
        
        if login_result.get("status") != "success":
            logger.error(f"登录失败: {login_result.get('message')}")
            return {
                "status": "error",
                "step": "login",
                "message": f"登录FourMeme平台失败: {login_result.get('message')}"
            }
            
        access_token = login_result.get("token")
        logger.info("登录成功，获取到访问令牌")
        
        # 步骤2：上传Token图片
        logger.info("步骤2: 上传Token图片")

        # 步骤3：创建Token信息获取签名
        logger.info("步骤3: 创建Token信息获取签名")
        create_result = await create_token(
            name=name,
            short_name=symbol,
            description=description,
            image_url=image_url,
            access_token=access_token
        )
        
        if create_result.get("status") != "success":
            logger.error(f"创建Token信息失败: {create_result.get('message')}")
            return {
                "status": "error",
                "step": "create_token",
                "message": f"创建Token信息失败: {create_result.get('message')}"
            }
            
        token_data = create_result.get("data", {})
        create_arg = token_data.get("createArg")
        signature = token_data.get("signature")
        token_id = token_data.get("tokenId")
        
        if not create_arg or not signature:
            error_msg = "创建Token成功但未返回所需的合约参数"
            logger.error(error_msg)
            return {
                "status": "error",
                "step": "create_token",
                "message": error_msg
            }
            
        logger.info(f"Token信息创建成功，获取到签名和参数")
        
        # 步骤4：调用合约部署Token
        logger.info("步骤4: 调用合约部署Token")
        deploy_result = await call_four_meme_contract(
            create_arg=create_arg,
            signature=signature
        )
        
        if deploy_result.get("status") != "success":
            logger.error(f"部署合约失败: {deploy_result.get('message')}")
            return {
                "status": "error",
                "step": "deploy_contract",
                "message": f"部署Token合约失败: {deploy_result.get('message')}"
            }
            
        token_address = deploy_result.get("token_address")
        tx_hash = deploy_result.get("tx_hash")
        
        logger.info(f"Token部署成功! 地址: {token_address}, 交易哈希: {tx_hash}")
        
        # 返回完整结果
        return {
            "status": "success",
            "message": "FourMeme Token创建并部署成功",
            "data": {
                "token_id": token_id,
                "token_name": name,
                "token_symbol": symbol,
                "description": description,
                "image_url": image_url,
                "token_address": token_address,
                "tx_hash": tx_hash,
                "block_number": deploy_result.get("block_number"),
                "gas_used": deploy_result.get("gas_used")
            }
        }
        
    except Exception as e:
        logger.error(f"创建FourMeme Token流程失败: {str(e)}")
        return {
            "status": "error",
            "message": f"创建FourMeme Token流程发生错误: {str(e)}"
        }
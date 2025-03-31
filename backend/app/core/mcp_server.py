from app.utils.mcp_server import get_cached_kol_tweets, get_user_info, get_user_tweets, get_search_result, get_four_meme_data
from mcp.server.fastmcp import FastMCP
from typing import Dict, Any
import logging
import os
import aiohttp
import datetime
import sys

from dotenv import load_dotenv

load_dotenv()

# 添加父级目录到路径，以便可以导入 app 包中的模块
sys.path.append(os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../..")))


# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP("Crypto X Tools")

# Create a cache for tool results
tools_cache = {}

# Simple helper for HTTP requests


async def fetch_url(url, headers=None, params=None):
    """Fetch data from URL"""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as response:
            return await response.json()


@mcp.tool()
async def get_twitter_user(username: str) -> Dict[str, Any]:
    """Get detailed information about a Twitter user.

    Use this tool when you need to view someone's profile information (without tweets).

    Args:
        username: Twitter username (without @ symbol)
    """
    # Remove potential @ prefix
    username = username.strip().lstrip('@')

    # Check cache
    cache_key = f"user_{username}"
    if cache_key in tools_cache:
        logger.info(f"Returning user info from cache: {username}")
        return tools_cache[cache_key]

    try:
        logger.info(f"Getting Twitter user info: {username}")

        result = await get_user_info(username)
        if result["code"] == 1:
            # Cache result
            tools_cache[cache_key] = result["data"]
            return result["data"]
        else:
            error_msg = result.get("msg", "Failed to get user information")
            logger.error(f"Failed to get Twitter user info: {error_msg}")
            return {
                "error": f"Failed to get user information: {error_msg}",
                "username": username
            }
    except Exception as e:
        logger.error(
            f"Error getting Twitter user info: {str(e)}", exc_info=True)
        return {
            "error": f"Failed to get user information: {str(e)}",
            "username": username
        }

# Twitter Tool 2: Get User Tweets


@mcp.tool()
async def get_tweets(username: str = None, user_id: str = None, count: int = 10) -> Dict[str, Any]:
    """Get the latest tweets from a Twitter user.

    Use this tool when you want to see someone's tweets. You must provide either username or user_id.
    当你回复用户的时候，如果涉及到参考的推文内容， 把参考的推文id放到后面， 格式如：最近，内容 [tweet_id]
    
    Args:
        username: Twitter username (without @ symbol)
        user_id: Twitter user ID
        count: Number of tweets to retrieve (default: 10)
    """
    try:
        # Parameter validation
        if not username and not user_id:
            raise ValueError("You must provide either username or user_id")

        # If username is provided but not user_id, get the user ID first
        if not user_id and username:
            username = username.strip().lstrip('@')
            logger.info(f"Getting user ID from username: {username}")

            # Try to get user info from cache
            user_cache_key = f"user_{username}"
            user_info = None

            if user_cache_key in tools_cache:
                user_info = tools_cache[user_cache_key]
                logger.info(f"Got user info from cache: {username}")
            else:
                # Get user info
                try:
                    user_result = await get_user_info(username)
                    logger.info(f"API returned user info: {user_result}")

                    if user_result["code"] == 1:
                        user_info = user_result["data"]
                        # Cache user info
                        tools_cache[user_cache_key] = user_info
                    else:
                        error_msg = user_result.get(
                            "msg", "Failed to get user information")
                        logger.error(
                            f"Failed to get Twitter user info: {error_msg}")
                        return {
                            "error": f"User '{username}' not found: {error_msg}",
                            "tweets": []
                        }
                except Exception as e:
                    logger.error(
                        f"Error calling user info API: {str(e)}", exc_info=True)
                    return {
                        "error": f"Error getting info for user '{username}': {str(e)}",
                        "tweets": []
                    }

            if user_info and "rest_id" in user_info:
                user_id = user_info["rest_id"]
                logger.info(
                    f"Successfully got user ID: {user_id} (username: {username})")
            else:
                logger.error(
                    f"Could not get ID from user info, user_info: {user_info}")
                return {
                    "error": f"Could not get ID for user '{username}'",
                    "tweets": []
                }

        # Check cache
        cache_key = f"tweets_{user_id}_{count}"
        if cache_key in tools_cache:
            logger.info(f"Returning tweets from cache: user_id={user_id}")
            return tools_cache[cache_key]

        logger.info(f"Getting user tweets: user_id={user_id}, count={count}")
        try:
            tweets_result = await get_user_tweets(user_id)

            # Limit the number of returned tweets
            if "tweets" in tweets_result and isinstance(tweets_result["tweets"], list):
                tweets_result["tweets"] = tweets_result["tweets"][:int(count)]
                logger.info(
                    f"Successfully got {len(tweets_result['tweets'])} tweets")
            else:
                logger.warning(
                    f"Tweet data format not as expected: {tweets_result}")
                # Try to build standard format
                if isinstance(tweets_result, dict):
                    # Try to extract tweets from different possible structures
                    tweet_data = []

                    # Check various possible data structures
                    for key in ["data", "results", "tweets", "timeline"]:
                        if key in tweets_result and isinstance(tweets_result[key], list):
                            tweet_data = tweets_result[key]
                            break

                    tweets_result = {
                        "tweets": tweet_data[:int(count)],
                        "user_id": user_id
                    }

                    logger.info(
                        f"Reconstructed tweet data: {len(tweets_result['tweets'])} tweets")
                else:
                    tweets_result = {
                        "tweets": [], "error": "Cannot parse API response format", "user_id": user_id}

            # Cache result
            tools_cache[cache_key] = tweets_result

            return tweets_result
        except Exception as e:
            logger.error(
                f"Error calling user tweets API: {str(e)}", exc_info=True)
            return {
                "error": f"Error getting tweets: {str(e)}",
                "tweets": [],
                "user_id": user_id
            }
    except Exception as e:
        logger.error(f"Error getting user tweets: {str(e)}", exc_info=True)
        return {
            "error": str(e),
            "tweets": []
        }


@mcp.tool()
async def get_kol_tweets() -> Dict[str, Any]:
    """Get cached KOL tweets data

    Use this tool to get the latest KOL tweets data from cache.
    """
    logger.info("Getting cached KOL tweets data")

    # Check cache (with 15 min expiry)
    return await get_cached_kol_tweets(for_ai=True)


@mcp.tool()
async def x_search(q: str, type: str = "Top", cursor: str = "") -> Dict[str, Any]:
    """Search X for tweets, users, photos, and videos

    Args:
        q (str): Search keywords
        type (str, optional): Search type (Top, Latest, People, Photos, Videos)
        cursor (str, optional): Pagination cursor

    Returns:
        dict: Search results with status code
    """
    return await get_search_result(q, type, cursor)

# New Tool 1: Crypto Price Checker


@mcp.tool()
async def get_crypto_price(symbol: str) -> Dict[str, Any]:
    """Get current price and basic market data for a cryptocurrency

    Use this tool to fetch the latest price information for a cryptocurrency.

    Args:
        symbol: Cryptocurrency symbol (e.g., BTC, ETH, SOL)
    """
    symbol = symbol.upper().strip()
    logger.info(f"Getting price data for {symbol}")

    # Check cache (with 5 min expiry)
    cache_key = f"price_{symbol}"
    if cache_key in tools_cache:
        cached_data = tools_cache[cache_key]
        # Check if cache is still valid (5 minutes)
        cache_time = cached_data.get("cache_time", 0)
        if (datetime.datetime.now().timestamp() - cache_time) < 300:
            logger.info(f"Returning cached price data for {symbol}")
            return cached_data

    try:
        # Use CoinGecko API (free, no API key required)
        coin_id_map = {
            "BTC": "bitcoin",
            "ETH": "ethereum",
            "SOL": "solana",
            "BNB": "binancecoin",
            "USDT": "tether",
            "XRP": "ripple",
            "DOGE": "dogecoin",
            "ADA": "cardano",
            "MATIC": "matic-network",
            "DOT": "polkadot",
            "AVAX": "avalanche-2",
            "UNI": "uniswap",
            "LINK": "chainlink",
            "SHIB": "shiba-inu",
            "PEPE": "pepe",
        }

        # Handle custom symbols that aren't in our map
        coin_id = coin_id_map.get(symbol, symbol.lower())

        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "community_data": "false",
            "developer_data": "false"
        }

        data = await fetch_url(url, params=params)

        # Extract relevant information
        if "market_data" not in data:
            return {
                "error": f"Could not fetch price data for {symbol}",
                "symbol": symbol
            }

        market_data = data["market_data"]

        result = {
            "symbol": symbol,
            "name": data.get("name", ""),
            "price_usd": market_data.get("current_price", {}).get("usd", 0),
            "market_cap_usd": market_data.get("market_cap", {}).get("usd", 0),
            "volume_24h_usd": market_data.get("total_volume", {}).get("usd", 0),
            "price_change_24h_percent": market_data.get("price_change_percentage_24h", 0),
            "high_24h_usd": market_data.get("high_24h", {}).get("usd", 0),
            "low_24h_usd": market_data.get("low_24h", {}).get("usd", 0),
            "image": data.get("image", {}).get("small", ""),
            "cache_time": datetime.datetime.now().timestamp()
        }

        # Cache result
        tools_cache[cache_key] = result

        return result
    except Exception as e:
        logger.error(f"Error getting crypto price: {str(e)}", exc_info=True)
        return {
            "error": f"Failed to get price data: {str(e)}",
            "symbol": symbol
        }

# New Tool 2: Generate Market Analysis


@mcp.tool()
async def get_market_sentiment() -> Dict[str, Any]:
    """Get overall crypto market sentiment and summary of major assets

    Use this tool to get a quick overview of the crypto market sentiment 
    and performance of major cryptocurrencies.
    """
    logger.info("Getting market sentiment")

    # Check cache (with 15 min expiry)
    cache_key = "market_sentiment"
    if cache_key in tools_cache:
        cached_data = tools_cache[cache_key]
        # Check if cache is still valid (15 minutes)
        cache_time = cached_data.get("cache_time", 0)
        if (datetime.datetime.now().timestamp() - cache_time) < 900:
            logger.info("Returning cached market sentiment")
            return cached_data

    try:
        # Get global market data
        global_url = "https://api.coingecko.com/api/v3/global"
        global_data = await fetch_url(global_url)

        if "data" not in global_data:
            return {
                "error": "Could not fetch global market data",
                "timestamp": datetime.datetime.now().isoformat()
            }

        market_data = global_data["data"]

        # Get top coins performance
        coins_url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": "10",
            "page": "1",
            "sparkline": "false",
            "price_change_percentage": "24h"
        }

        top_coins_data = await fetch_url(coins_url, params=params)

        # Determine overall sentiment based on market cap change and dominance
        market_cap_change_24h = market_data.get(
            "market_cap_change_percentage_24h_usd", 0)

        if market_cap_change_24h > 5:
            sentiment = "very_bullish"
        elif market_cap_change_24h > 1:
            sentiment = "bullish"
        elif market_cap_change_24h > -1:
            sentiment = "neutral"
        elif market_cap_change_24h > -5:
            sentiment = "bearish"
        else:
            sentiment = "very_bearish"

        # Prepare top coins summary
        top_coins = []
        for coin in top_coins_data:
            top_coins.append({
                "symbol": coin.get("symbol", "").upper(),
                "name": coin.get("name", ""),
                "price_usd": coin.get("current_price", 0),
                "price_change_24h_percent": coin.get("price_change_percentage_24h", 0),
                "market_cap_usd": coin.get("market_cap", 0)
            })

        result = {
            "timestamp": datetime.datetime.now().isoformat(),
            "overall_sentiment": sentiment,
            "market_cap_usd": market_data.get("total_market_cap", {}).get("usd", 0),
            "market_cap_change_24h_percent": market_cap_change_24h,
            "btc_dominance": market_data.get("market_cap_percentage", {}).get("btc", 0),
            "eth_dominance": market_data.get("market_cap_percentage", {}).get("eth", 0),
            "total_volume_usd": market_data.get("total_volume", {}).get("usd", 0),
            "active_cryptocurrencies": market_data.get("active_cryptocurrencies", 0),
            "top_coins": top_coins,
            "cache_time": datetime.datetime.now().timestamp()
        }

        # Cache result
        tools_cache[cache_key] = result

        return result
    except Exception as e:
        logger.error(
            f"Error getting market sentiment: {str(e)}", exc_info=True)
        return {
            "error": f"Failed to get market sentiment: {str(e)}",
            "timestamp": datetime.datetime.now().isoformat()
        }

@mcp.tool()
async def get_meme_data(address: str) -> Dict[str, Any]:
    """Get token data from FourMeme API

    Use this tool to get token data from FourMeme API by token address
    返回的数据中name为Token Name， ShortName为Token Symbol. 返回格式：TokenName, Symbol
    """
    logger.info(f"Starting get_meme_data for address: {address}")
    try:
        # 获取基本token数据
        token_data = await get_four_meme_data(address)
        
        # 确保返回格式符合 MCP 工具的要求
        if token_data.get("code") == 0:
            return {
                "success": True,
                "data": token_data.get("data", {}),
                "message": "Successfully retrieved token data"
            }
        else:
            return {
                "success": False,
                "data": None,
                "message": token_data.get("msg", "Failed to get token data")
            }
            
    except Exception as e:
        logger.error(f"Error in get_meme_data: {str(e)}")
        return {
            "success": False,
            "data": None,
            "message": f"Error: {str(e)}"
        }

@mcp.tool()
async def query_four_meme_token_list(
    token_name: str,
    order_by: str = "OrderDesc",
    listed_pancake: bool = False,
    page_index: int = 1,
    page_size: int = 30,
    symbol: str = ""
) -> Dict[str, Any]:
    """Query FourMeme for token list

    Use this tool to search for tokens on FourMeme platform based on various criteria.

    Args:
        token_name (str): Name of the token to search for
        order_by (str, optional): Sort order - "OrderDesc" or "OrderAsc". Defaults to "OrderDesc"
        listed_pancake (bool, optional): Filter by PancakeSwap listing status. Defaults to False
        page_index (int, optional): Page number for pagination. Defaults to 1
        page_size (int, optional): Number of items per page. Defaults to 30
        symbol (str, optional): Token symbol filter. Defaults to ""

    Returns:
        Dict[str, Any]: Response containing:
            - code (int): Status code (0 for success)
            - msg (str): Status message
            - data (List[Dict]): List of token information including:
                - id: Token ID
                - address: Token contract address
                - name: Token name
                - symbol: Token symbol
                - price data
                - trading information
                - etc.
    """
    from app.utils.mcp_server import query_four_meme_token_list as utils_query
    return await utils_query(
        token_name=token_name,
        order_by=order_by,
        listed_pancake=listed_pancake,
        page_index=page_index,
        page_size=page_size,
        symbol=symbol
    )

@mcp.tool()
async def create_four_meme_token(
    name: str,
    symbol: str,
    description: str,
    image_url: str
) -> Dict[str, Any]:
    """Create a new FourMeme token
    
    Use this tool to create a new token on the FourMeme platform with specified details.
    
    Args:
        name: Full name of the token
        symbol: Short symbol/ticker for the token (usually 3-5 characters)
        description: Detailed description of the token and its purpose
        image_url: URL to the token's logo or image
        
    Returns:
        Dict containing:
            - success (bool): Whether token creation was successful
            - data: Token data if successful, including contract address
            - message: Success or error message
    """
    from app.utils.mcp_server import create_complete_four_meme_token as utils_create
    return await utils_create(
        name=name, symbol=symbol, description=description, image_url=image_url)

def run_server():
    """Run the MCP server"""
    logger.info("Starting standalone MCP server")
    mcp.run(transport='stdio')


# Main function
if __name__ == "__main__":
    run_server()

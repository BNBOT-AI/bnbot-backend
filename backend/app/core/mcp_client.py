import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

import requests
import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()  # load environment variables from .env

class MCPClient:
    def __init__(self):
        # Initialize session and client objects
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        
        # OpenRouter configuration
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        if not self.openrouter_api_key:
            logger.warning("OPENROUTER_API_KEY environment variable not found, test mode will be used")
            
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"
        self.model = "openai/gpt-4o-2024-11-20"  # Default model, can be changed
        self.tools_cache = []  # 初始化为空列表，而不是None
        self.initialized = False
        self._initializing = False  # 添加标志防止递归

    async def initialize(self, server_script_path: str = None):
        """Initialize the MCP client with the server"""
        if self.initialized:
            logger.info("MCP client already initialized")
            return
            
        if self._initializing:
            logger.warning("MCP client initialization already in progress")
            return
            
        self._initializing = True  # 设置标志防止递归
        
        try:
            # 设置默认服务器脚本路径
            if not server_script_path:
                server_script_path = os.getenv("MCP_SERVER_PATH")
                
            # 如果仍然没有路径，使用内置的服务器脚本路径
            if not server_script_path:
                # 获取当前文件所在目录
                current_dir = os.path.dirname(os.path.abspath(__file__))
                server_script_path = os.path.join(current_dir, "mcp_server.py")
                logger.info(f"Using default server script path: {server_script_path}")
                
                # 确保文件存在
                if not os.path.exists(server_script_path):
                    logger.warning(f"Default server script not found at {server_script_path}")
            
            logger.info(f"Initializing MCP client with server: {server_script_path}")
            await self.connect_to_server(server_script_path)
            # 提前设置为 True，避免 connect_to_server 中的 get_available_tools 调用导致递归
            self.initialized = True
            logger.info("MCP client initialization complete")
        except Exception as e:
            logger.error(f"MCP client initialization error: {str(e)}", exc_info=True)
            raise
        finally:
            self._initializing = False  # 重置标志

    async def connect_to_server(self, server_script_path: str):
        """Connect to an MCP server"""
        logger.info(f"Connecting to MCP server: {server_script_path}")
        
        is_python = server_script_path.endswith('.py')
        is_js = server_script_path.endswith('.js')
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")
            
        command = "python" if is_python else "node"
        server_params = StdioServerParameters(
            command=command,
            args=[server_script_path],
            env=None
        )
        
        try:
            read_stream, write_stream = await self.exit_stack.enter_async_context(stdio_client(server_params))
            self.session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
            
            # Initialize connection
            await self.session.initialize()
            logger.info("MCP server connection initialized")
            
            # 设置初始化标志为True，这样get_available_tools才能正常工作
            # 注意：这里不设置self.initialized，因为在initialize方法中会设置
            # 只是临时告诉get_available_tools可以请求工具列表
            
            # 尝试预加载工具缓存
            try:
                # 这里我们强制清空缓存，确保get_available_tools会从服务器获取工具
                self.tools_cache = []
                
                # 使用get_available_tools获取并缓存工具
                tools = await self.get_available_tools()
                logger.info(f"Preloaded {len(tools)} tools during initialization")
            except Exception as tools_error:
                logger.error(f"Error preloading tools: {str(tools_error)}")
                # 不要因为工具获取失败而中断整个初始化过程
                self.tools_cache = []
        except Exception as e:
            logger.error(f"Error connecting to MCP server: {str(e)}", exc_info=True)
            raise

    async def get_available_tools(self) -> List[Dict[str, Any]]:
        """获取适用于OpenRouter/OpenAI API的工具列表"""
        logger.info("Getting available tools from server")
        
        if not self.initialized:
            # 不要在这里尝试初始化，因为这会导致递归
            logger.warning("MCP client not initialized, returning empty tools list")
            return []
            
        # 如果工具缓存为空，从服务器获取并缓存
        if not self.tools_cache:
            try:
                response = await self.session.list_tools()
                self.tools_cache = []
                
                # 直接将MCP工具转换为API格式
                for tool in response.tools:
                    tool_def = {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "required": []
                            }
                        }
                    }

                    # 处理参数
                    if hasattr(tool, 'inputSchema') and tool.inputSchema:
                        properties = tool.inputSchema.get('properties', {})
                        required = tool.inputSchema.get('required', [])
                        
                        for param_name, param_info in properties.items():
                            tool_def["function"]["parameters"]["properties"][param_name] = {
                                "type": "string",
                                "description": param_info.get('description', '')
                            }
                            
                            if param_name in required:
                                tool_def["function"]["parameters"]["required"].append(param_name)
                    
                    self.tools_cache.append(tool_def)
                
                tool_names = [tool["function"]["name"] for tool in self.tools_cache]
                logger.info(f"Loaded tools: {len(self.tools_cache)}")
                logger.info(f"Available tools: {tool_names}")
            except Exception as e:
                logger.error(f"Error getting available tools: {str(e)}", exc_info=True)
        
        return self.tools_cache

    async def call_openrouter(self, messages, tools=None, stream=False):
        """Call OpenRouter API with streaming support
        
        Args:
            messages: List of message objects
            tools: List of tool definitions
            stream: Whether to enable streaming response
            
        Returns:
            If stream=False: Returns the complete response as a dictionary
            If stream=True: Returns an async generator yielding response chunks
        """
        headers = {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://your-app-domain.com",  # Required by OpenRouter
            "X-Title": "MCP Client"  # Optional, helps identify in OpenRouter dashboard
        }
        
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 1000
        }
        
        if tools:
            payload["tools"] = tools
        
        if stream:
            payload["stream"] = True
        
        logger.info(f"Calling OpenRouter API, model: {self.model}, stream: {stream}")
        
        try:
            if not stream:
                # 原来的非流式请求模式
                response = requests.post(self.openrouter_url, headers=headers, json=payload)
                
                if response.status_code != 200:
                    logger.error(f"OpenRouter API call failed: {response.text}")
                    raise Exception(f"OpenRouter API call failed: {response.text}")
                    
                return response.json()
            else:
                # 流式请求模式
                async def stream_response():
                    async with httpx.AsyncClient() as client:
                        async with client.stream("POST", self.openrouter_url, 
                                                 headers=headers, json=payload, timeout=60.0) as response:
                            
                            if response.status_code != 200:
                                error_text = await response.aread()
                                yield json.dumps({"type": "error", "content": f"API error: {error_text.decode()}"})
                                return
                            
                            buffer = ""
                            async for chunk in response.aiter_bytes():
                                buffer += chunk.decode('utf-8')
                                
                                # 处理SSE格式的数据
                                lines = buffer.split('\n\n')
                                buffer = lines.pop()  # 保留可能不完整的最后一行
                                
                                for line in lines:
                                    if line.startswith('data: '):
                                        data = line[6:]
                                        if data.strip() == '[DONE]':
                                            yield json.dumps({"type": "done"})
                                            continue
                                        
                                        try:
                                            parsed = json.loads(data)
                                            if 'choices' in parsed and len(parsed['choices']) > 0:
                                                delta = parsed['choices'][0].get('delta', {})
                                                
                                                # 处理文本内容
                                                if 'content' in delta and delta['content']:
                                                    yield json.dumps({
                                                        "type": "content", 
                                                        "content": delta['content']
                                                    })
                                                    
                                                # 处理工具调用
                                                if 'tool_calls' in delta:
                                                    yield json.dumps({
                                                        "type": "tool_call_part",
                                                        "delta": delta['tool_calls']
                                                    })
                                        except json.JSONDecodeError:
                                            logger.warning(f"Failed to parse stream data: {data}")
                
                return stream_response()
                
        except Exception as e:
            logger.error(f"Error calling OpenRouter: {str(e)}")
            raise

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a specific tool"""
        logger.info(f"Calling tool: {tool_name} with arguments: {arguments}")
        
        # 检查是否初始化，如果未初始化，尝试初始化
        if not self.initialized:
            logger.warning("MCP client not initialized, attempting to initialize")
            try:
                await self.initialize()
            except Exception as e:
                logger.error(f"Failed to initialize MCP client: {str(e)}")
                return {
                    "success": False,
                    "error": f"MCP client not initialized: {str(e)}"
                }
        
        try:
            # Use session to call the tool
            result = await self.session.call_tool(tool_name, arguments)
            
            # Convert standard MCP result to your format
            return {
                "success": True,
                "data": result.content
            }
        except Exception as e:
            logger.error(f"Error calling tool: {str(e)}", exc_info=True)
            
            # 尝试重连一次
            try:
                logger.info("Attempting to reconnect MCP client after error")
                await self.cleanup()
                await self.initialize()
                
                # 重试工具调用
                result = await self.session.call_tool(tool_name, arguments)
                return {
                    "success": True,
                    "data": result.content
                }
            except Exception as reconnect_error:
                logger.error(f"Failed to reconnect and retry: {str(reconnect_error)}")
                return {
                    "success": False,
                    "error": f"Tool call failed and reconnection attempt failed: {str(e)}"
                }

    async def process_query(self, query: str) -> str:
        """Process a query using OpenRouter and available tools"""
        try:
            logger.info(f"Processing query: {query}")
            
            # System message to define the assistant's role
            messages = [
                {
                    "role": "system",
                    "content": "You are a helpful assistant. Please use the provided tools to help answer user questions."
                },
                {
                    "role": "user", 
                    "content": query
                }
            ]

            # 获取API格式工具列表 - 直接使用，不需要再次转换
            available_tools = await self.get_available_tools()

            # Call OpenRouter API
            logger.info("Calling LLM to process query")
            response_data = await self.call_openrouter(messages, available_tools)
            
            # Process response and tool calls
            final_text = []
            assistant_response = response_data["choices"][0]["message"]
            
            if "content" in assistant_response and assistant_response["content"]:
                final_text.append(assistant_response["content"])
                
            # Handle tool calls
            if "tool_calls" in assistant_response and assistant_response["tool_calls"]:
                logger.info(f"Detected tool calls: {len(assistant_response['tool_calls'])}")
                for tool_call in assistant_response["tool_calls"]:
                    function_call = tool_call["function"]
                    tool_name = function_call["name"]
                    tool_args = json.loads(function_call["arguments"])
                    
                    logger.info(f"Calling tool: {tool_name} with arguments: {tool_args}")
                    # Call the tool
                    result = await self.call_tool(tool_name, tool_args)
                    
                    # Check tool call result
                    if not result.get("success", False):
                        error_msg = result.get("error", "Unknown error")
                        final_text.append(f"[Tool call {tool_name}, args {tool_args}] - Failed: {error_msg}")
                    else:
                        result_content = result.get("data", "No result")
                        final_text.append(f"[Tool call {tool_name}, args {tool_args}]")
                    
                    logger.info(f"Tool call result: {result_content}")
                    
                    # Add tool call result to message history
                    messages.append(assistant_response)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": tool_name,
                        "content": json.dumps(result_content) if isinstance(result_content, dict) else str(result_content)
                    })
                    
                    # Get LLM's next response
                    logger.info("Getting LLM response to tool result")
                    follow_up_response = await self.call_openrouter(messages, available_tools)
                    follow_up_content = follow_up_response["choices"][0]["message"].get("content", "")
                    if follow_up_content:
                        final_text.append(follow_up_content)

            return "\n".join(final_text)
        except Exception as e:
            logger.error(f"Error processing query: {str(e)}", exc_info=True)
            return f"Error processing query: {str(e)}"

    async def chat_loop(self):
        """Run an interactive chat loop"""
        logger.info("MCP Client started")
        print("\nMCP Client Started!")
        print("Available tools:")
        tools = await self.get_available_tools()
        for tool in tools:
            print(f" - {tool['function']['name']}: {tool['function']['description']}")
        
        print("\nSupported commands:")
        print(" - /model <model_name> - Switch model")
        print(" - /quit - Exit")
        print("\nEnter your query or command:")

        while True:
            try:
                query = input("\n> ").strip()

                if query.lower() == '/quit' or query.lower() == 'quit':
                    logger.info("User requested exit")
                    break
                
                # Handle model switch command
                if query.startswith('/model '):
                    model_name = query[7:].strip()
                    self.model = model_name
                    logger.info(f"Model switched to: {model_name}")
                    print(f"Model switched to: {model_name}")
                    continue

                # Process normal query
                response = await self.process_query(query)
                print("\n" + response)

            except Exception as e:
                logger.error(f"Error in chat loop: {str(e)}", exc_info=True)
                print(f"\nError: {str(e)}")

    async def cleanup(self):
        """Clean up resources"""
        logger.info("Beginning resource cleanup")
        try:
            # ExitStack will automatically clean up sessions
            await self.exit_stack.aclose()
            logger.info("Resource cleanup complete")
        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}", exc_info=True)

    async def chat(self, messages, stream=False):
        """Process chat messages and yield responses
        
        Args:
            messages: List of message objects with role and content
            stream: Whether to stream responses
        """
        if not self.initialized:
            logger.warning("MCP client not initialized when calling chat, attempting to initialize")
            try:
                await self.initialize()
            except Exception as e:
                logger.error(f"Failed to initialize MCP client: {str(e)}")
                yield json.dumps({"content": f"Error: MCP client not initialized: {str(e)}"})
                return
        
        # 构建查询字符串
        query_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            query_parts.append(f"{role}: {content}")
        
        query = "\n".join(query_parts)
        
        try:
            response = await self.process_query(query)
            
            if stream:
                # 对于流式传输，分块返回
                chunks = response.split("\n")
                for i, chunk in enumerate(chunks):
                    yield json.dumps({"content": chunk})
                    if i < len(chunks) - 1:  # 不要在最后一个块后添加延迟
                        await asyncio.sleep(0.1)  # 小延迟以模拟流式传输
            else:
                # 对于非流式响应，一次性返回完整响应
                yield json.dumps({"content": response})
        except Exception as e:
            logger.error(f"Error in chat: {str(e)}", exc_info=True)
            error_msg = {"content": f"Error processing chat: {str(e)}"}
            yield json.dumps(error_msg)

    async def direct_query(self, query: str):
        """Process a direct query and return the result
        
        Args:
            query: Raw query string to process
        """
        if not self.initialized:
            logger.error("MCP client not initialized")
            raise RuntimeError("MCP client must be initialized before use")
            
        return await self.process_query(query)

    async def stream_process_query(self, messages: List[Dict[str, str]] = None):
        """流式处理查询，实时返回工具调用信息和结果
        
        使用完整的messages列表进行查询
        
        Args:
            messages: List of message objects with role and content, including system messages
            
        Yields:
            JSON字符串，包含以下类型的事件：
            - content: LLM生成的文本片段
            - tool_call: 当LLM决定调用某个工具时
            - tool_result: 当工具调用完成并返回结果时
            - error: 发生错误时
            - done: 流处理完成
        """
        if not self.initialized:
            try:
                await self.initialize()
            except Exception as e:
                yield json.dumps({"type": "error", "content": f"初始化失败: {str(e)}"})
                return
        
        try:
            # 获取API格式工具列表
            available_tools = await self.get_available_tools()
            
            # 收集工具调用信息
            collected_tool_calls = []
            
            # 使用流式API获取初始响应
            async for chunk in await self.call_openrouter(messages, available_tools, stream=True):
                try:
                    data = json.loads(chunk)
                    chunk_type = data.get("type", "")
                    
                    # 直接转发内容类型的块
                    if chunk_type == "content":
                        yield chunk
                    
                    # 处理工具调用部分
                    elif chunk_type == "tool_call_part":
                        tool_call_deltas = data.get("delta", [])
                        
                        for delta in tool_call_deltas:
                            index = delta.get("index", 0)
                            
                            # 确保collected_tool_calls有足够的位置
                            while len(collected_tool_calls) <= index:
                                collected_tool_calls.append({
                                    "id": None,
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""}
                                })
                            
                            # 更新ID
                            if "id" in delta:
                                collected_tool_calls[index]["id"] = delta["id"]
                            
                            # 更新函数信息
                            if "function" in delta:
                                if "name" in delta["function"]:
                                    collected_tool_calls[index]["function"]["name"] += delta["function"]["name"]
                                
                                if "arguments" in delta["function"]:
                                    collected_tool_calls[index]["function"]["arguments"] += delta["function"]["arguments"]
                            
                            # 检查是否有完整的工具调用
                            if (collected_tool_calls[index]["id"] and 
                                collected_tool_calls[index]["function"]["name"] and 
                                collected_tool_calls[index]["function"]["arguments"]):
                                
                                # 尝试解析参数 - 添加验证完整性的逻辑
                                arguments_str = collected_tool_calls[index]["function"]["arguments"]
                                try:
                                    # 检查JSON字符串是否完整
                                    if arguments_str.strip().endswith("}"):
                                        try:
                                            tool_call = collected_tool_calls[index]
                                            tool_name = tool_call["function"]["name"]
                                            tool_args = json.loads(arguments_str)
                                            
                                            # 通知将要调用工具
                                            yield json.dumps({
                                                "type": "tool_call",
                                                "tool_name": tool_name,
                                                "arguments": tool_args
                                            })
                                            
                                            # 执行工具调用
                                            logger.info(f"Calling tool: {tool_name} with arguments: {tool_args}")
                                            result = await self.call_tool(tool_name, tool_args)
                                        
                                            # 返回工具调用结果
                                            result_data = result.get("data")
                                            serializable_result = self._ensure_serializable(result_data)
                                            yield json.dumps({
                                                "type": "tool_result",
                                                "tool_name": tool_name,
                                                "result": serializable_result
                                            })
                                            
                                            # 处理工具调用后的响应
                                            serialized_content = json.dumps(serializable_result) if isinstance(serializable_result, (dict, list)) else str(serializable_result)
                                            
                                            # 构建包含工具结果的新消息
                                            follow_up_messages = messages.copy()
                                            follow_up_messages.append({
                                                "role": "assistant",
                                                "content": None,
                                                "tool_calls": [tool_call]
                                            })
                                            follow_up_messages.append({
                                                "role": "tool",
                                                "tool_call_id": tool_call["id"],
                                                "name": tool_name,
                                                "content": serialized_content
                                            })
                                            
                                            # 获取后续回复
                                            async for follow_up_chunk in await self.call_openrouter(follow_up_messages, available_tools, stream=True):
                                                yield follow_up_chunk
                                            
                                            # 标记该工具调用已处理，清空内容防止重复处理
                                            collected_tool_calls[index] = {
                                                "id": None, 
                                                "type": "function",
                                                "function": {"name": "", "arguments": ""}
                                            }
                                        except json.JSONDecodeError:
                                            logger.warning(f"无法解析工具参数: {arguments_str}")
                                        except Exception as tool_error:
                                            logger.error(f"工具调用错误: {str(tool_error)}")
                                            yield json.dumps({
                                                "type": "error",
                                                "content": f"工具调用错误: {str(tool_error)}"
                                            })
                                except json.JSONDecodeError:
                                    logger.warning(f"无法解析工具参数: {arguments_str}")
                                except Exception as tool_error:
                                    logger.error(f"工具调用错误: {str(tool_error)}")
                                    yield json.dumps({
                                        "type": "error",
                                        "content": f"工具调用错误: {str(tool_error)}"
                                    })
                    
                    # 处理其他类型的块
                    elif chunk_type == "error":
                        yield chunk
                    elif chunk_type == "done":
                        # 检查是否有未完成的工具调用
                        any_incomplete = False
                        for call in collected_tool_calls:
                            if call["id"] or call["function"]["name"] or call["function"]["arguments"]:
                                any_incomplete = True
                                break
                        
                        if not any_incomplete:
                            yield chunk
                
                except json.JSONDecodeError:
                    logger.warning(f"无法解析JSON: {chunk}")
                    continue
                except Exception as e:
                    logger.error(f"处理流数据错误: {str(e)}")
                    yield json.dumps({"type": "error", "content": f"处理错误: {str(e)}"})
        
        except Exception as e:
            logger.error(f"流处理总体错误: {str(e)}")
            yield json.dumps({"type": "error", "content": f"处理查询错误: {str(e)}"})
        
        # 确保最后发送完成信号
        yield json.dumps({"type": "done"})

    def _ensure_serializable(self, obj):
        """确保对象可以被JSON序列化"""
        if obj is None:
            return None
        
        if isinstance(obj, (str, int, float, bool)):
            return obj
        
        if isinstance(obj, (list, tuple)):
            return [self._ensure_serializable(item) for item in obj]
        
        if isinstance(obj, dict):
            return {k: self._ensure_serializable(v) for k, v in obj.items()}
        
        # 处理MCP特有的类型
        if hasattr(obj, "__class__"):
            class_name = obj.__class__.__name__
            if class_name == "TextContent" and hasattr(obj, "content"):
                return obj.content
            
            # 尝试将对象转换为字典
            if hasattr(obj, "__dict__"):
                try:
                    return {k: self._ensure_serializable(v) for k, v in obj.__dict__.items() 
                           if not k.startswith("_")}
                except:
                    pass
        
        # 最后的手段：转换为字符串
        return str(obj)

# Create a global client instance for use throughout the application
mcp_client = MCPClient()

async def main():
    if len(sys.argv) < 2:
        print("Usage: python client.py <path_to_server_script>")
        sys.exit(1)
        
    client = MCPClient()
    
    try:
        logger.info(f"Starting MCP client with server: {sys.argv[1]}")
        await client.connect_to_server(sys.argv[1])
        await client.chat_loop()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt signal")
        print("\nProgram interrupted")
    except Exception as e:
        logger.error(f"Error running program: {str(e)}", exc_info=True)
        print(f"Program error: {str(e)}")
    finally:
        logger.info("Program exiting, performing cleanup")
        await client.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram interrupted by user")

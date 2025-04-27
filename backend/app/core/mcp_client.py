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
        self.tools_cache = []  # Initialize as an empty list instead of None
        self.initialized = False
        self._initializing = False  # Add a flag to prevent recursion

    async def initialize(self, server_script_path: str = None):
        """Initialize the MCP client with the server"""
        if self.initialized:
            logger.info("MCP client already initialized")
            return
            
        if self._initializing:
            logger.warning("MCP client initialization already in progress")
            return
            
        self._initializing = True  # Set flag to prevent recursion
        
        try:
            # Set default server script path
            if not server_script_path:
                server_script_path = os.getenv("MCP_SERVER_PATH")
                
            # If still no path, use the built-in server script path
            if not server_script_path:
                # Get the current directory of the file
                current_dir = os.path.dirname(os.path.abspath(__file__))
                server_script_path = os.path.join(current_dir, "mcp_server.py")
                logger.info(f"Using default server script path: {server_script_path}")
                
                # Ensure the file exists
                if not os.path.exists(server_script_path):
                    logger.warning(f"Default server script not found at {server_script_path}")
            
            logger.info(f"Initializing MCP client with server: {server_script_path}")
            await self.connect_to_server(server_script_path)
            # Set to True in advance to avoid recursive calls in get_available_tools in connect_to_server
            self.initialized = True
            logger.info("MCP client initialization complete")
        except Exception as e:
            logger.error(f"MCP client initialization error: {str(e)}", exc_info=True)
            raise
        finally:
            self._initializing = False  # Reset flag

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
            
            # Set initialization flag to True so that get_available_tools can work properly
            # Note: Do not set self.initialized here, as it will be set in the initialize method
            # Just temporarily inform get_available_tools that it can request the tool list
            
            # Attempt to preload tool cache
            try:
                # Here we force clear the cache to ensure get_available_tools fetches tools from the server
                self.tools_cache = []
                
                # Use get_available_tools to fetch and cache tools
                tools = await self.get_available_tools()
                logger.info(f"Preloaded {len(tools)} tools during initialization")
            except Exception as tools_error:
                logger.error(f"Error preloading tools: {str(tools_error)}")
                # Do not interrupt the entire initialization process due to tool fetching failure
                self.tools_cache = []
        except Exception as e:
            logger.error(f"Error connecting to MCP server: {str(e)}", exc_info=True)
            raise

    async def get_available_tools(self) -> List[Dict[str, Any]]:
        """Get the list of tools available for OpenRouter/OpenAI API"""
        logger.info("Getting available tools from server")
        
        if not self.initialized:
            # Do not attempt to initialize here, as it would cause recursion
            logger.warning("MCP client not initialized, returning empty tools list")
            return []
            
        # If the tool cache is empty, fetch and cache from the server
        if not self.tools_cache:
            try:
                response = await self.session.list_tools()
                self.tools_cache = []
                
                # Directly convert MCP tools to API format
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

                    # Process parameters
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
                # Original non-streaming request mode
                response = requests.post(self.openrouter_url, headers=headers, json=payload)
                
                if response.status_code != 200:
                    logger.error(f"OpenRouter API call failed: {response.text}")
                    raise Exception(f"OpenRouter API call failed: {response.text}")
                    
                return response.json()
            else:
                # Streaming request mode
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
                                
                                # Process SSE formatted data
                                lines = buffer.split('\n\n')
                                buffer = lines.pop()  # Keep the possibly incomplete last line
                                
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
                                                
                                                # Process text content
                                                if 'content' in delta and delta['content']:
                                                    yield json.dumps({
                                                        "type": "content", 
                                                        "content": delta['content']
                                                    })
                                                    
                                                # Process tool calls
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
        
        # Check if initialized, if not, attempt to initialize
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
            
            # Attempt to reconnect once
            try:
                logger.info("Attempting to reconnect MCP client after error")
                await self.cleanup()
                await self.initialize()
                
                # Retry tool call
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

            # Get API format tool list - use directly, no need to convert again
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
        
        # Build query string
        query_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            query_parts.append(f"{role}: {content}")
        
        query = "\n".join(query_parts)
        
        try:
            response = await self.process_query(query)
            
            if stream:
                # For streaming, return in chunks
                chunks = response.split("\n")
                for i, chunk in enumerate(chunks):
                    yield json.dumps({"content": chunk})
                    if i < len(chunks) - 1:  # Do not add delay after the last chunk
                        await asyncio.sleep(0.1)  # Small delay to simulate streaming
            else:
                # For non-streaming response, return complete response at once
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
        """Stream process query, returning tool call information and results in real-time
        
        Use the complete messages list for the query
        
        Args:
            messages: List of message objects with role and content, including system messages
            
        Yields:
            JSON string containing the following types of events:
            - content: Text fragments generated by LLM
            - tool_call: When LLM decides to call a tool
            - tool_result: When a tool call is completed and returns a result
            - error: When an error occurs
            - done: When stream processing is complete
        """
        if not self.initialized:
            try:
                await self.initialize()
            except Exception as e:
                yield json.dumps({"type": "error", "content": f"Initialization failed: {str(e)}"})
                return
        
        try:
            # Get API format tool list
            available_tools = await self.get_available_tools()
            
            # Collect tool call information
            collected_tool_calls = []
            
            # Use streaming API to get initial response
            async for chunk in await self.call_openrouter(messages, available_tools, stream=True):
                try:
                    data = json.loads(chunk)
                    chunk_type = data.get("type", "")
                    
                    # Directly forward content type chunks
                    if chunk_type == "content":
                        yield chunk
                    
                    # Process tool call parts
                    elif chunk_type == "tool_call_part":
                        tool_call_deltas = data.get("delta", [])
                        
                        for delta in tool_call_deltas:
                            index = delta.get("index", 0)
                            
                            # Ensure collected_tool_calls has enough positions
                            while len(collected_tool_calls) <= index:
                                collected_tool_calls.append({
                                    "id": None,
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""}
                                })
                            
                            # Update ID
                            if "id" in delta:
                                collected_tool_calls[index]["id"] = delta["id"]
                            
                            # Update function information
                            if "function" in delta:
                                if "name" in delta["function"]:
                                    collected_tool_calls[index]["function"]["name"] += delta["function"]["name"]
                                
                                if "arguments" in delta["function"]:
                                    collected_tool_calls[index]["function"]["arguments"] += delta["function"]["arguments"]
                            
                            # Check if there is a complete tool call
                            if (collected_tool_calls[index]["id"] and 
                                collected_tool_calls[index]["function"]["name"] and 
                                collected_tool_calls[index]["function"]["arguments"]):
                                
                                # Attempt to parse arguments - add integrity validation logic
                                arguments_str = collected_tool_calls[index]["function"]["arguments"]
                                try:
                                    # Check if JSON string is complete
                                    if arguments_str.strip().endswith("}"):
                                        try:
                                            tool_call = collected_tool_calls[index]
                                            tool_name = tool_call["function"]["name"]
                                            tool_args = json.loads(arguments_str)
                                            
                                            # Notify that a tool will be called
                                            yield json.dumps({
                                                "type": "tool_call",
                                                "tool_name": tool_name,
                                                "arguments": tool_args
                                            })
                                            
                                            # Execute tool call
                                            logger.info(f"Calling tool: {tool_name} with arguments: {tool_args}")
                                            result = await self.call_tool(tool_name, tool_args)
                                        
                                            # Return tool call result
                                            result_data = result.get("data")
                                            serializable_result = self._ensure_serializable(result_data)
                                            yield json.dumps({
                                                "type": "tool_result",
                                                "tool_name": tool_name,
                                                "result": serializable_result
                                            })
                                            
                                            # Process response after tool call
                                            serialized_content = json.dumps(serializable_result) if isinstance(serializable_result, (dict, list)) else str(serializable_result)
                                            
                                            # Build new message containing tool result
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
                                            
                                            # Get follow-up response
                                            async for follow_up_chunk in await self.call_openrouter(follow_up_messages, available_tools, stream=True):
                                                yield follow_up_chunk
                                            
                                            # Mark this tool call as processed, clear content to prevent reprocessing
                                            collected_tool_calls[index] = {
                                                "id": None, 
                                                "type": "function",
                                                "function": {"name": "", "arguments": ""}
                                            }
                                        except json.JSONDecodeError:
                                            logger.warning(f"Failed to parse tool arguments: {arguments_str}")
                                        except Exception as tool_error:
                                            logger.error(f"Tool call error: {str(tool_error)}")
                                            yield json.dumps({
                                                "type": "error",
                                                "content": f"Tool call error: {str(tool_error)}"
                                            })
                                except json.JSONDecodeError:
                                    logger.warning(f"Failed to parse tool arguments: {arguments_str}")
                                except Exception as tool_error:
                                    logger.error(f"Tool call error: {str(tool_error)}")
                                    yield json.dumps({
                                        "type": "error",
                                        "content": f"Tool call error: {str(tool_error)}"
                                    })
                    
                    # Process other types of chunks
                    elif chunk_type == "error":
                        yield chunk
                    elif chunk_type == "done":
                        # Check if there are any incomplete tool calls
                        any_incomplete = False
                        for call in collected_tool_calls:
                            if call["id"] or call["function"]["name"] or call["function"]["arguments"]:
                                any_incomplete = True
                                break
                        
                        if not any_incomplete:
                            yield chunk
                
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse JSON: {chunk}")
                    continue
                except Exception as e:
                    logger.error(f"Error processing stream data: {str(e)}")
                    yield json.dumps({"type": "error", "content": f"Processing error: {str(e)}"})
        
        except Exception as e:
            logger.error(f"Overall stream processing error: {str(e)}")
            yield json.dumps({"type": "error", "content": f"Error processing query: {str(e)}"})
        
        # Ensure the final completion signal is sent
        yield json.dumps({"type": "done"})

    def _ensure_serializable(self, obj):
        """Ensure the object can be JSON serialized"""
        if obj is None:
            return None
        
        if isinstance(obj, (str, int, float, bool)):
            return obj
        
        if isinstance(obj, (list, tuple)):
            return [self._ensure_serializable(item) for item in obj]
        
        if isinstance(obj, dict):
            return {k: self._ensure_serializable(v) for k, v in obj.items()}
        
        # Handle MCP specific types
        if hasattr(obj, "__class__"):
            class_name = obj.__class__.__name__
            if class_name == "TextContent" and hasattr(obj, "content"):
                return obj.content
            
            # Attempt to convert the object to a dictionary
            if hasattr(obj, "__dict__"):
                try:
                    return {k: self._ensure_serializable(v) for k, v in obj.__dict__.items() 
                           if not k.startswith("_")}
                except:
                    pass
        
        # Last resort: convert to string
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

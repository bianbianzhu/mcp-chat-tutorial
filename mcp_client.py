import os
import sys
import json
import asyncio
from typing import Optional, Any
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from pydantic import AnyUrl

class MCPClient:
    def __init__(
        self,
        command: str,
        args: list[str],
        env: Optional[dict] = None,
    ):
        self._command = command
        self._args = args
        self._env = env
        self._session: Optional[ClientSession] = None
        self._exit_stack: AsyncExitStack = AsyncExitStack()

    def _subprocess_env(self) -> Optional[dict]:
        # By default we let the MCP SDK use its safe behavior: env=None ->
        # get_default_environment() (only PATH/HOME/... — NOT secrets like
        # ANTHROPIC_API_KEY). Only when DEBUG_MCP_SERVER is set do we add the
        # debugger vars on top, so the server's debugpy listener can start. The
        # SDK still merges these with get_default_environment(), so PATH etc.
        # remain available. Secrets are never forwarded to the subprocess.
        env = dict(self._env or {})
        if os.environ.get("DEBUG_MCP_SERVER"):
            env["DEBUG_MCP_SERVER"] = os.environ["DEBUG_MCP_SERVER"]
            env.update(
                {
                    k: v
                    for k, v in os.environ.items()
                    if k.startswith(("DEBUGPY_", "PYDEVD_"))
                }
            )
        return env or None

    async def connect(self):
        server_params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._subprocess_env(),
        )
        stdio_transport = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        _stdio, _write = stdio_transport
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(_stdio, _write)
        )
        await self._session.initialize()

    def session(self) -> ClientSession:
        if self._session is None:
            raise ConnectionError(
                "Client session not initialized or cache not populated. Call connect_to_server first."
            )
        return self._session

    async def list_tools(self) -> list[types.Tool]:
        result = await self.session().list_tools()
        return result.tools

    async def call_tool(
        self, tool_name: str, tool_input: dict
    ) -> types.CallToolResult | None:
        result = await self.session().call_tool(tool_name, tool_input)
        return result

    async def list_prompts(self) -> list[types.Prompt]:
        result = await self.session().list_prompts()
        return result.prompts

    async def get_prompt(self, prompt_name: str, args: dict[str, str]):
        result = await self.session().get_prompt(prompt_name, args)
        return result.messages

    async def read_resource(self, uri: str) -> Any:
        result = await self.session().read_resource(AnyUrl(uri))
        resource = result.contents[0]

        if isinstance(resource, types.TextResourceContents):
            if resource.mimeType == "application/json":
                return json.loads(resource.text)  # docs://documents -> list[str]
            return resource.text  # docs://documents/{id} -> str
        return resource  # BlobResourceContents (binary) — returned as-is

    async def cleanup(self):
        await self._exit_stack.aclose()
        self._session = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()


# For testing
async def main():
    async with MCPClient(
        # If using Python without UV, update command to 'python' and remove "run" from args.
        command="uv",
        args=["run", "mcp_server.py"],
    ) as _client:
        pass
        # result = await _client.list_tools()
        # print(result)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

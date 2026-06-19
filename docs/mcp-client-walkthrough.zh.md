# `mcp_client.py` 逐行精读

这个项目使用了官方的python sdk: https://modelcontextprotocol.io/docs/develop/build-client

以"MCP 客户端在做什么 / 底层发了什么 JSON-RPC"和"这是什么 Python 语法、作用是什么"
两条线索逐行拆解本仓库的 `mcp_client.py`,并与官方 client 快速入门 (quickstart) 互相映照。

quickstart client.py: https://github.com/modelcontextprotocol/quickstart-resources/blob/main/mcp-client-python/client.py

> **grounding 说明**
>
> - 🟢 **已核实**:在本工作区的源码中直接核对过(repo 文件,或 `.venv` 里的 mcp SDK 源码),给出路径 + 行号。
> - 🟡 **示意**:JSON-RPC 报文 (payload) 的整体形状按 MCP 规范给出,属于代表性示例;其中**方法名 (method) 字符串均为 🟢 已核实**,但字段细节(如 `protocolVersion` 取值、`id` 起始值)随 SDK 版本而变。
>
> 状态提示:相对早先的文档,`mcp_client.py` 已更新——`list_tools()` / `call_tool()`
> **已从 stub 变为真实现**;`list_prompts` / `get_prompt` / `read_resource` 仍是 TODO。
> 本文按当前实际代码讲解。

---

## 分层心智模型 (layering)

后文每一行都可以挂到下面某一层上:

```
你的 MCPClient 包装类
   └─ ClientSession         ← 协议层 (protocol):JSON-RPC 2.0 请求/响应按 id 配对、通知 (notification)
        └─ stdio 字节流      ← 传输层 (transport):换行符分隔的 JSON (newline-delimited JSON)
             └─ 子进程        ← 操作系统层:uv run mcp_server.py,通过 stdin/stdout 通信
```

**通信对象**:它启动并连接的是 **MCP server 子进程**——本项目里就是 `mcp_server.py`
(`FastMCP("DocumentMCP")` 服务器),以 `uv run mcp_server.py` 拉起。

**两项底层事实(🟢 已在 SDK 源码核实)**:

- JSON-RPC 方法名字符串存在于 SDK 中:`initialize`、`notifications/initialized`、
  `tools/list`、`tools/call`、`prompts/list`、`prompts/get`、`resources/read`、
  `resources/list`、`resources/templates/list`、`ping`。
- stdio 分帧 (framing):写时 `model_dump_json(...) + "\n"` 再编码发出
  (`.venv/.../mcp/client/stdio/__init__.py:172-174`),读时按 `"\n"` 切分(`:150`)。
  即**每条消息一行 JSON**。

---

## ① 导入 (lines 1-6)

```python
import sys
import asyncio
from typing import Optional, Any
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
```

- **Python 角度**:前四行是标准库(异步运行时 `asyncio`、类型标注 `Optional/Any`、
  `AsyncExitStack`);后两行来自 MCP SDK。
- **MCP 角度**:`from mcp.client.stdio import stdio_client` 这一行就决定了**传输方式是 stdio**
  ——把 server 当子进程拉起、走它的 stdin/stdout。(另两种传输是 `sse` 和
  `streamable-http`,要导入的就是别的东西。)`ClientSession` 是协议层;
  `StdioServerParameters` 描述"怎么拉起子进程";`types` 里是 `Tool` / `CallToolResult` /
  `Prompt` 等数据类型。
- **映照 quickstart**:完全一致。

---

## ② `__init__`:只记配置,不连接 (lines 9-20)

```python
def __init__(self, command: str, args: list[str], env: Optional[dict] = None):
    self._command = command
    self._args = args
    self._env = env
    self._session: Optional[ClientSession] = None
    self._exit_stack: AsyncExitStack = AsyncExitStack()
```

- **Python 角度**:类型标注(`command: str` 等)只是提示,运行时不强制。
  `env: Optional[dict] = None` 表示可选、默认 `None`。前导下划线 `_command` 是
  "约定俗成的私有 (convention for private)"。`self._session: Optional[ClientSession] = None`
  先占位为 `None`——**此刻尚未连接**。
- **MCP 角度**:**一个字节都没发**。仅把"将来怎么启动 server"(命令、参数、环境变量)
  记下来;`AsyncExitStack` 也只是先 new 出来备用。
- **映照 quickstart**:同样是 `self.session = None` + `self.exit_stack = AsyncExitStack()`。

---

## ③ `connect()`:握手发生在这里 (lines 22-35) —— 全文重点

```python
async def connect(self):
    server_params = StdioServerParameters(command=self._command, args=self._args, env=self._env)
    stdio_transport = await self._exit_stack.enter_async_context(stdio_client(server_params))
    _stdio, _write = stdio_transport
    self._session = await self._exit_stack.enter_async_context(ClientSession(_stdio, _write))
    await self._session.initialize()
```

逐句拆开,**重点看"哪一句才真正产生进程/网络行为"**:

**line 23-27 `StdioServerParameters(...)`**

- Python:构造一个配置对象,关键字传参。
- MCP:**还没动**,只是把命令打包成结构体。

**line 28-30 `enter_async_context(stdio_client(server_params))`**

- Python:`stdio_client(...)` 是 `@asynccontextmanager`;`enter_async_context` 进入它并把
  清理登记到 stack(`await` 因为是异步)。
- MCP:**这一句才真正 spawn 子进程**(`uv run mcp_server.py`)并把管道接到它的
  stdin/stdout,返回一对流 `(read_stream, write_stream)`。**仍然没有 JSON-RPC**——
  纯 OS 层的进程启动 + 管道连接。

**line 31 `_stdio, _write = stdio_transport`**

- Python:**元组解包 (tuple unpacking)**,把那对流拆给两个变量。
- 小坑:命名有点误导——`_stdio` 其实是**接收(读)流**,`_write` 是**发送(写)流**。

**line 32-34 `ClientSession(_stdio, _write)`**

- Python:同样 `enter_async_context` 登记到 stack。
- MCP:把裸字节流**包成协议层**(负责 JSON-RPC 的 `id` 配对、通知分发)。
  **构造时也还不发消息**。

**line 35 `await self._session.initialize()`** ← **第一笔 JSON-RPC 流量**

这是 MCP 的**初始化握手 (initialize handshake)**,线缆上实为三条消息
(方法名 🟢 已核实,payload 为 🟡 代表性示意):

```jsonc
// 1) Client → Server  请求(有 id)
{"jsonrpc":"2.0","id":0,"method":"initialize",
 "params":{"protocolVersion":"<日期版本>","capabilities":{...},
           "clientInfo":{"name":"mcp","version":"..."}}}

// 2) Server → Client  响应(同一个 id)
{"jsonrpc":"2.0","id":0,
 "result":{"protocolVersion":"...",
           "capabilities":{"tools":{...},"prompts":{...},"resources":{...}},
           "serverInfo":{"name":"DocumentMCP","version":"..."}}}

// 3) Client → Server  通知(无 id → 不需要响应)
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

每条消息都是**一行 JSON + `\n`** 写入子进程 stdin。握手做三件事:协商协议版本、
互换 capabilities、客户端最后发 `initialized` 表示"我准备好了"。

- **映照 quickstart**:`connect_to_server()` 里就是 `stdio_client → ClientSession →
session.initialize()`,顺序一字不差。

---

## ④ `session()`:防呆访问器 (lines 37-42)

```python
def session(self) -> ClientSession:
    if self._session is None:
        raise ConnectionError("Client session not initialized ...")
    return self._session
```

- **Python**:**注意它不是 `async`**,就是个普通方法 / getter。
- **MCP**:不发消息。作用是:谁要用 session,先确认 `connect()` 跑过了,否则抛
  `ConnectionError`,避免对着 `None` 调方法。下面 `list_tools` / `call_tool` 都经它取 session。

---

## ⑤ 真正的操作:tools 已实现,prompts/resources 仍是 TODO (lines 44-64)

```python
async def list_tools(self) -> list[types.Tool]:
    result = await self.session().list_tools()
    return result.tools

async def call_tool(self, tool_name: str, tool_input: dict) -> types.CallToolResult | None:
    result = await self.session().call_tool(tool_name, tool_input)
    return result
```

- **`list_tools`(44-46)**
  - MCP/wire:发 `{"id":N,"method":"tools/list","params":{}}`(🟢 `tools/list`),server 回
    `{"result":{"tools":[{name, description, inputSchema}, ...]}}`。SDK 解析成
    `ListToolsResult`,`.tools` 是 `list[types.Tool]`。
  - Python:`result.tools` 取出列表返回。
- **`call_tool`(48-52)**
  - MCP/wire:发 `{"id":N,"method":"tools/call","params":{"name":tool_name,"arguments":tool_input}}`
    (🟢 `tools/call`),server 回 `CallToolResult`(含 `.content` 内容块列表 + `.isError`)。
  - Python:返回类型 `types.CallToolResult | None` 里的 `|` 是 **PEP 604 联合类型 (union type)** 写法。
- **`list_prompts` / `get_prompt` / `read_resource`(54-64)**:仍是 stub,返回 `[]`。
  实现后分别对应线缆上的 `prompts/list`、`prompts/get`、`resources/read`(三个方法名 🟢 都在 SDK 里)。
  注意:`get_prompt` 的 `prompt_name` 漏了类型标注;`read_resource(uri)` 现收 `str`,
  但调用 `session.read_resource` 时官方要求传 `AnyUrl(uri)`——实现时需包一层。

---

## ⑥ `cleanup()` 与异步上下文协议 (lines 66-75)

```python
async def cleanup(self):
    await self._exit_stack.aclose()
    self._session = None

async def __aenter__(self):
    await self.connect()
    return self

async def __aexit__(self, exc_type, exc_val, exc_tb):
    await self.cleanup()
```

- **`cleanup`**:`aclose()` 按 **LIFO** 反向退栈——先关 `ClientSession`(收掉协议层、
  取消其 task group),再关 `stdio_client`(终止子进程、关管道),最后 `_session = None`。
  - MCP/wire:stdio 传输**没有显式的"关闭" JSON-RPC**;关闭即关掉 stdin → server 读到
    **EOF** → 自行退出,进程随后被回收。
- **`__aenter__` / `__aexit__`**:实现**异步上下文管理器协议 (async context manager protocol)**,
  使外部可写 `async with MCPClient(...)`;二者只是转发到 `connect` / `cleanup`。
  (为何用这套而非直接内联 `async with`——见 [`mcp-client-lifecycle-patterns.md`](./mcp-client-lifecycle-patterns.md)
  的 Finding 1/2:session 需要跨方法存活。)

---

## ⑦ 自测入口 + Windows 处理 (lines 78-91)

```python
async def main():
    async with MCPClient(command="uv", args=["run", "mcp_server.py"]) as _client:
        pass

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
```

- **`main`**:`async with` 进去就 `pass`——一个**冒烟测试 (smoke test)**:连上(含 initialize
  握手)→ 立刻 cleanup。能跑通即说明"拉起子进程 → 握手 → 收尾"整条链路 OK。
- **`__main__` 卫语句 (guard)**:`sys.platform == "win32"` 时切到
  `WindowsProactorEventLoopPolicy`——因为 Windows 上**子进程管道**需要 Proactor 事件循环才支持。
  `asyncio.run(main())` 是从同步世界进入异步世界的入口。

---

## 一句话总结

`connect()` 之前全是"记配置 / 搭管子",真正的 MCP 协议流量从 `session.initialize()`
那一刻才开始;之后每个 `list_tools` / `call_tool` 各对应一来一回的 JSON-RPC,
全部以"一行 JSON"的形式在子进程 stdin/stdout 上流动。

---

## 来源

本仓库(🟢):`mcp_client.py:1-91`(逐行)。
SDK 源码 in `.venv`(🟢):

- `mcp/client/stdio/__init__.py:105-106`(`stdio_client` 为 `@asynccontextmanager`)、
  `:150`(读端按 `"\n"` 切分)、`:172-174`(写端 `model_dump_json(...) + "\n"`)。
- JSON-RPC 方法名字符串(`initialize` / `notifications/initialized` / `tools/list` /
  `tools/call` / `prompts/list` / `prompts/get` / `resources/read` / `resources/list` 等)
  均在 mcp 包内核实存在。
  官方 client 快速入门(对照):https://modelcontextprotocol.io/quickstart/client

# MCP 客户端生命周期模式

如何在 Python SDK 中管理 MCP `ClientSession` 的生命周期、本仓库的 `mcp_client.py`
为何采用现有结构，以及该结构是否为推荐做法。

> **接地标记说明 (Grounding legend)**
> - 🟢 **已在本仓库 / SDK 源码中验证** —— 在调研过程中直接对照本工作区中的文件
>   （路径 + 行号）进行了核对。
> - 🔵 **外部来源，已抓取** —— 取自通过网络抓取的官方文档 / GitHub 源码，并附有
>   URL。
> - 🟡 **外部来源，未经独立复核** —— issue 编号 / 引文系在一次间歇性搜索服务中断
>   （2026 年 6 月）期间通过委托的网络抓取收集而来；请将相关编号视为指示性信息，
>   并通过链接进行确认。

---

## TL;DR

- 官方 README 中那种**一次性、嵌套 `async with`** 的写法将整个会话保持在单个函数
  代码块内部。而在真实应用中，会话几乎总是需要**比打开它的函数活得更久**（打开
  一次，之后多次调用各种方法，在关闭时再清理），因此这种内联写法很少能够直接照搬
  使用。
- 本仓库的 `mcp_client.py` 通过**将一个 `AsyncExitStack` 存储在实例上**来解决这一
  问题 —— 在 `connect()` 中打开上下文，在 `cleanup()` 中关闭它们。这是**官方客户端
  快速入门 (quickstart) 模式**，并非本仓库的发明。
- 对于**单服务器、单任务的 CLI**（恰好正是本应用的形态 —— 这一点是 🟢 已接地的）
  而言，它是**可接受的**。但根据下文的网络调研 / issues（🟡，未经独立验证），它似乎
  也是最容易触发 anyio 的 "cancel scope in a different task" 错误的模式，而所调研的
  成熟库都避免使用它，转而采用每会话一个 `@asynccontextmanager`，或显式的
  connect/disconnect。

---

## 背景：两个内部对象都是异步上下文管理器 (async context manager) 🟢

`connect()` 所打开的两样东西都是异步上下文管理器，这一点已在所安装的 SDK
（`.venv/lib/python3.13/site-packages/`）中得到验证：

- `stdio_client` 被 `@asynccontextmanager` 装饰
  —— `mcp/client/stdio/__init__.py:105-106`。
- `ClientSession`（定义于 `mcp/client/session.py:103`）继承自 `BaseSession`，后者
  定义了 `__aenter__` / `__aexit__`
  —— `mcp/shared/session.py:221` 与 `:227`。因此它本身就是一个异步 CM。

`contextlib.AsyncExitStack.enter_async_context` "Enters the supplied async context
manager. If successful, also pushes its `__aexit__` method as a callback and
returns the result of the `__aenter__` method."（标准库文档字符串，已通过
`python -c` 验证）。因此，在栈上进入一个 CM 会使其保持**打开**状态，直到该栈被
关闭 —— 这正是本仓库所依赖的全部机制。

---

## 发现 1 —— README 中那种一次性内联写法在实践中很少见

SDK README / 快速入门代码片段把所有事情都放在一个函数里完成，并在 `with` 代码块
**内部**使用会话：🔵

```python
# https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md
async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        prompts = await session.list_prompts()   # used INSIDE the blocks
        ...
# blocks exit here -> cleanup runs -> fine, because we are DONE using it
```

这是正确的，但仅仅因为使用过程从未离开该代码块。一旦真实应用想要**打开一次并在
后续调用中重用该会话**，这种形态就不再适用了：如果你把这些 `async with` 代码块放进
一个 `connect()` 方法里并让它返回，那么两个上下文都会立即退出，被存储的会话也就
失效了。

本仓库恰好就是这种"真实应用"的情形 —— `Chat.run()`（`core/chat.py:24`）中的
`while True` 智能体循环 (agentic loop) 在每一轮都会通过 `core/tools.py` 中的
`ToolManager`（`get_all_tools` / `execute_tool_requests`，从 `core/chat.py:27,34`
调用；客户端方法在 `mcp_client.py:44-64` 处以桩 (stub) 形式存在）对一个长生命周期
的客户端驱动 `list_tools()` / `call_tool()` 🟢，并且 `main.py` 在启动时一次性打开
所有客户端，并在结束时将它们拆除 🟢（`main.py:38` 的 `AsyncExitStack`，文档客户端
在 `main.py:39-41` 处进入）。因此，内联的一次性写法在这里从来都不是一个选项；会话
必须比 `connect()` 活得更久。

---

## 发现 2 —— 本仓库的 `mcp_client.py` 就是官方快速入门模式 🟢🔵

`mcp_client.py`（本仓库）将一个 `AsyncExitStack` 存储在实例上，并把
setup/teardown 拆分到多个方法中：🟢

- `__init__`：`self._exit_stack = AsyncExitStack()` —— `mcp_client.py:20`
- `connect()`：先 `enter_async_context(stdio_client(...))`，再
  `enter_async_context(ClientSession(...))`，然后 `await session.initialize()`
  —— `mcp_client.py:22-35`
- `cleanup()`：`await self._exit_stack.aclose()` —— `mcp_client.py:66-68`
- 它还暴露了委托给 connect/cleanup 的 `__aenter__` / `__aexit__`
  —— `mcp_client.py:70-75`，因此调用方仍可写成 `async with MCPClient(...)`
  （测试用的 `main()` 就是这么做的 —— `mcp_client.py:79-85`）。

这与**官方 MCP 客户端快速入门**中所教授的结构相同（`MCPClient` 带有
`self.exit_stack = AsyncExitStack()`，`connect_to_server()` 使用
`enter_async_context`，`cleanup()` 在一个 `try/finally` 中调用
`await self.exit_stack.aclose()`）：🔵
- https://modelcontextprotocol.io/quickstart/client
- https://github.com/modelcontextprotocol/quickstart-resources/blob/main/mcp-client-python/client.py

因此，这个文件的形态是教程标准形态，而非本地的偏离做法。

---

## 发现 3 —— 真实 / 成熟项目实际的做法

| 项目 | 模式 | 机制 | 来源 |
|---|---|---|---|
| 官方快速入门 `MCPClient` | 在类上挂一个 `AsyncExitStack`（**= 本仓库**） | `connect()` 进入上下文，`cleanup()` 执行 `aclose()` | 🔵 quickstart-resources `client.py` |
| SDK README | 在单个函数中嵌套 `async with` | 一次性，会话在代码块内部使用 | 🔵 python-sdk README |
| LangChain `langchain-mcp-adapters` | 每会话一个 `@asynccontextmanager` | 顶层的 `create_session(...) -> AsyncIterator[ClientSession]` 进行 `yield`；该客户端类**有意**设计为*不*作为 CM（`__aenter__` 抛出 `NotImplementedError`） | 🟡 `langchain_mcp_adapters/sessions.py` |
| `mcp-use` | 显式的 `connect()` / `disconnect()` | 会话存放在一个 dict 中；手动调用 `session.__aenter__()` / `__aexit__()`，**不使用** AsyncExitStack | 🟡 mcp-use |
| Strands `MCPClient` | 专用后台线程 + 自有事件循环 | 会话存活于单个协程的 `async with` 之内，靠一个 close-future 保持存活；暴露**同步**的 `__enter__`/`__exit__` | 🟡 strands-agents/sdk-python |

**结论要点：** 在类上挂 `AsyncExitStack` 的 connect/cleanup 模式是*教程*模式。
**此处所调研的**每一个库似乎都选择了别的做法 —— 每会话一个
`@asynccontextmanager`（LangChain）、显式的命令式生命周期（mcp-use），或一个专用的
长期运行任务/线程（Strands）。这些针对各个库的描述属于 🟡（已抓取，未经独立验证 ——
在依赖某个具体细节之前，请对照所链接的源码进行确认）。

来源：🔵/🟡
- https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md
- https://github.com/langchain-ai/langchain-mcp-adapters/blob/main/langchain_mcp_adapters/sessions.py
- https://github.com/mcp-use/mcp-use
- https://github.com/strands-agents/sdk-python

---

## 发现 4 —— anyio 的 "cancel scope" 陷阱

`stdio_client` 与 `ClientSession` 在内部打开了 anyio 的任务组 (task group) / 取消域
(cancel scope)（例如 `BaseSession.__aenter__` 会进入 `self._task_group`
—— `mcp/shared/session.py:221-223` 🟢）。anyio 要求取消域必须在**进入它的同一个
任务中退出**，并且遵循 **LIFO** 顺序。把栈存储在一个对象上，并跨任务边界调用
`connect()` / `cleanup()` —— 或者乱序关闭多个客户端的栈 —— 都会违反这一约束，并
抛出：

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

该问题在 python-sdk 中被反复报告。🟡 **下面列表中的所有内容 —— issue 编号、
开启/关闭状态、PR 关联，以及维护者的引文 —— 都是在一次搜索服务中断期间通过网络
抓取收集的，_未经_独立验证。请将其视为线索，而非引证：在依赖任何具体细节之前，
请打开链接进行确认。**
- **#79**（已关闭）—— 使用 `AsyncExitStack` 时出现 cancel-scope 错误；报告者得出
  结论认为这"很可能不是正确的做法"（不要 `asyncio.create_task(stack.aclose())`）。
- **#252**（已关闭）→ 由 **PR #353** 修复：simple-chatbot 的清理逻辑从并行的
  `asyncio.gather()` 改为**顺序逆向 (LIFO)** 的顺序。
- **#521**（已关闭）—— 同一问题的 SSE 变体。
- **#577**（开启中，P1）—— 在乱序清理**多个**客户端时出现错误；分析中提到
  "a fundamental limitation of anyio's structured concurrency … anyio enforces
  strict LIFO cleanup … AsyncExitStack allows arbitrary order."
- **#831**（已关闭）—— 使用 `AsyncExitStack` 而非 `async with` 的
  `streamablehttp_client`；该 issue 本身指出 README 的 `async with` 写法
  "works fine and does not emit these warnings."
- **#922**（已关闭）—— 据称是一条维护者评论（归属于 @felixweinberger）：
  "an issue … potentially with anyio implementation details — we're unlikely to
  move away from anyio in the short term as this would be a major refactor."
  （引文/归属未经验证 —— 请在该讨论串中确认。）

Issues 索引：https://github.com/modelcontextprotocol/python-sdk/issues/

---

## 对本项目的建议

- **目前保持 `mcp_client.py` 原样。** 本应用是一个单服务器、单任务的 CLI：`main.py`
  在单个 asyncio 任务和单个 `AsyncExitStack` 内完成 `connect → run loop → cleanup`
  全过程 🟢（`main.py:38-59`）。这恰好是快速入门模式可以安全运行的理想路径 ——
  同一任务、LIFO 清理。要填充那些桩方法，无需做任何改动。
- **了解其边界。** 一旦该客户端被用于单个线性任务之外，这种模式就会失效：在不同
  的任务中关闭（`asyncio.create_task(cleanup())`）、FastAPI 的启动/关闭钩子、信号
  处理器，或者管理多个各自持有独立栈、却乱序关闭的客户端。
- **若 / 当上述情况发生时，** 应切换到 LangChain 风格的**每会话
  `@asynccontextmanager`**（打开传输层 + `ClientSession`，再 `yield` 它），这是最
  符合习惯用法且最可复用的设计，并能规避跨任务的 cancel-scope 陷阱。对于多个长生命
  周期的客户端，可让每个会话运行在它自己的 anyio 任务中、靠一个关闭事件保持存活，
  或运行在一个专用后台线程中（Strands 风格）。

---

## 来源

本地已验证（🟢）：
- 本仓库：`mcp_client.py:20,22-35,44-64,66-68,70-75,79-85`；`main.py:38-59`；
  `core/chat.py`（智能体循环）。
- `.venv` 中的 SDK 源码：`mcp/client/stdio/__init__.py:105-106`；
  `mcp/client/session.py:103`；`mcp/shared/session.py:221-238`。

外部来源（🔵 已抓取 / 🟡 未经独立复核）：
- 客户端快速入门：https://modelcontextprotocol.io/quickstart/client
- 快速入门 `client.py`：https://github.com/modelcontextprotocol/quickstart-resources/blob/main/mcp-client-python/client.py
- python-sdk README：https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md
- LangChain adapters：https://github.com/langchain-ai/langchain-mcp-adapters/blob/main/langchain_mcp_adapters/sessions.py
- mcp-use：https://github.com/mcp-use/mcp-use
- Strands SDK：https://github.com/strands-agents/sdk-python
- Issues：https://github.com/modelcontextprotocol/python-sdk/issues/ (#79, #252+PR#353, #521, #577, #831, #922)

_注：在调研期间，WebSearch 遭遇了一次间歇性的分类器服务中断；外部的 GitHub 发现是
通过直接抓取源码收集的。issue 编号/标题/状态系当时（2026 年 6 月）所报告的状态，
并标记为 🟡。_

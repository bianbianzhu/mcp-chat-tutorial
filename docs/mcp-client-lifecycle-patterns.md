# MCP Client Lifecycle Patterns

How to manage the lifecycle of an MCP `ClientSession` in the Python SDK, why this
repo's `mcp_client.py` is shaped the way it is, and whether that shape is the
recommended one.

> **Grounding legend**
> - 🟢 **Verified in this repo / SDK source** — checked directly against files in
>   this workspace (paths + line numbers) during research.
> - 🔵 **External, fetched** — taken from official docs / GitHub source fetched
>   over the web. URLs given.
> - 🟡 **External, not independently re-verified** — issue numbers / quotes
>   gathered via delegated web fetches during an intermittent search outage
>   (June 2026); treat numbers as indicative, follow the link to confirm.

---

## TL;DR

- The official README's **one-shot, nested `async with`** style keeps the whole
  session inside a single function block. In real applications a session almost
  always has to **outlive the function that opened it** (open once, call many
  methods later, close at shutdown), so the inline style is rarely usable as-is.
- This repo's `mcp_client.py` solves that with an **`AsyncExitStack` stored on the
  instance** — open contexts in `connect()`, close them in `cleanup()`. This is
  **the official client quickstart pattern**, not an invention of this repo.
- It is **acceptable for a single-server, single-task CLI** (exactly this app's
  shape — this part is 🟢 grounded). Per the web survey/issues below (🟡, not
  independently verified) it also appears to be the pattern most prone to anyio's
  "cancel scope in a different task" error, and the mature libraries surveyed
  avoid it in favor of a per-session `@asynccontextmanager` or an explicit
  connect/disconnect.

---

## Background: both inner objects are async context managers 🟢

The two things `connect()` opens are both async context managers, verified in the
installed SDK (`.venv/lib/python3.13/site-packages/`):

- `stdio_client` is decorated `@asynccontextmanager`
  — `mcp/client/stdio/__init__.py:105-106`.
- `ClientSession` (defined at `mcp/client/session.py:103`) subclasses `BaseSession`,
  which defines `__aenter__` / `__aexit__`
  — `mcp/shared/session.py:221` and `:227`. So it is itself an async CM.

`contextlib.AsyncExitStack.enter_async_context` "Enters the supplied async context
manager. If successful, also pushes its `__aexit__` method as a callback and
returns the result of the `__aenter__` method." (stdlib docstring, verified via
`python -c`). So entering a CM on the stack keeps it **open** until the stack is
closed — that is the whole mechanism this repo relies on.

---

## Finding 1 — the README's one-shot inline style is rare in practice

The SDK README / quickstart snippet does everything inside one function, and uses
the session **inside** the `with` blocks: 🔵

```python
# https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md
async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        prompts = await session.list_prompts()   # used INSIDE the blocks
        ...
# blocks exit here -> cleanup runs -> fine, because we are DONE using it
```

This is correct, but only because usage never leaves the block. The moment a real
app wants to **open once and reuse the session across later calls**, this shape no
longer fits: if you put these `async with` blocks inside a `connect()` method and
let it return, both contexts exit immediately and the stored session is dead.

This repo is exactly that "real app" case — the `while True` agentic loop in
`Chat.run()` (`core/chat.py:24`) drives `list_tools()` / `call_tool()` on a
long-lived client every turn, via `ToolManager` in `core/tools.py`
(`get_all_tools` / `execute_tool_requests`, called from `core/chat.py:27,34`;
client methods stubbed at `mcp_client.py:44-64`) 🟢, and
`main.py` opens all clients once at startup and tears them down at the end 🟢
(`main.py:38` `AsyncExitStack`, doc client entered at `main.py:39-41`). So the
inline one-shot style was never an option here; the session must outlive
`connect()`.

---

## Finding 2 — this repo's `mcp_client.py` IS the official quickstart pattern 🟢🔵

`mcp_client.py` (this repo) stores an `AsyncExitStack` on the instance and splits
setup/teardown across methods: 🟢

- `__init__`: `self._exit_stack = AsyncExitStack()` — `mcp_client.py:20`
- `connect()`: `enter_async_context(stdio_client(...))` then
  `enter_async_context(ClientSession(...))`, then `await session.initialize()`
  — `mcp_client.py:22-35`
- `cleanup()`: `await self._exit_stack.aclose()` — `mcp_client.py:66-68`
- It also exposes `__aenter__` / `__aexit__` delegating to connect/cleanup
  — `mcp_client.py:70-75`, so callers can still write `async with MCPClient(...)`
  (the test `main()` does — `mcp_client.py:79-85`).

This is the same structure taught in the **official MCP client quickstart**
(`MCPClient` with `self.exit_stack = AsyncExitStack()`, `connect_to_server()` using
`enter_async_context`, and `cleanup()` calling `await self.exit_stack.aclose()` in a
`try/finally`): 🔵
- https://modelcontextprotocol.io/quickstart/client
- https://github.com/modelcontextprotocol/quickstart-resources/blob/main/mcp-client-python/client.py

So the shape of this file is the tutorial-standard shape, not a local deviation.

---

## Finding 3 — what real / mature projects actually do

| Project | Pattern | Mechanism | Source |
|---|---|---|---|
| Official quickstart `MCPClient` | `AsyncExitStack` on the class (**= this repo**) | `connect()` enters contexts, `cleanup()` `aclose()`s | 🔵 quickstart-resources `client.py` |
| SDK README | nested `async with` in one function | one-shot, session used inside the block | 🔵 python-sdk README |
| LangChain `langchain-mcp-adapters` | per-session `@asynccontextmanager` | top-level `create_session(...) -> AsyncIterator[ClientSession]` that `yield`s; the client class **intentionally** is *not* a CM (`__aenter__` raises `NotImplementedError`) | 🟡 `langchain_mcp_adapters/sessions.py` |
| `mcp-use` | explicit `connect()` / `disconnect()` | sessions in a dict; manual `session.__aenter__()` / `__aexit__()`, **no** AsyncExitStack | 🟡 mcp-use |
| Strands `MCPClient` | dedicated background thread + own event loop | session lives inside one coroutine's `async with`, kept alive on a close-future; exposes **sync** `__enter__`/`__exit__` | 🟡 strands-agents/sdk-python |

**Takeaway:** the `AsyncExitStack`-on-a-class connect/cleanup pattern is the
*tutorial* pattern. Every library **surveyed here** appears to choose something
else — per-session `@asynccontextmanager` (LangChain), explicit imperative
lifecycle (mcp-use), or a dedicated long-running task/thread (Strands). These
per-library descriptions are 🟡 (fetched, not independently verified — confirm
against the linked source before relying on a specific detail).

Sources: 🔵/🟡
- https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md
- https://github.com/langchain-ai/langchain-mcp-adapters/blob/main/langchain_mcp_adapters/sessions.py
- https://github.com/mcp-use/mcp-use
- https://github.com/strands-agents/sdk-python

---

## Finding 4 — the anyio "cancel scope" pitfall

`stdio_client` and `ClientSession` open anyio task groups / cancel scopes
internally (e.g. `BaseSession.__aenter__` enters `self._task_group`
— `mcp/shared/session.py:221-223` 🟢). anyio requires a cancel scope be **exited
in the same task it was entered**, in **LIFO** order. Storing the stack on an
object and calling `connect()` / `cleanup()` across task boundaries — or closing
several clients' stacks out of order — violates that and raises:

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

Reported repeatedly against the python-sdk. 🟡 **Everything in the list below —
issue numbers, open/closed status, the PR linkage, and the maintainer quote — was
gathered via web fetch during a search outage and is _not_ independently verified.
Treat it as a pointer, not a citation: open the links to confirm before relying on
any specific.**
- **#79** (closed) — cancel-scope error with `AsyncExitStack`; reporter concluded it
  is "probably not the correct approach" (don't `asyncio.create_task(stack.aclose())`).
- **#252** (closed) → fixed by **PR #353**: simple-chatbot cleanup changed from
  parallel `asyncio.gather()` to **sequential reversed (LIFO)** order.
- **#521** (closed) — SSE variant of the same problem.
- **#577** (open, P1) — error cleaning up **multiple** clients out-of-order;
  analysis cites "a fundamental limitation of anyio's structured concurrency …
  anyio enforces strict LIFO cleanup … AsyncExitStack allows arbitrary order."
- **#831** (closed) — `streamablehttp_client` with `AsyncExitStack` instead of
  `async with`; issue itself notes the README's `async with` "works fine and does
  not emit these warnings."
- **#922** (closed) — reportedly a maintainer comment (attributed to
  @felixweinberger): "an issue … potentially with anyio implementation details —
  we're unlikely to move away from anyio in the short term as this would be a major
  refactor." (quote/attribution unverified — confirm in the thread.)

Issues index: https://github.com/modelcontextprotocol/python-sdk/issues/

---

## Recommendation for this project

- **Keep `mcp_client.py` as-is for now.** This app is a single-server,
  single-task CLI: `main.py` does `connect → run loop → cleanup` all within one
  asyncio task and one `AsyncExitStack` 🟢 (`main.py:38-59`). That is precisely
  the happy path the quickstart pattern is safe in — same task, LIFO cleanup. No
  change needed to fill in the stubbed methods.
- **Know the boundary.** The pattern breaks if this client ever gets used outside
  one linear task: closing in a different task (`asyncio.create_task(cleanup())`),
  FastAPI startup/shutdown hooks, signal handlers, or managing multiple clients
  with independent stacks closed out of order.
- **If/when that happens,** switch to the LangChain-style **per-session
  `@asynccontextmanager`** (open transport + `ClientSession`, `yield` it), which is
  the most idiomatic and reusable design and sidesteps the cross-task cancel-scope
  trap. For multiple long-lived clients, run each session in its own anyio task kept
  alive on a shutdown event, or a dedicated background thread (Strands style).

---

## Sources

Verified locally (🟢):
- This repo: `mcp_client.py:20,22-35,44-64,66-68,70-75,79-85`; `main.py:38-59`;
  `core/chat.py` (agentic loop).
- SDK source in `.venv`: `mcp/client/stdio/__init__.py:105-106`;
  `mcp/client/session.py:103`; `mcp/shared/session.py:221-238`.

External (🔵 fetched / 🟡 not independently re-verified):
- Client quickstart: https://modelcontextprotocol.io/quickstart/client
- quickstart `client.py`: https://github.com/modelcontextprotocol/quickstart-resources/blob/main/mcp-client-python/client.py
- python-sdk README: https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md
- LangChain adapters: https://github.com/langchain-ai/langchain-mcp-adapters/blob/main/langchain_mcp_adapters/sessions.py
- mcp-use: https://github.com/mcp-use/mcp-use
- Strands SDK: https://github.com/strands-agents/sdk-python
- Issues: https://github.com/modelcontextprotocol/python-sdk/issues/ (#79, #252+PR#353, #521, #577, #831, #922)

_Note: during research, WebSearch hit an intermittent classifier outage; the
external GitHub findings were gathered by directly fetching sources. Issue
numbers/titles/status are as reported then (June 2026) and are marked 🟡._

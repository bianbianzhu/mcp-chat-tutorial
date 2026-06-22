# The `/slash-command` (MCP prompt) flows

> 中文版见 [`slash-command-prompt-flow.zh.md`](slash-command-prompt-flow.zh.md). The two docs stay in sync.

Typing `/` drives **two different flows**, exactly like `@` does (see
[`at-mention-resource-flow.md`](at-mention-resource-flow.md)). This doc separates them and
traces each function-by-function, with `file:line`, then lists the **bugs** in the current
implementation.

> **Grounding legend**
> - 🟢 **Verified** — checked against repo source (`file:line`) or the mcp SDK in `.venv/`,
>   or observed from a headless probe in-session.
> - 🟡 **Behavioral** — depends on the model's runtime decision, not fixed by code.

A "slash command" *is* an **MCP prompt** (`@mcp.prompt`). The server publishes named,
parameterized message templates; the client lists them (`prompts/list`), renders one with
arguments (`prompts/get`), and splices the returned messages straight into the conversation.

## The two flows at a glance

| | **Flow A — autocomplete** | **Flow B — render + agent loop** |
|---|---|---|
| Trigger | you type `/` … (NO Enter) | `/format plan.md` then **Enter** |
| What you see | dropdown of command names / doc ids; ghost arg hint | the model's answer |
| `list_prompts` (`prompts/list`) | **not called here** — reads a cached list filled **once at startup** | not called |
| `get_prompt` (`prompts/get`) | never involved | **called live** to render the template |
| Server round-trip on the keystroke/submit? | **No** (local cache) | **Yes** (`prompts/get` ×1) |

> ⚠️ **Same misconception as with `@`:** "typing `/` calls `list_prompts`." It does **not**.
> `UnifiedCompleter.get_completions` is **synchronous** (`cli.py:52`) — it cannot `await`,
> so it only filters the in-memory prompt cache. `list_prompts` runs at **startup** (to fill
> that cache); it is *not* re-run on submit either — Flow B only calls `get_prompt`.

> 🔁 **Contrast with `@` (Flow B there):** an `@`-submit does **two** live resource reads
> (`list_docs` + `fetch_doc`). A `/`-submit does **one** live prompt render (`get_prompt`)
> and *no* resource reads — the doc id is just interpolated into the template as a string;
> the document content is fetched later, by the model, via the `read_doc_contents` tool. 🟡

---

## Flow A — autocomplete (typing `/`, no Enter)

### Precondition: the prompt list is cached once at startup
```
main.py → await cli.initialize()                       main.py:58  (def core/cli.py:179)
  └─ await self.refresh_prompts()                      cli.py:181  (def cli.py:190)
       self.prompts = await agent.list_prompts()       cli.py:192
                       └─ doc_client.list_prompts()    cli_chat.py:22
                            └─ session().list_prompts() → server  ← the ONLY list_prompts call
       self.completer.update_prompts(self.prompts)     cli.py:193   ← list cached in memory
       self.command_autosuggester = CommandAutoSuggest(self.prompts)  cli.py:194  ← ghost-hint cache
```
This is the single moment `list_prompts` runs. It is **not** refreshed again during the
session (`refresh_prompts` is only called from `initialize`, `cli.py:181`).

🟢 Probe of the live server — what gets cached:
```
list_prompts() -> [ Prompt(name='format',
                           description='Rewrites the contents of the document in Markdown format.',
                           arguments=[ PromptArgument(name='doc_id',
                                                      description='Id of the document to format',
                                                      required=True) ]) ]
```

### On each keystroke — NO server call. Three completion sub-stages + one ghost hint.

```
you press "/"  (empty buffer)                          key binding  cli.py:125
  buffer.insert_text("/"); buffer.start_completion()         cli.py:129-130
        │
        ▼
UnifiedCompleter.get_completions(document)             cli.py:52   ← SYNC (cannot await)
  text.startswith("/")                                 cli.py:70
        │
        ├─(A1) len(parts) <= 1 and not endswith(" ")   cli.py:73    e.g.  "/fo"
        │        for prompt in self.prompts:           cli.py:76    ← reads CACHED prompts
        │           if prompt.name.startswith(cmd_prefix):  cli.py:77
        │              yield Completion("/format", meta=description)  cli.py:78-83
        │
        ├─(A2) len(parts) == 1 and endswith(" ")       cli.py:86    e.g.  "/format "
        │        if cmd in self.prompt_dict:           cli.py:89
        │           for id in self.resources:          cli.py:90    ← reads CACHED resources (list[str])
        │              yield Completion(id)             cli.py:91-95   ← lists ALL doc ids
        │
        └─(A3) len(parts) >= 2                          cli.py:98    e.g.  "/format pl"
                 for resource in self.resources:        cli.py:101
                    if "id" in resource and resource["id"]…  cli.py:102  ← 🐛 BUG (see below)
                       yield Completion(resource["id"]) cli.py:105-109
```

Separately, a **ghost-text hint** (auto-suggest, not the dropdown) shows the argument name
after a fully-typed command:
```
CommandAutoSuggest.get_suggestion(buffer, document)    cli.py:19
  text.startswith("/")  and  len(parts) == 1           cli.py:24,29
  if cmd in self.prompt_dict:                           cli.py:32
     return Suggestion(" " + prompt.arguments[0].name)  cli.py:34   → ghost " doc_id"
```

The **space key binding** proactively re-opens the dropdown so A2/A3 fire without a manual trigger:
```
@self.kb.add(" ")                                       cli.py:141
  if text.startswith("/"):                              cli.py:148
     len(parts)==1 → start_completion()                 cli.py:151-152   (→ A2 lists docs)
     len(parts)==2 and arg looks like doc/file/id → start_completion()  cli.py:153-160
```

### Key code
```python
# core/cli.py:52 — SYNCHRONOUS completer: filters cached lists, no server I/O
def get_completions(self, document, complete_event):
    text = document.text
    ...
    if text.startswith("/"):
        parts = text[1:].split()
        if len(parts) <= 1 and not text.endswith(" "):        # A1: complete the command name
            cmd_prefix = parts[0] if parts else ""
            for prompt in self.prompts:                       # <- cached at startup
                if prompt.name.startswith(cmd_prefix):
                    yield Completion(prompt.name, start_position=-len(cmd_prefix),
                                     display=f"/{prompt.name}", display_meta=prompt.description or "")
            return
        if len(parts) == 1 and text.endswith(" "):            # A2: list every doc id
            cmd = parts[0]
            if cmd in self.prompt_dict:
                for id in self.resources:                     # self.resources is list[str]
                    yield Completion(id, start_position=0, display=id)
            return
        if len(parts) >= 2:                                   # A3: filter doc ids by prefix
            doc_prefix = parts[-1]
            for resource in self.resources:                   # 🐛 each `resource` is a STRING
                if "id" in resource and resource["id"].lower().startswith(doc_prefix.lower()):
                    yield Completion(resource["id"], ...)     #    never reached; dict access on a str
            return

# core/cli.py:190 — the cache filler (called once, from initialize())
async def refresh_prompts(self):
    self.prompts = await self.agent.list_prompts()            # -> list_prompts -> server prompts/list
    self.completer.update_prompts(self.prompts)
    self.command_autosuggester = CommandAutoSuggest(self.prompts)
    self.session.auto_suggest = self.command_autosuggester
```

**Takeaway:** Flow A = "show cached command names, then cached doc ids, filtered by what you
typed." The only server contact (`list_prompts`) already happened at startup. **A1 and A2
work; A3 is broken** (🐛 below) — so typing a *partial* doc name after the command shows
nothing.

---

## Flow B — render + agent loop (`/format plan.md` + Enter)

This is the runtime path. Unlike `@` (which injects document *content*), `/` injects a
**rendered prompt template** — the doc id is interpolated as a *string*; the content is not
read here.

```
you submit  "/format plan.md"  ⏎
        │
        ▼
CliApp.run()                                   core/cli.py:199
  user_input = await session.prompt_async("> ")     cli.py:202
  response   = await self.agent.run(user_input)      cli.py:206
        │
        ▼
Chat.run(query)                                 core/chat.py:16
  await self._process_query(query)                  chat.py:22   ← overridden by CliChat
        │
        ▼   ┌──────────── phase 1: command → rendered messages ─────────────┐
CliChat._process_query(query)                   core/cli_chat.py:65
  if await self._process_command(query):  return    cli_chat.py:66   ← True for "/" → SKIP _extract_resources
        │
        ▼
CliChat._process_command(query)                 core/cli_chat.py:51
  if not query.startswith("/"): return False        cli_chat.py:52   → "/" so continue
  words   = query.split()                            cli_chat.py:55   → ["/format", "plan.md"]
  command = words[0].replace("/", "")                cli_chat.py:56   → "format"   (🐛 strips ALL "/")
  messages = await self.doc_client.get_prompt(       cli_chat.py:58
                 command, {"doc_id": words[1]})      cli_chat.py:59   (🐛 words[1] / hardcoded "doc_id")
        │            │  LIVE get_prompt
        │            ▼
        │   MCPClient.get_prompt(name, args)         mcp_client.py:79
        │     result = await session().get_prompt(name, args)   mcp_client.py:80
        │     return result.messages                            mcp_client.py:81
        │            │   JSON-RPC  "prompts/get"  (stdio, one JSON line)
        │            ▼
        │   ┌──────────── MCP server subprocess (mcp_server.py) ────────────┐
        │   │ @mcp.prompt(name="format")  format_document(doc_id)  :60-66    │
        │   │   SDK render(): validate required args  (base.py:144-149)      │
        │   │   interpolate {doc_id} into template :67-77                    │
        │   │   return [base.UserMessage(prompt)]                 :79        │
        │   └────────────────────────────────────────────────────────────────┘
        │            │  -> [PromptMessage(role='user', content=TextContent(text="…plan.md…"))]
        │            ▼
  self.messages += convert_prompt_messages_to_message_params(messages)  cli_chat.py:62
        │            └─ TextContent → {"role":"user","content": "<the rendered text>"}  cli_chat.py:92-135
  return True                                        cli_chat.py:63
        │
        ▼   ┌──────────────────── phase 2: agent loop ────────────────────┐
Chat.run()  while True:                          chat.py:24
   response = claude_service.chat(messages, tools=get_all_tools(clients))  chat.py:25-28
            ↑ the rendered prompt told the model to use 'edit_document'; it will also
              call 'read_doc_contents' to load plan.md (content was NEVER injected here) 🟡
   if response.stop_reason == "tool_use":  execute tool, loop   chat.py:32-40
   else:                                   take final text, break chat.py:41-45
        │
        ▼
CliApp.run():  print(f"\nResponse:\n{response}")  cli.py:207
```

### Key functions
```python
# core/cli_chat.py:51 — "/" branch: render the prompt and splice it into the conversation
async def _process_command(self, query: str) -> bool:
    if not query.startswith("/"):
        return False
    words = query.split()
    command = words[0].replace("/", "")                 # 🐛 replaces EVERY "/", not just the leading one
    messages = await self.doc_client.get_prompt(
        command, {"doc_id": words[1]}                   # 🐛 IndexError if no arg; 🐛 arg name hardcoded
    )
    self.messages += convert_prompt_messages_to_message_params(messages)
    return True

# mcp_client.py:75 / :79 — thin client wrappers
async def list_prompts(self) -> list[types.Prompt]:
    result = await self.session().list_prompts()        # JSON-RPC prompts/list
    return result.prompts
async def get_prompt(self, prompt_name: str, args: dict[str, str]):
    result = await self.session().get_prompt(prompt_name, args)   # JSON-RPC prompts/get
    return result.messages

# mcp_server.py:60 — the server-side prompt ({doc_id} interpolated into the template text)
@mcp.prompt(name="format", description="Rewrites the contents of the document in Markdown format.")
def format_document(doc_id: str = Field(description="Id of the document to format")) -> list[base.Message]:
    prompt = f"""... The id of the document you need to reformat is:
    <document_id>
    {doc_id}
    </document_id>
    ... Use the 'edit_document' tool to edit the document. ..."""
    return [base.UserMessage(prompt)]
```

🟢 Probe — the exact render and the SDK-level argument validation:
```
get_prompt('format', {'doc_id':'plan.md'})
  -> [PromptMessage(role='user', content=TextContent(type='text', text='… plan.md …'))]
get_prompt('format', {})                       # missing required arg
  -> McpError: Missing required arguments: {'doc_id'}
     # SDK validates at prompts/base.py:144-149 (raises ValueError server-side; client surfaces it as McpError)
```

### Why no document content appears in Flow B (and why the model still reads the file)
With `@`, phase 1 injects the **whole document** (`fetch_doc`). With `/`, phase 1 injects
only the **template** — `format_document` interpolates the *id string* `plan.md`, never the
content (🟢 `mcp_server.py:67-77`). The template then *instructs* the model to use
`edit_document` (🟢 `mcp_server.py:76`), and since tools are re-sent every loop iteration
(🟢 `chat.py:27`), the model will typically call `read_doc_contents` first to load the file
(🟡 model decision). So: `/` = "inject instructions, let the model fetch & act"; `@` =
"inject content, answer directly."

---

## Are there problems with the prompt impl? — yes, but **not** in `mcp_server.py` / `mcp_client.py`

Short answer: the **server prompt** (`mcp_server.py`) and the **client SDK wrappers**
(`mcp_client.py`) are correct and verified working. The real bugs live in the **CLI glue**
(`core/cli_chat.py`, `core/cli.py`) — the orchestration *around* prompts.

### ✅ `mcp_server.py` — correct
- `format` registers cleanly; `list_prompts` exposes it with the right `arguments` and
  `required=True`, and `Field(description=…)` as the default is correctly picked up as the
  argument description (🟢 probe; SDK reads it in `prompts/base.py:113-122`). Using `Field`
  as a default does **not** make the arg optional — it stays required. 🟢
- `get_prompt` renders and interpolates `doc_id` correctly (🟢 probe).
- *Nit (not a bug):* the template text ends mid-sentence — "After the document has been
  reformatted…" (`mcp_server.py:76`). Polish, not correctness. The `summarize` prompt is an
  intentional `TODO` (`mcp_server.py:83`).

### ✅ `mcp_client.py` — correct
- `list_prompts` (`:75-77`) and `get_prompt` (`:79-81`) match the SDK reference exactly and
  return the right shapes (🟢 probe). No bug. (They have no `try/except`, so a server-side
  `McpError` propagates — see Bug ❶/❹ for where that hurts.)

### 🐛 Bugs in the CLI glue

| # | Severity | Location | Symptom (🟢 reproduced in-session) |
|---|---|---|---|
| ❶ | **High** | `cli_chat.py:59` `{"doc_id": words[1]}` | `/format` with **no argument** → `IndexError: list index out of range`, *before any server call*. It propagates through `Chat.run` → `CliApp.run`, whose loop catches only `KeyboardInterrupt` (`cli.py:209`) → **the whole CLI crashes.** |
| ❷ | **High** | `cli_chat.py:51-63` (no validation) | Unknown command or bad id, e.g. `/bogus x`, calls `get_prompt("bogus", …)` → server raises `McpError`, again **uncaught** → CLI crashes (same path as ❶). |
| ❸ | **Med** | `cli.py:98-110` (A3 branch) | `self.resources` is `list[str]`, but A3 does `if "id" in resource and resource["id"]…` — a *substring* test that is always `False` for these ids, so it short-circuits and `resource["id"]` (dict access on a `str`, a latent `TypeError`) is never even reached. So once you type a partial doc name (`/format pl`) the dropdown goes **empty**. A2 (and the `@` handler) treat resources as strings; A3 treats them as dicts — inconsistent (A1 doesn't touch resources — it iterates `self.prompts`). 🟢 |
| ❹ | **Med** | `cli_chat.py:59` hardcoded `"doc_id"` | The argument name is hardwired. Any prompt whose argument isn't named `doc_id` (or that needs >1 arg) silently sends the wrong key. Works for `format` (and a `doc_id`-shaped `summarize`) only. |
| ❺ | **Low** | `cli_chat.py:56` `words[0].replace("/", "")` | Strips **every** `/`, not just the leading one (`/fo/rmat` → `format`). Harmless for normal commands; use `words[0][1:]` or `removeprefix("/")`. 🟢 |

**Minimal fix for the high-severity ones** (guard arity + map args by the prompt's declared
argument name + wrap the render):
```python
async def _process_command(self, query: str) -> bool:
    if not query.startswith("/"):
        return False
    words = query.split()
    command = words[0].removeprefix("/")
    # look up the prompt's real first-argument name instead of hardcoding "doc_id"
    prompt = next((p for p in await self.list_prompts() if p.name == command), None)
    if prompt is None:
        print(f"Unknown command: /{command}")
        return True
    if prompt.arguments and len(words) < 2:
        print(f"Usage: /{command} <{prompt.arguments[0].name}>")
        return True
    arg_name = prompt.arguments[0].name if prompt.arguments else None
    args = {arg_name: words[1]} if arg_name else {}
    messages = await self.doc_client.get_prompt(command, args)
    self.messages += convert_prompt_messages_to_message_params(messages)
    return True
```
And fix A3 in `cli.py:98-110` to treat `self.resources` as the `list[str]` it is:
```python
if len(parts) >= 2:
    doc_prefix = parts[-1]
    for resource_id in self.resources:                 # strings, like A2
        if resource_id.lower().startswith(doc_prefix.lower()):
            yield Completion(resource_id, start_position=-len(doc_prefix), display=resource_id)
    return
```

---

## ✅ Fixes applied (this session)

The bug list above is the **original** state (kept for tracing). What actually changed:

### ❶ ❹ ❺ — `_process_command` rewritten (`core/cli_chat.py:51`)
- **❺** `words[0].replace("/", "")` → `removeprefix("/")` — strips only the *leading*
  slash, so `/fo/rmat` no longer collapses to `format` and mask a typo.
- **❹** No more hardcoded `"doc_id"`: look the prompt up via `self.list_prompts()`, then
  map the positional words onto the prompt's **declared** argument names
  (`prompt.arguments[i].name`). Generic for any prompt / arg name / arg count.
- **❶** No more `words[1]` IndexError. When a **required** arg is missing we *skip*
  `get_prompt` and append a synthesized user message asking the model to prompt the user
  for it (`/format` → model replies "which document?"). This keeps `self.messages`
  non-empty (no empty-messages 400) and resolves it conversationally instead of crashing.

### ❷ — unknown command no longer crashes (3 small changes)
- **`core/cli_chat.py`** — once the lookup gives `prompt is None` we already know it
  isn't a real command, so we **don't call the server**: just
  `print(f"Unknown command: /{command}")` and queue nothing.
- **`core/chat.py`** (`Chat.run`) — if a turn queues **no new message**, return before
  the agent loop → no model call (this also permanently closes the empty-messages 400
  risk class).
- **`core/cli.py`** (`CliApp.run`) — a **narrow safety net**: `except (McpError, APIError)`
  → print and continue the REPL, so any *other* runtime error (server hiccup, API 400)
  no longer kills the CLI; truly unexpected exceptions still surface as a traceback. Plus
  `if response:` to drop the empty `Response:` line.

> Correction to ❷: a **bad doc id** (`/format nope.md`) does **not** crash — the server
> prompt just interpolates the string (no `docs` lookup). ❷ was only ever the
> **unknown command** case.

### Still open
- **❸** (Flow-A A3 completer) — unchanged; the partial-doc-name dropdown is still empty.

🟢 Verified in-session (headless): `/bogus` → prints `Unknown command: /bogus`, no model
call, REPL stays alive; `/format` → model asks for the id; `/format plan.md` → renders &
edits as before.

---

## Sources (🟢 repo `file:line`)

- Flow A: `core/cli.py:19,24,29,32,34,52,70,73,76-83,86,89,90-95,98,101,102,105-109,125,129-130,141,148,151-160,179,181,190,192,193,194` ; `core/cli_chat.py:21,22`
- Flow B: `core/cli.py:199,202,206,207` ; `core/chat.py:16,22,24,25-28,32-45` ;
  `core/cli_chat.py:51,52,55,56,58,59,62,63,65,66,92-135` ; `mcp_client.py:75-77,79-81` ;
  `mcp_server.py:60-66,67-77,79,83`
- SDK behavior: `.venv/.../mcp/server/fastmcp/prompts/base.py:113-122,144-149` ; in-session probe of `list_prompts` / `get_prompt`

# The `@mention` document-resource flows

Typing `@` triggers **two different flows**, and they hit the MCP server differently.
This doc separates them and traces each function-by-function, with `file:line`.

> **Grounding legend**
> - 🟢 **Verified** — checked against repo source (file:line) or the mcp SDK in `.venv/`,
>   or observed from a headless probe in-session.
> - 🟡 **Behavioral** — depends on the model's runtime decision, not fixed by code.

## The two flows at a glance

| | **Flow A — autocomplete** | **Flow B — injection + agent loop** |
|---|---|---|
| Trigger | you type `@` (NO Enter) | `@file.ext` then **Enter** |
| What you see | dropdown of doc ids | the model's answer |
| `list_docs` (`docs://documents`) | **not called here** — reads a cached list filled **once at startup** | **called live** each submit |
| `fetch_doc` (`docs://documents/{id}`) | never involved | **called live** for each mentioned doc |
| Server round-trip on the keystroke/submit? | **No** (local cache) | **Yes** (`resources/read` ×N) |

> ⚠️ **Common misconception:** "typing `@` calls `list_docs`." It does **not**.
> `UnifiedCompleter.get_completions` is a **synchronous** method (`cli.py:52`) — it
> cannot `await`, so it physically cannot call the async resource. It only filters the
> in-memory cache. `list_docs` runs at **startup** (to fill that cache) and again on
> **submit** (Flow B) — never on the `@` keystroke itself.

---

## Flow A — autocomplete (typing `@`, no Enter)

### Precondition: the cache is filled once at startup
```
main.py → await cli.initialize()                       main.py:58  (def core/cli.py:179)
  └─ await self.refresh_resources()                    cli.py:180  (def cli.py:183)
       self.resources = await agent.list_docs_ids()    cli.py:185
                          └─ read_resource("docs://documents")  → server list_docs   ← the ONLY list_docs call in Flow A
       self.completer.update_resources(self.resources) cli.py:186   ← list cached in memory
```
This is the single moment `list_docs` runs for autocomplete. It is **not** refreshed
again during the session (`refresh_resources` is only called from `initialize`).

### On each `@` keystroke — NO server call
```
you press "@"                                           key binding  cli.py:134
  buffer.insert_text("@"); buffer.start_completion()         cli.py:137-139
        │
        ▼
UnifiedCompleter.get_completions(document)              cli.py:52   ← SYNC (cannot await)
  if "@" in text_before_cursor:                         cli.py:56
     prefix = text after the last "@"                   cli.py:58
     for resource_id in self.resources:                 cli.py:60   ← reads the CACHED list
        if resource_id.lower().startswith(prefix):      cli.py:61
           yield Completion(resource_id, ...)           cli.py:62   ← shown in the dropdown
```

### Key code
```python
# core/cli.py:134 — the "@" key binding just opens the completion menu
@self.kb.add("@")
def _(event):
    buffer = event.app.current_buffer
    buffer.insert_text("@")
    if buffer.document.is_cursor_at_the_end:
        buffer.start_completion(select_first=False)

# core/cli.py:52 — SYNCHRONOUS completer: filters the cached list, no server I/O
def get_completions(self, document, complete_event):
    text_before_cursor = document.text_before_cursor
    if "@" in text_before_cursor:
        prefix = text_before_cursor[text_before_cursor.rfind("@") + 1 :]
        for resource_id in self.resources:                 # <- cached at startup
            if resource_id.lower().startswith(prefix.lower()):
                yield Completion(resource_id, start_position=-len(prefix),
                                 display=resource_id, display_meta="Resource")
        return

# core/cli.py:183 — the cache filler (called once, from initialize())
async def refresh_resources(self):
    self.resources = await self.agent.list_docs_ids()      # -> read_resource("docs://documents") -> list_docs
    self.completer.update_resources(self.resources)
```

**Takeaway:** Flow A = "show cached ids, filtered by what you typed after `@`." The only
server contact (`list_docs`) already happened at startup.

---

## Flow B — injection + agent loop (`@file.ext` + Enter)

This is the runtime path. It has two sub-phases: **resource injection** (pull the doc
content into `messages`), then the **agent loop** (model replies, may call tools).

```
you submit  "@deposition.md"  ⏎
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
        ▼   ┌──────────────── phase 1: resource injection ────────────────┐
CliChat._process_query(query)                   core/cli_chat.py:65
  ├─ await self._process_command(query)             cli_chat.py:66 → False (not "/")
  ├─ added_resources = await self._extract_resources(query)   cli_chat.py:69
  └─ self.messages.append({... <context>{added_resources}</context> ...})  cli_chat.py:89  (after extract)
        │
        ▼
CliChat._extract_resources(query)               cli_chat.py:35
  mentions = [w[1:] ... startswith("@")]            cli_chat.py:36 → ["deposition.md"]
  doc_ids  = await self.list_docs_ids()             cli_chat.py:38   (a) LIVE list_docs
  for doc_id in doc_ids:                            cli_chat.py:41
     if doc_id in mentions:
        content = await self.get_doc_content(doc_id) cli_chat.py:43   (b) LIVE fetch_doc
        │  (a)                                        │  (b)
        ▼                                             ▼
list_docs_ids() cli_chat.py:24            get_doc_content(id) cli_chat.py:27
 read_resource("docs://documents")         read_resource("docs://documents/{id}")
        └───────────────────┬──────────────────────┘
                            ▼
        MCPClient.read_resource(uri)            mcp_client.py:83
          await self.session().read_resource(AnyUrl(uri))
                            │   JSON-RPC  "resources/read"  (stdio, one JSON line)
                            ▼
        ┌──────────── MCP server subprocess (mcp_server.py) ────────────┐
        │ @mcp.resource("docs://documents")      list_docs()  :46 → list(docs.keys())  [JSON]
        │ @mcp.resource("docs://documents/{id}") fetch_doc(id):51 → docs[id]           [text]
        └────────────────────────────────────────────────────────────────┘
                            │
                            ▼   the FULL document text is now embedded in self.messages
        ┌──────────────────── phase 2: agent loop ────────────────────┐
Chat.run()  while True:                          chat.py:24
   response = claude_service.chat(messages, tools=get_all_tools(clients))  chat.py:25-28
            ↑ every iteration re-sends tools (incl. read_doc_contents) — model MAY call it
   if response.stop_reason == "tool_use":  execute tool, loop   chat.py:32-40
   else:                                   take final text, break chat.py:41-45
                            │
                            ▼
CliApp.run():  print(f"\nResponse:\n{response}")  cli.py:207
```

### Key functions
```python
# core/cli_chat.py:35 — "@" is not "/", so _process_command returns False; we extract resources
async def _extract_resources(self, query: str) -> str:
    mentions = [word[1:] for word in query.split() if word.startswith("@")]  # "@deposition.md" -> "deposition.md"
    doc_ids = await self.list_docs_ids()           # (a) LIVE list_docs — all valid ids
    mentioned_docs: list[Tuple[str, str]] = []
    for doc_id in doc_ids:
        if doc_id in mentions:                      # only real ids get injected
            content = await self.get_doc_content(doc_id)   # (b) LIVE fetch_doc
            mentioned_docs.append((doc_id, content))
    return "".join(
        f'\n<document id="{doc_id}">\n{content}\n</document>\n'
        for doc_id, content in mentioned_docs
    )

# core/cli_chat.py:24 / :27 — thin wrappers (URI only)
async def list_docs_ids(self) -> list[str]:
    return await self.doc_client.read_resource("docs://documents")           # -> ["deposition.md", ...]
async def get_doc_content(self, doc_id: str) -> str:
    return await self.doc_client.read_resource(f"docs://documents/{doc_id}")  # -> "This deposition..."

# mcp_client.py:83 — client-side read (await + parse)
async def read_resource(self, uri: str) -> Any:
    result = await self.session().read_resource(AnyUrl(uri))   # JSON-RPC resources/read
    resource = result.contents[0]
    if isinstance(resource, types.TextResourceContents):
        if resource.mimeType == "application/json":
            return json.loads(resource.text)   # docs://documents      -> list[str]
        return resource.text                    # docs://documents/{id} -> str
    return resource

# mcp_server.py:46 / :51 — server resources ({doc_id} binds to the parameter)
@mcp.resource("docs://documents", mime_type="application/json")
def list_docs() -> list[str]:
    return list(docs.keys())

@mcp.resource("docs://documents/{doc_id}", mime_type="text/plain")
def fetch_doc(doc_id: str) -> str:
    if doc_id not in docs:
        raise ValueError(f"Doc with id {doc_id} not found")
    return docs[doc_id]      # the WHOLE content — no truncation
```

The injected user message is a template (`cli_chat.py:71-87`) that embeds the query and
the `<document>` block, and explicitly tells the model not to re-read (`cli_chat.py:84`).

### Q: the content is already in context — why might the model still call `read_doc_contents`?
A common intuition is "`@` injects a top-K preview, the tool reads the full file."
**Not in this codebase.** Verified:
1. **`@` injects the whole doc — no truncation.** `fetch_doc` → `return docs[doc_id]` (🟢 `mcp_server.py:51-55`).
2. **The `read_doc_contents` tool returns the same thing.** `read_document` → `return docs[doc_id]` (🟢 `mcp_server.py:16-26`). Tool and resource return **identical** content.
3. **The prompt discourages re-reading** (🟢 `cli_chat.py:84`).
4. **But tools are re-sent every loop iteration** (🟢 `chat.py:27`), so the model *can* call `read_doc_contents` and may choose to (🟡 model decision) — redundantly re-fetching the same sentence.

**Conclusion:** not a top-K issue; it's a redundant model call of identical content
(harmless here since the docs are one sentence). For *large* docs, the preview-vs-full
split you imagined is a legitimate design — this teaching project just doesn't do it.

---

## Sources (🟢 repo file:line)

- Flow A: `core/cli.py:52,56,58,60-62,134-139,179,180,183,185,186` ; `core/cli_chat.py:24`
- Flow B: `core/cli.py:199,202,206,207` ; `core/chat.py:16,22,24,25-28,32-45` ;
  `core/cli_chat.py:27,35,36,38,41,43,65,66,69,84,89` ; `mcp_client.py:83` ;
  `mcp_server.py:16-26,46,51`

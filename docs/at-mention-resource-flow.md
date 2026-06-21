# The `@mention` document-resource flow (end-to-end)

What actually happens, function by function, when you type `@deposition.md` in the
chat — from keypress to model response — and why the model may still call the
`read_doc_contents` tool even though the document is already in context.

> **Grounding legend**
> - 🟢 **Verified** — checked against repo source in this workspace (file:line) or the
>   mcp SDK under `.venv/`, or observed from a headless probe in-session.
> - 🟡 **Behavioral** — depends on the model's runtime decision, not fixed by code.

The flow has **two phases**: first **resource injection** (the document content is
pulled in and embedded into `messages`), then the **agent loop** (the model generates a
reply and *may* call tools).

---

## Flow diagram

```
you type  "@deposition.md"  ⏎
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
  └─ self.messages.append({... <context>{added_resources}</context> ...})  cli_chat.py:89
        │
        ▼
CliChat._extract_resources(query)               cli_chat.py:35
  mentions = [w[1:] ... startswith("@")]            cli_chat.py:36 → ["deposition.md"]
  doc_ids  = await self.list_docs_ids()             cli_chat.py:38   (a)
  for doc_id in doc_ids:                            cli_chat.py:41
     if doc_id in mentions:
        content = await self.get_doc_content(doc_id) cli_chat.py:43   (b)
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

> Side note: the `@` **autocomplete suggestions while you type** take a different path —
> `CliApp.initialize()` calls `refresh_resources()` → `list_docs_ids()` → the same
> `read_resource("docs://documents")` at startup, feeding the id list to the completer.
> The flow above is what runs *after* you press Enter.

---

## Step-by-step (key functions shown in full)

### 1. Entry — `@` is not `/`, so `_process_command` returns False and we go to `_extract_resources`
```python
# core/cli_chat.py:35
async def _extract_resources(self, query: str) -> str:
    mentions = [word[1:] for word in query.split() if word.startswith("@")]  # "@deposition.md" -> "deposition.md"

    doc_ids = await self.list_docs_ids()           # all valid ids on the server
    mentioned_docs: list[Tuple[str, str]] = []

    for doc_id in doc_ids:
        if doc_id in mentions:                      # only real ids get injected
            content = await self.get_doc_content(doc_id)
            mentioned_docs.append((doc_id, content))

    return "".join(                                 # build <document>…</document> blocks
        f'\n<document id="{doc_id}">\n{content}\n</document>\n'
        for doc_id, content in mentioned_docs
    )
```

### 2. Thin wrappers — they only build the URI; the work is in `read_resource`
```python
# core/cli_chat.py:24
async def list_docs_ids(self) -> list[str]:
    return await self.doc_client.read_resource("docs://documents")           # -> ["deposition.md", ...]

# core/cli_chat.py:27
async def get_doc_content(self, doc_id: str) -> str:
    return await self.doc_client.read_resource(f"docs://documents/{doc_id}")  # -> "This deposition..."
```

### 3. Client-side resource read (the wrapper we fixed)
```python
# mcp_client.py:83
async def read_resource(self, uri: str) -> Any:
    result = await self.session().read_resource(AnyUrl(uri))   # JSON-RPC resources/read
    resource = result.contents[0]

    if isinstance(resource, types.TextResourceContents):
        if resource.mimeType == "application/json":
            return json.loads(resource.text)   # docs://documents      -> list[str]
        return resource.text                    # docs://documents/{id} -> str
    return resource
```

### 4. Server-side resources (`{doc_id}` in the URI template binds to the parameter)
```python
# mcp_server.py:46
@mcp.resource("docs://documents", mime_type="application/json")
def list_docs() -> list[str]:
    return list(docs.keys())

# mcp_server.py:51
@mcp.resource("docs://documents/{doc_id}", mime_type="text/plain")
def fetch_doc(doc_id: str) -> str:
    if doc_id not in docs:
        raise ValueError(f"Doc with id {doc_id} not found")
    return docs[doc_id]      # returns the WHOLE content — no truncation
```

### 5. The injected prompt (built in `_process_query`)
The user message that finally enters `messages` is a template (`cli_chat.py:71-87`) that
embeds the raw query and the `<document>` block, and explicitly tells the model not to
re-read (`cli_chat.py:84`):
> *"If the document content is included in this prompt, you don't need to use an
> additional tool to read the document."*

---

## Q: the content is already in context — why does the model still call `read_doc_contents`?

A reasonable intuition is "`@` injects a top-K preview, the tool reads the full file."
**This codebase does not work that way.** Verified:

1. **`@` injects the whole document — no top-K, no truncation.** `get_doc_content` →
   `fetch_doc` does `return docs[doc_id]` (🟢 `mcp_server.py:51-55`).
2. **The `read_doc_contents` tool returns the same thing.** Its handler `read_document`
   also does `return docs[doc_id]` (🟢 `mcp_server.py:16-26`). So **tool and resource
   return identical content** — there is no preview-vs-full distinction here.
3. **The prompt explicitly discourages re-reading** (🟢 `cli_chat.py:84`, quoted above).
4. **But the tools are re-sent every loop iteration** (🟢 `chat.py:27`,
   `get_all_tools` inside `while True`), so the model is always *able* to call
   `read_doc_contents` and may choose to (🟡 model's runtime decision). When it does, it
   just fetches the same one sentence again — redundant, but harmless here.

**Conclusion:** it's not a top-K issue. `@` already supplied the full text;
`read_doc_contents` is the model redundantly re-fetching identical content. Because the
sample docs are one sentence each, it doesn't matter.

> Real-world note: the design you imagined is legitimate for *large* documents — e.g.
> inject only a summary / first-K lines on `@` to save tokens and let the model call a
> tool for the full read; or, conversely, *don't* expose `read_doc_contents` on a turn
> where the content was already injected, to avoid the redundant call. This teaching
> project does neither (the docs are trivially small).

---

## Sources (🟢 repo file:line)

- `core/cli.py:199,202,206,207` — REPL loop / input / `agent.run` / print
- `core/chat.py:16,22,24,25-28,32-45` — `run`, `_process_query` call, agent loop, tool branch
- `core/cli_chat.py:24,27,35,36,38,41,43,65,66,69,84,89` — wrappers, `_extract_resources`, `_process_query`, prompt
- `mcp_client.py:83` — `read_resource`
- `mcp_server.py:16-26,46,51` — `read_doc_contents` tool, `list_docs` / `fetch_doc` resources

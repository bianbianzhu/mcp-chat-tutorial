# Bug & fix: `read_resource` and the `@mention` resource flow

Why `@deposition.md`-style document mentions raised
`TypeError: 'coroutine' object is not iterable`, and the client + server changes
needed to make resource reading work end-to-end.

> **Grounding legend**
> - 🟢 **Verified** — checked against source in this workspace (repo files or the
>   mcp SDK under `.venv/`), or observed from a headless probe run in-session.
> - 🟡 **Explanation** — reasoning, not separately proven.

---

## Symptom

```
File ".../core/cli_chat.py", line 41, in _extract_resources
    for doc_id in doc_ids:
TypeError: 'coroutine' object is not iterable
```

The initial `read_resource` was:
```python
async def read_resource(self, uri: str) -> Any:
    result = self.session().read_resource(AnyUrl(uri))   # not awaited
    return result
```

---

## Three issues (one visible, two latent)

### 1. Missing `await` (the actual crash) 🟢
`ClientSession.read_resource` is a coroutine — `async def read_resource(self, uri:
AnyUrl) -> types.ReadResourceResult` (`mcp/client/session.py:335`). Without `await`,
the wrapper returns the **inner coroutine object**, so:
`read_resource()` → coroutine → `list_docs_ids()` (`cli_chat.py:25`) returns it →
`doc_ids` is a coroutine → `for doc_id in doc_ids` (`cli_chat.py:41`) → not iterable.

### 2. The awaited result is a `ReadResourceResult`, not a `list` / `str` 🟢
Even once awaited, you get a result object, not usable data
(`mcp/types.py:888-891`):
```python
class ReadResourceResult(Result):
    contents: list[TextResourceContents | BlobResourceContents]
```
`TextResourceContents.text: str` (`types.py:871`), plus inherited `.uri` / `.mimeType`.
The two call sites expect different shapes:
- `read_resource("docs://documents")` → a **`list[str]`** (iterated at `cli_chat.py:41`)
- `read_resource("docs://documents/{id}")` → a **`str`** (used at `cli_chat.py:43`)

So the wrapper must read `result.contents[0]` and parse by `mimeType`.

### 3. The server resources don't exist yet 🟢
`mcp_server.py:46-47` were still TODO — no `docs://documents` resource was registered.
So even a corrected client would get an "unknown resource" error. **Both ends must be
implemented.**

---

## The fix

### Server (`mcp_server.py`) — add two resources
```python
@mcp.resource("docs://documents", mime_type="application/json")
def list_docs() -> list[str]:
    return list(docs.keys())


@mcp.resource("docs://documents/{doc_id}", mime_type="text/plain")
def fetch_doc(doc_id: str) -> str:
    if doc_id not in docs:
        raise ValueError(f"Doc with id {doc_id} not found")
    return docs[doc_id]
```
Why the `mime_type`s matter (🟢 `mcp/server/fastmcp/resources/types.py:55-71`):
`FunctionResource.read()` returns a `str` as-is, but **any non-`str` (e.g. a list) is
`pydantic_core.to_json(...)`-encoded**. So `list_docs` is sent as a JSON string —
declaring `mime_type="application/json"` lets the client know to parse it. `fetch_doc`
returns a plain `str`, so `text/plain` is correct and the client returns it verbatim.

### Client (`mcp_client.py`) — await + parse
```python
import json   # add to imports

async def read_resource(self, uri: str) -> Any:
    result = await self.session().read_resource(AnyUrl(uri))
    resource = result.contents[0]

    if isinstance(resource, types.TextResourceContents):
        if resource.mimeType == "application/json":
            return json.loads(resource.text)  # docs://documents -> list[str]
        return resource.text                  # docs://documents/{id} -> str
    return resource  # BlobResourceContents (binary) — returned as-is
```
(`types` and `AnyUrl` are already imported.) Accessing `.text` only inside the
`TextResourceContents` guard keeps it type-safe — `BlobResourceContents` has `.blob`,
not `.text`.

---

## Official references

- **Authoritative — SDK source in `.venv`** (version-matched):
  - `mcp/client/session.py:335` — `read_resource` is async → `ReadResourceResult`
  - `mcp/types.py:888-891` — `ReadResourceResult.contents: list[TextResourceContents | BlobResourceContents]`
  - `mcp/types.py:871` — `TextResourceContents.text`
  - `mcp/server/fastmcp/resources/types.py:55-71` — `str` vs JSON serialization rule
- **python-sdk README** (client usage: `await session.read_resource(AnyUrl(...))` →
  `.contents[0]` → `.text`): https://github.com/modelcontextprotocol/python-sdk#readme
  - ⚠️ The README snippet shows `types.TextContent`; for *resources* the correct type
    is `TextResourceContents` (per the SDK source above).
- **Client quickstart:** https://modelcontextprotocol.io/quickstart/client

---

## Verification

Applied to `mcp_server.py` (two resources) and `mcp_client.py` (`read_resource`), then
exercised end-to-end with a headless probe (🟢 in-session):
```
docs://documents      -> list ['deposition.md', 'report.pdf', 'financials.docx', 'outlook.pdf', 'plan.md', 'spec.txt']
docs://documents/{id} -> str 'This deposition covers the testimony of Angela Smith, P.E.'
tools                 -> ['read_doc_contents', 'edit_document']

ALL CHECKS PASSED
```
- `docs://documents` returns a parsed `list[str]` (JSON path) — iterable, so
  `cli_chat._extract_resources` (`cli_chat.py:41`) no longer crashes.
- `docs://documents/{id}` returns the document `str` (text path).

So `@mention` document resolution now works end-to-end.

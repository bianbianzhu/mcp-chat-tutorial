# Bug 与修复:`read_resource` 与 `@mention` 资源流程

为什么 `@deposition.md` 这类文档引用会抛
`TypeError: 'coroutine' object is not iterable`,以及要让资源读取端到端跑通,
client 与 server 两端各需要哪些改动。

> **grounding 说明**
> - 🟢 **已核实**:在本工作区源码中直接核对过(repo 文件,或 `.venv` 里的 mcp SDK),
>   或来自本次会话内的 headless 探针实测。
> - 🟡 **解释**:推理说明,未单独验证。

---

## 现象

```
File ".../core/cli_chat.py", line 41, in _extract_resources
    for doc_id in doc_ids:
TypeError: 'coroutine' object is not iterable
```

最初的 `read_resource` 写法:
```python
async def read_resource(self, uri: str) -> Any:
    result = self.session().read_resource(AnyUrl(uri))   # 没有 await
    return result
```

---

## 三个问题(一个显式、两个潜在)

### 1. 漏了 `await`(真正导致崩溃的原因) 🟢
`ClientSession.read_resource` 是协程 —— `async def read_resource(self, uri: AnyUrl)
-> types.ReadResourceResult`(`mcp/client/session.py:335`)。不 `await` 的话,封装方法
返回的是**内层 coroutine 对象**,于是:
`read_resource()` → coroutine → `list_docs_ids()`(`cli_chat.py:25`)把它返回出去 →
`doc_ids` 是一个 coroutine → `for doc_id in doc_ids`(`cli_chat.py:41`)→ 不可迭代。

### 2. `await` 之后得到的是 `ReadResourceResult`,不是 `list` / `str` 🟢
即便加了 `await`,拿到的也是结果对象,不能直接当数据用
(`mcp/types.py:888-891`):
```python
class ReadResourceResult(Result):
    contents: list[TextResourceContents | BlobResourceContents]
```
`TextResourceContents.text: str`(`types.py:871`),并继承 `.uri` / `.mimeType`。
两个调用点期望的形状不同:
- `read_resource("docs://documents")` → 需要 **`list[str]`**(在 `cli_chat.py:41` 被迭代)
- `read_resource("docs://documents/{id}")` → 需要 **`str`**(在 `cli_chat.py:43` 当内容用)

所以封装方法必须读取 `result.contents[0]`,并按 `mimeType` 解析。

### 3. server 端的 resource 还没实现 🟢
`mcp_server.py:46-47` 还是 TODO —— 根本没有注册 `docs://documents` 资源。
所以即使 client 改对了,`await` 出去也会得到「unknown resource」错误。**两端都要实现。**

---

## 修复

### Server(`mcp_server.py`)—— 新增两个 resource
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
为什么 `mime_type` 很关键(🟢 `mcp/server/fastmcp/resources/types.py:55-71`):
`FunctionResource.read()` 对 `str` 原样返回,但**任何非 `str`(例如 list)都会被
`pydantic_core.to_json(...)` 编码成 JSON 字符串**。所以 `list_docs` 发出去的是 JSON
字符串 —— 声明 `mime_type="application/json"` 才能让 client 知道要去解析它。
`fetch_doc` 返回纯 `str`,因此 `text/plain` 正确,client 原样返回。

### Client(`mcp_client.py`)—— await + 解析
```python
import json   # 加到导入处

async def read_resource(self, uri: str) -> Any:
    result = await self.session().read_resource(AnyUrl(uri))
    resource = result.contents[0]

    if isinstance(resource, types.TextResourceContents):
        if resource.mimeType == "application/json":
            return json.loads(resource.text)  # docs://documents -> list[str]
        return resource.text                  # docs://documents/{id} -> str
    return resource  # BlobResourceContents(二进制)—— 原样返回
```
(`types` 与 `AnyUrl` 都已导入。)只在 `TextResourceContents` 守卫内部访问 `.text` 才是
类型安全的 —— `BlobResourceContents` 只有 `.blob`,没有 `.text`。

---

## 官方 reference

- **权威来源 —— `.venv` 里的 SDK 源码**(版本一致):
  - `mcp/client/session.py:335` —— `read_resource` 是 async → `ReadResourceResult`
  - `mcp/types.py:888-891` —— `ReadResourceResult.contents: list[TextResourceContents | BlobResourceContents]`
  - `mcp/types.py:871` —— `TextResourceContents.text`
  - `mcp/server/fastmcp/resources/types.py:55-71` —— `str` vs JSON 的序列化规则
- **python-sdk README**(client 用法:`await session.read_resource(AnyUrl(...))` →
  `.contents[0]` → `.text`):https://github.com/modelcontextprotocol/python-sdk#readme
  - ⚠️ README 示例里写的是 `types.TextContent`;对 *resource* 而言正确类型是
    `TextResourceContents`(以上面 SDK 源码为准)。
- **Client quickstart:** https://modelcontextprotocol.io/quickstart/client

---

## 验证

已应用到 `mcp_server.py`(两个 resource)与 `mcp_client.py`(`read_resource`),
随后用 headless 探针端到端验证(🟢 本次会话实测):
```
docs://documents      -> list ['deposition.md', 'report.pdf', 'financials.docx', 'outlook.pdf', 'plan.md', 'spec.txt']
docs://documents/{id} -> str 'This deposition covers the testimony of Angela Smith, P.E.'
tools                 -> ['read_doc_contents', 'edit_document']

ALL CHECKS PASSED
```
- `docs://documents` 返回解析后的 `list[str]`(JSON 路径)—— 可迭代,
  因此 `cli_chat._extract_resources`(`cli_chat.py:41`)不再崩溃。
- `docs://documents/{id}` 返回文档 `str`(纯文本路径)。

至此 `@mention` 文档引用端到端打通。

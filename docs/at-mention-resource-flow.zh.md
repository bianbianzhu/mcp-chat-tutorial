# `@mention` 文档资源流程(端到端)

当你在 chat 里输入 `@deposition.md`,从按键到模型回复,**逐个函数**究竟发生了什么;
以及为什么文档内容已经在 context 里了,模型仍可能去调 `read_doc_contents` 这个 tool。

> **grounding 说明**
> - 🟢 **已核实**:在本工作区 repo 源码中按 file:line 核对过,或核对了 `.venv` 里的
>   mcp SDK,或来自本次会话的 headless 探针实测。
> - 🟡 **行为相关**:取决于模型的运行时决策,并非由代码固定。

整个流程分**两段**:先「**资源注入**」(把文档内容拉取并嵌入 `messages`),
再「**agent loop**」(模型生成回复,期间*可能*调 tool)。

---

## 流程图

```
你输入  "@deposition.md"  ⏎
        │
        ▼
CliApp.run()                                   core/cli.py:199
  user_input = await session.prompt_async("> ")     cli.py:202
  response   = await self.agent.run(user_input)      cli.py:206
        │
        ▼
Chat.run(query)                                 core/chat.py:16
  await self._process_query(query)                  chat.py:22   ← 被 CliChat 覆写
        │
        ▼   ┌──────────────── 第 1 段:资源注入 ────────────────┐
CliChat._process_query(query)                   core/cli_chat.py:65
  ├─ await self._process_command(query)             cli_chat.py:66 → False(不是 "/")
  ├─ added_resources = await self._extract_resources(query)   cli_chat.py:69
  └─ self.messages.append({... <context>{added_resources}</context> ...})  cli_chat.py:89 (invoke after)
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
                            │   JSON-RPC  "resources/read"  (stdio,一行 JSON)
                            ▼
        ┌──────────── MCP server 子进程 (mcp_server.py) ────────────┐
        │ @mcp.resource("docs://documents")      list_docs()  :46 → list(docs.keys())  [JSON]
        │ @mcp.resource("docs://documents/{id}") fetch_doc(id):51 → docs[id]           [text]
        └────────────────────────────────────────────────────────────┘
                            │
                            ▼   「整篇文档内容」现已嵌入 self.messages
        ┌──────────────────── 第 2 段:agent loop ────────────────────┐
Chat.run()  while True:                          chat.py:24
   response = claude_service.chat(messages, tools=get_all_tools(clients))  chat.py:25-28
            ↑ 每轮都重新把 tools(含 read_doc_contents)发给模型 —— 模型"可能"调它
   if response.stop_reason == "tool_use":  执行 tool 后继续循环   chat.py:32-40
   else:                                   取最终文本, break       chat.py:41-45
                            │
                            ▼
CliApp.run():  print(f"\nResponse:\n{response}")  cli.py:207
```

> 旁注:你**打字时**的 `@` 补全候选走的是另一条路 —— `CliApp.initialize()` 启动时调
> `refresh_resources()` → `list_docs_ids()` → 同一个 `read_resource("docs://documents")`,
> 把 id 列表喂给补全器。上面这条主流程是**回车提交后**才跑的。

---

## 逐步精读(关键函数给全)

### 1. 入口 —— `@` 不是 `/`,所以 `_process_command` 返回 False,走 `_extract_resources`
```python
# core/cli_chat.py:35
async def _extract_resources(self, query: str) -> str:
    mentions = [word[1:] for word in query.split() if word.startswith("@")]  # "@deposition.md" -> "deposition.md"

    doc_ids = await self.list_docs_ids()           # server 上所有合法 id
    mentioned_docs: list[Tuple[str, str]] = []

    for doc_id in doc_ids:
        if doc_id in mentions:                      # 只有真实存在的 id 才会被注入
            content = await self.get_doc_content(doc_id)
            mentioned_docs.append((doc_id, content))

    return "".join(                                 # 拼成 <document>…</document> 文本块
        f'\n<document id="{doc_id}">\n{content}\n</document>\n'
        for doc_id, content in mentioned_docs
    )
```

### 2. 两个薄封装 —— 只负责拼 URI,真正干活在 `read_resource`
```python
# core/cli_chat.py:24
async def list_docs_ids(self) -> list[str]:
    return await self.doc_client.read_resource("docs://documents")           # -> ["deposition.md", ...]

# core/cli_chat.py:27
async def get_doc_content(self, doc_id: str) -> str:
    return await self.doc_client.read_resource(f"docs://documents/{doc_id}")  # -> "This deposition..."
```

### 3. client 端读资源(我们修好的封装)
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

### 4. server 端两个 resource(URI 模板里的 `{doc_id}` 自动绑到参数)
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
    return docs[doc_id]      # 返回「整条」内容 —— 没有截断
```

### 5. 注入的 prompt(在 `_process_query` 里拼)
最终进入 `messages` 的那条 user 消息是一个模板(`cli_chat.py:71-87`),把原始 query 与
`<document>` 块嵌进去,并**明确叫模型不要再读**(`cli_chat.py:84`):
> *"If the document content is included in this prompt, you don't need to use an
> additional tool to read the document."*

---

## 问:内容已经在 context 里了,为什么模型还会调 `read_doc_contents`?

一个常见直觉是「`@` 只放 top-K 预览,tool 才读全文」。**这个 codebase 不是这样的。** 已核实:

1. **`@` 注入的就是整篇 —— 没有 top-K、没有截断。** `get_doc_content` → `fetch_doc`
   直接 `return docs[doc_id]`(🟢 `mcp_server.py:51-55`)。
2. **`read_doc_contents`(tool)返回的是同一个东西。** 它的实现 `read_document` 也是
   `return docs[doc_id]`(🟢 `mcp_server.py:16-26`)。所以 **tool 和 resource 拿到的内容
   完全一致** —— 这里不存在"预览 vs 全文"的区别。
3. **prompt 里其实明确劝阻再读**(🟢 `cli_chat.py:84`,见上方引文)。
4. **但 tool 每一轮都会被重新发给模型**(🟢 `chat.py:27`,`while True` 里的
   `get_all_tools`),所以模型始终*有能力*调 `read_doc_contents`,也可能选择去调
   (🟡 模型运行时决策)。它真去调时,拿回来的还是那同一句话 —— 冗余,但在这里无害。

**结论**:这不是 top-K 的问题。`@` 已经给了全文;`read_doc_contents` 只是模型多此一举地
又拿了一遍相同内容。因为示例文档每篇就一句话,所以没影响。

> 真实场景补充:你设想的设计在**大文档**时是合理的 —— 比如 `@` 只注入摘要/前 K 行省
> token,真要全文时让模型调 tool;或者反过来,内容已注入的那一轮干脆**不暴露**
> `read_doc_contents`,避免冗余调用。这个教学项目两者都没做(文档太小)。

---

## 来源(🟢 repo file:line)

- `core/cli.py:199,202,206,207` —— REPL 循环 / 输入 / `agent.run` / 打印
- `core/chat.py:16,22,24,25-28,32-45` —— `run`、`_process_query` 调用、agent loop、tool 分支
- `core/cli_chat.py:24,27,35,36,38,41,43,65,66,69,84,89` —— 封装、`_extract_resources`、`_process_query`、prompt
- `mcp_client.py:83` —— `read_resource`
- `mcp_server.py:16-26,46,51` —— `read_doc_contents` tool、`list_docs` / `fetch_doc` resource

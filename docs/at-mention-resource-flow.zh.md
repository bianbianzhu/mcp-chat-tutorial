# `@mention` 文档资源流程(两条)

输入 `@` 会触发**两条不同的流程**,它们打到 MCP server 的方式不一样。本文把两条分开,
各自逐个函数追踪,并标注 `file:line`。

> **grounding 说明**
> - 🟢 **已核实**:在 repo 源码(file:line)或 `.venv` 里的 mcp SDK 中核对过,
>   或来自本次会话的 headless 探针实测。
> - 🟡 **行为相关**:取决于模型运行时决策,并非代码固定。

## 两条流程速览

| | **流程 A —— 自动补全** | **流程 B —— 注入 + agent loop** |
|---|---|---|
| 触发 | 输入 `@`(**不** Enter) | `@file.ext` 然后 **Enter** |
| 你看到的 | 文档 id 下拉框 | 模型的回答 |
| `list_docs`(`docs://documents`) | **这里不调** —— 读**启动时**填好的缓存 | 每次提交**实时**调 |
| `fetch_doc`(`docs://documents/{id}`) | 完全不涉及 | 每个被提及的文档**实时**调 |
| 敲键/提交时有 server 往返吗? | **没有**(读本地缓存) | **有**(`resources/read` ×N) |

> ⚠️ **常见误解**:「敲 `@` 会调 `list_docs`」。**不会。**
> `UnifiedCompleter.get_completions` 是**同步**方法(`cli.py:52`)——它没法 `await`,
> 所以物理上不可能去调那个 async 的 resource。它只是过滤内存缓存。`list_docs` 在**启动时**
> 跑(填缓存),以及在**提交时**(流程 B)跑——**绝不**在敲 `@` 那一下跑。

---

## 流程 A —— 自动补全(输入 `@`,不 Enter)

### 前提:缓存在启动时被填一次
```
main.py → await cli.initialize()                       main.py:58  (def core/cli.py:179)
  └─ await self.refresh_resources()                    cli.py:180  (def cli.py:183)
       self.resources = await agent.list_docs_ids()    cli.py:185
                          └─ read_resource("docs://documents")  → server list_docs   ← 流程 A 里唯一一次 list_docs
       self.completer.update_resources(self.resources) cli.py:186   ← 列表缓存进内存
```
这是补全用到 `list_docs` 的唯一时刻。会话期间**不会再刷新**
(`refresh_resources` 只被 `initialize` 调用)。

### 每次敲 `@` —— 不发 server 请求
```
你按下 "@"                                              key binding  cli.py:134
  buffer.insert_text("@"); buffer.start_completion()         cli.py:137-139
        │
        ▼
UnifiedCompleter.get_completions(document)              cli.py:52   ← 同步(不能 await)
  if "@" in text_before_cursor:                         cli.py:56
     prefix = 最后一个 "@" 之后的文本                     cli.py:58
     for resource_id in self.resources:                 cli.py:60   ← 读「缓存」列表
        if resource_id.lower().startswith(prefix):      cli.py:61
           yield Completion(resource_id, ...)           cli.py:62   ← 显示在下拉框
```

### 关键代码
```python
# core/cli.py:134 —— "@" 键绑定:只是打开补全菜单
@self.kb.add("@")
def _(event):
    buffer = event.app.current_buffer
    buffer.insert_text("@")
    if buffer.document.is_cursor_at_the_end:
        buffer.start_completion(select_first=False)

# core/cli.py:52 —— 同步补全器:过滤缓存列表,无 server I/O
def get_completions(self, document, complete_event):
    text_before_cursor = document.text_before_cursor
    if "@" in text_before_cursor:
        prefix = text_before_cursor[text_before_cursor.rfind("@") + 1 :]
        for resource_id in self.resources:                 # <- 启动时缓存
            if resource_id.lower().startswith(prefix.lower()):
                yield Completion(resource_id, start_position=-len(prefix),
                                 display=resource_id, display_meta="Resource")
        return

# core/cli.py:183 —— 缓存填充器(只在 initialize() 里调一次)
async def refresh_resources(self):
    self.resources = await self.agent.list_docs_ids()      # -> read_resource("docs://documents") -> list_docs
    self.completer.update_resources(self.resources)
```

**要点**:流程 A = 「显示缓存的 id,按你在 `@` 后输入的内容过滤」。唯一一次 server 接触
(`list_docs`)在启动时就已经发生了。

---

## 流程 B —— 注入 + agent loop(`@file.ext` + Enter)

这是运行时路径,分两段:**资源注入**(把文档内容拉进 `messages`),再 **agent loop**
(模型回复,期间*可能*调 tool)。

```
你提交  "@deposition.md"  ⏎
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
  └─ self.messages.append({... <context>{added_resources}</context> ...})  cli_chat.py:89  (extract 之后)
        │
        ▼
CliChat._extract_resources(query)               cli_chat.py:35
  mentions = [w[1:] ... startswith("@")]            cli_chat.py:36 → ["deposition.md"]
  doc_ids  = await self.list_docs_ids()             cli_chat.py:38   (a) 实时 list_docs
  for doc_id in doc_ids:                            cli_chat.py:41
     if doc_id in mentions:
        content = await self.get_doc_content(doc_id) cli_chat.py:43   (b) 实时 fetch_doc
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

### 关键函数
```python
# core/cli_chat.py:35 —— "@" 不是 "/",所以 _process_command 返回 False,走资源提取
async def _extract_resources(self, query: str) -> str:
    mentions = [word[1:] for word in query.split() if word.startswith("@")]  # "@deposition.md" -> "deposition.md"
    doc_ids = await self.list_docs_ids()           # (a) 实时 list_docs —— 所有合法 id
    mentioned_docs: list[Tuple[str, str]] = []
    for doc_id in doc_ids:
        if doc_id in mentions:                      # 只有真实存在的 id 才被注入
            content = await self.get_doc_content(doc_id)   # (b) 实时 fetch_doc
            mentioned_docs.append((doc_id, content))
    return "".join(
        f'\n<document id="{doc_id}">\n{content}\n</document>\n'
        for doc_id, content in mentioned_docs
    )

# core/cli_chat.py:24 / :27 —— 薄封装(只拼 URI)
async def list_docs_ids(self) -> list[str]:
    return await self.doc_client.read_resource("docs://documents")           # -> ["deposition.md", ...]
async def get_doc_content(self, doc_id: str) -> str:
    return await self.doc_client.read_resource(f"docs://documents/{doc_id}")  # -> "This deposition..."

# mcp_client.py:83 —— client 端读取(await + 解析)
async def read_resource(self, uri: str) -> Any:
    result = await self.session().read_resource(AnyUrl(uri))   # JSON-RPC resources/read
    resource = result.contents[0]
    if isinstance(resource, types.TextResourceContents):
        if resource.mimeType == "application/json":
            return json.loads(resource.text)   # docs://documents      -> list[str]
        return resource.text                    # docs://documents/{id} -> str
    return resource

# mcp_server.py:46 / :51 —— server resource({doc_id} 自动绑到参数)
@mcp.resource("docs://documents", mime_type="application/json")
def list_docs() -> list[str]:
    return list(docs.keys())

@mcp.resource("docs://documents/{doc_id}", mime_type="text/plain")
def fetch_doc(doc_id: str) -> str:
    if doc_id not in docs:
        raise ValueError(f"Doc with id {doc_id} not found")
    return docs[doc_id]      # 「整条」内容 —— 没有截断
```

最终注入的 user 消息是一个模板(`cli_chat.py:71-87`),把 query 与 `<document>` 块嵌进去,
并**明确叫模型不要再读**(`cli_chat.py:84`)。

### 问:内容已在 context 里,为什么模型还可能调 `read_doc_contents`?
常见直觉是「`@` 只放 top-K 预览,tool 才读全文」。**这个 codebase 不是这样的。** 已核实:
1. **`@` 注入的就是整篇,没截断。** `fetch_doc` → `return docs[doc_id]`(🟢 `mcp_server.py:51-55`)。
2. **`read_doc_contents`(tool)返回同一个东西。** `read_document` → `return docs[doc_id]`(🟢 `mcp_server.py:16-26`)。tool 与 resource 内容**完全一致**。
3. **prompt 明确劝阻再读**(🟢 `cli_chat.py:84`)。
4. **但 tool 每轮都重发给模型**(🟢 `chat.py:27`),所以模型*有能力*调 `read_doc_contents`,也可能去调(🟡 模型决策)——冗余地又拿了一遍同一句话。

**结论**:不是 top-K 的问题;是模型冗余地重取了相同内容(这里无害,因为文档就一句话)。
对**大文档**而言,你设想的「预览 vs 全文」分工是合理设计——只是这个教学项目没做。

---

## 来源(🟢 repo file:line)

- 流程 A:`core/cli.py:52,56,58,60-62,134-139,179,180,183,185,186` ; `core/cli_chat.py:24`
- 流程 B:`core/cli.py:199,202,206,207` ; `core/chat.py:16,22,24,25-28,32-45` ;
  `core/cli_chat.py:27,35,36,38,41,43,65,66,69,84,89` ; `mcp_client.py:83` ;
  `mcp_server.py:16-26,46,51`

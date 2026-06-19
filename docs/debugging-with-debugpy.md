# Debugging the MCP Chat app with debugpy (VS Code)

How to attach a Python debugger to **both** the async client (`main.py` + `core/*`)
**and** the MCP server subprocess (`mcp_server.py`) — including the non-obvious
environment-stripping gotcha that breaks it, the fix, the security trade-off, and
exact run steps. Written to be reproducible in any `uv` + MCP project.

> **Grounding legend**
> - 🟢 **Verified** — checked against source in this workspace (repo files or the
>   mcp/anyio SDK under `.venv/`), or observed from a headless probe run in-session.
> - 🟡 **Explanation** — reasoning / likely cause, not independently proven here.

---

## 0. TL;DR — what works, what doesn't

| Path | Mechanism | Status |
|---|---|---|
| Debug the **client** | VS Code `launch` of `main.py` | ✅ works |
| Debug the **server** via auto-attach (`"subProcess": true`) | debugpy follows child processes | ❌ does **not** hit server breakpoints here (empirically) |
| Debug the **server** via explicit listen + attach | `debugpy.listen()` in `mcp_server.py` + a VS Code `attach` config | ✅ works (verified) |

**Use the explicit listen+attach path for the server.** Why the easy path fails and
why the explicit path needs an extra fix are both explained below — they share one
root cause: the MCP SDK does not pass your environment to the server subprocess.

---

## 1. Setup (reproducible in any uv + MCP project)

Four pieces. Items (a)–(c) are code/config; (d) is a one-time VS Code action.

### (a) Add debugpy as a dev dependency
```bash
uv add --dev debugpy
```
This lands in `[dependency-groups] dev` in `pyproject.toml` (here: `debugpy>=1.8.21`).
Runtime deps are untouched — a debugger is dev-only.

### (b) Forward the debugger vars to the server subprocess — THE critical fix
In the MCP client wrapper (`mcp_client.py`), build the subprocess env with a helper
that forwards **only** debugger vars, **only** while debugging (secrets never leave —
see §3 for why the naive "forward everything" version is unsafe):
```python
def _subprocess_env(self) -> Optional[dict]:
    # Default: env=None -> the SDK's safe get_default_environment() (PATH/HOME/... only).
    # Only when DEBUG_MCP_SERVER is set do we add the debugger vars on top; the SDK
    # still merges them with get_default_environment(), so PATH etc. remain available.
    env = dict(self._env or {})
    if os.environ.get("DEBUG_MCP_SERVER"):
        env["DEBUG_MCP_SERVER"] = os.environ["DEBUG_MCP_SERVER"]
        env.update({k: v for k, v in os.environ.items()
                    if k.startswith(("DEBUGPY_", "PYDEVD_"))})
    return env or None

async def connect(self):
    server_params = StdioServerParameters(
        command=self._command,
        args=self._args,
        env=self._subprocess_env(),
    )
    ...
```
(Requires `import os` at the top.) Without forwarding the flag, **nothing** you set in
the parent reaches the server — see §2.

### (c) Add an opt-in debug listener in the server
The server's stdin/stdout are the JSON-RPC channel, so you **cannot** use a terminal
debugger or pass CLI flags. Embed a socket-based listener, gated by an env var, at the
top of the `__main__` block of `mcp_server.py` (before `mcp.run`):
```python
if __name__ == "__main__":
    import os

    if os.getenv("DEBUG_MCP_SERVER"):
        import debugpy

        debugpy.listen(("127.0.0.1", 5679))
        debugpy.wait_for_client()  # blocks until you attach; the client's connect() pauses here

    mcp.run(transport="stdio")
```
debugpy uses a TCP socket, so it never corrupts the stdio transport. Pick a port that
differs from any client debug port.

### (d) `.vscode/launch.json`
```jsonc
{
  "version": "0.2.0",
  "configurations": [
    {
      // Client only. prompt_toolkit needs a real terminal -> integratedTerminal.
      "name": "Debug client (main.py)",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/main.py",
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "subProcess": true,
      "justMyCode": false
    },
    {
      // Client + server. Sets DEBUG_MCP_SERVER=1 (forwarded by the fix in (b)),
      // so the server calls debugpy.listen + wait_for_client and PAUSES at startup.
      "name": "Debug client + server (waits for :5679 attach)",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/main.py",
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "subProcess": true,
      "justMyCode": false,
      "env": { "DEBUG_MCP_SERVER": "1" }
    },
    {
      // Run AFTER the config above to attach to the server's listener.
      "name": "Attach to MCP server (:5679)",
      "type": "debugpy",
      "request": "attach",
      "connect": { "host": "127.0.0.1", "port": 5679 }
    }
  ]
}
```
Notes:
- `"console": "integratedTerminal"` is **required** for the client because the REPL
  uses `prompt_toolkit`, which needs a real TTY (the Debug Console can't provide input).
- `"justMyCode": false` lets you step into the `mcp` / `anthropic` library code; set
  `true` to stay in your own code.
- Setting `DEBUG_MCP_SERVER` in the launch config (not in `.env`) scopes it to that
  debug session, so normal runs don't hang.

### (e) One-time in VS Code
Install the Python extension (`ms-python.python`), then
`Cmd+Shift+P → Python: Select Interpreter → ./.venv` so debugpy is found.

This only tells VS Code to use *this project's* `.venv` when debugging — it's a
per-workspace setting scoped to this project, and does **not** change your system
Python, affect other projects, or alter how `uv` resolves environments (`uv run`
relies on `.venv` + `pyproject.toml` + `uv.lock`, independent of VS Code's choice).
We point both at the same `./.venv` so F5-debugging and `uv run` use the identical
environment.

### What's project-specific when reusing this
- The **client wrapper** file where you build `StdioServerParameters` (here `mcp_client.py`).
- The **server entry** file and its `__main__` block (here `mcp_server.py`).
- The **debug port** (here `5679`).
- The launch **program** (here `main.py`) and that the client needs a terminal.

---

## 2. The environment-stripping gotcha (root cause)

### Symptom
With the listener in place and `DEBUG_MCP_SERVER=1` set in the shell or `.env`:
1. The app did **not** pause at startup, and
2. "Attach to MCP server (:5679)" failed with `connect ECONNREFUSED 127.0.0.1:5679`.

Both mean `debugpy.listen()` never ran — i.e. the server never saw `DEBUG_MCP_SERVER`.

### Root cause (🟢 grounded in the SDK)
The MCP stdio transport does **not** inherit your full environment. In
`.venv/.../mcp/client/stdio/__init__.py`:

- `StdioServerParameters.env` defaults to `None` (line **79**).
- When spawning, the env is built as (line **127**):
  ```python
  env = {**get_default_environment(), **server.env} if server.env is not None else get_default_environment()
  ```
- `get_default_environment()` (line **51**) copies through only an allowlist
  `DEFAULT_INHERITED_ENV_VARS` (lines **28–44**):
  - POSIX: `["HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"]`
  - Windows: `APPDATA, HOMEDRIVE, HOMEPATH, LOCALAPPDATA, PATH, PATHEXT, ...`

`main.py` constructs the client with `env=None` (`main.py:40`), so the server is
spawned with **only those ~6 vars**. `DEBUG_MCP_SERVER` is not among them → stripped.

This is intentional in the SDK: a curated env avoids leaking arbitrary parent secrets
into spawned servers (which may be third-party). It also breaks debugging until you
opt back in.

### Why the "easy path" (auto-attach) also failed
`"subProcess": true` relies on debugpy injecting its bootstrap (`DEBUGPY_*` /
`PYDEVD_*` vars, and command-line rewriting) into child processes. The server child is
spawned through `anyio` (`stdio/__init__.py:124`) → `asyncio.create_subprocess_exec`
(`anyio/_backends/_asyncio.py:2601`) 🟢. In this project it does **not** attach
(confirmed empirically). 🟡 Likely contributors: the env-stripping above removes the
`DEBUGPY_*`/`PYDEVD_*` vars, the optional `uv run` wrapper adds a non-Python process
layer between parent and server, and debugpy's child-injection heuristics don't
reliably fire across the asyncio-subprocess + wrapper path. Net: treat server
auto-attach as unreliable for stdio MCP servers and use explicit listen+attach.

### The fix and why it works
Pass a non-`None` env that includes the flag (see the `_subprocess_env()` helper in
§1b). When `server.env is not None`, the SDK takes the merge branch
(`{**get_default_environment(), **server.env}`, line 127) — so `PATH`/`HOME` are still
present **and** `DEBUG_MCP_SERVER` (plus any `DEBUGPY_*`/`PYDEVD_*` vars) ride along to
the server. When not debugging, the helper returns `None`, so the SDK uses its safe
default unchanged.

### Verification (🟢 in-session, headless)
A probe spawned the server through `MCPClient` with `DEBUG_MCP_SERVER=1` and checked
the port:
```
RESULT: env forwarded + server listening on 5679 = True
RESULT: connect() still blocked (server waiting for debugger) = True
```
i.e. the flag reached the server, `debugpy.listen(5679)` ran, and the server correctly
blocked in `wait_for_client()` (the "pause at startup" behavior).

---

## 3. Security concern and how to avoid it

### The concern
A naive fix — `env={**os.environ, ...}` — forwards **every** parent environment
variable, including secrets like `ANTHROPIC_API_KEY`, to **every** spawned MCP server.
In this app `main.py` can also spawn arbitrary servers passed on the command line
(`main.py:44-49`, `server_scripts = sys.argv[1:]`). So a **malicious or compromised
third-party MCP server** would receive your API keys and could exfiltrate them. This is
exactly the leakage the SDK's allowlist was designed to prevent — forwarding everything
trades it away.

### How it's avoided here — ✅ applied
The code uses the `_subprocess_env()` helper (§1b): rely on the SDK's safe default for
`PATH`/`HOME`, and add **only** debugger vars (`DEBUG_MCP_SERVER`, `DEBUGPY_*`,
`PYDEVD_*`), **only** when the debug flag is set. When the dict is non-empty the SDK
still merges in `get_default_environment()` (so `PATH` etc. work), but secrets are never
copied. When not debugging it returns `None` → exact original SDK behavior. The explicit
attach path still works; nothing functional is lost (server auto-attach didn't work
anyway).

Verified in-session (🟢):
```
[debug ON]  DEBUG_MCP_SERVER forwarded : True
[debug ON]  DEBUGPY_* forwarded        : True
[debug ON]  SECRET NOT forwarded       : True     # ANTHROPIC_API_KEY absent
[debug ON]  forwarded keys             : ['DEBUGPY_FAKE_LAUNCHER_PORT', 'DEBUG_MCP_SERVER']
[debug OFF] env is None (SDK default)  : True
```

### Other hardening options
- **Trust boundary per client:** only forward to the first-party `doc_client`, never to
  servers from `sys.argv`. Add a `forward_env: bool` flag to `MCPClient`.
- **Allowlist, not denylist:** forward an explicit set of names you intend to share.
- **Never forward in production:** keep forwarding strictly behind the debug flag (the
  recommended snippet already does this).

---

## 4. How to run the debugger (detailed steps)

### Debug the server (the path that works)
1. Set breakpoints in `mcp_server.py` (e.g. inside `read_document` / `edit_document`),
   and anywhere in the client (`main.py`, `core/*`) you like.
2. Run config **"Debug client + server (waits for :5679 attach)"** (F5 with it selected,
   or the Run-and-Debug dropdown).
   - The integrated terminal opens and the app **pauses at startup** with no `>` prompt.
     That's expected: the server is in `wait_for_client()`, so the client's `connect()`
     (the `initialize` handshake) is waiting on it.
3. Run config **"Attach to MCP server (:5679)"**.
   - The server's `wait_for_client()` returns, the handshake completes, and the `>` REPL
     prompt appears. You now have two live debug sessions (client + server).
4. Trigger a tool — e.g. type a message that makes Claude call `read_doc_contents`, or
   use `@deposition.md` — and your server breakpoint hits.

### Debug the client only (no pause)
- Run config **"Debug client (main.py)"**. No flag is set, the server runs normally,
  and the REPL appears immediately. Client/`core` breakpoints work.

### Troubleshooting
- **`ECONNREFUSED :5679` / no pause:** the flag didn't reach the server. Confirm the env
  forwarding in `mcp_client.py` (§1b) is present, and that you launched the
  **"+ server"** config (which sets `DEBUG_MCP_SERVER=1`).
- **Every run hangs at startup (even `uv run main.py`):** a `DEBUG_MCP_SERVER=1` is
  active in `.env`. Comment it out — set the flag via the launch config instead. (After
  the env-forwarding fix, an `.env` value *does* reach the server, so a stray one will
  hang all runs.)
- **`Address already in use` on 5679:** a previous server process is still listening.
  Kill it: `pkill -f mcp_server.py`.
- **`frozen modules ... may make the debugger miss breakpoints`:** harmless debugpy
  notice. To silence/strictly fix, run Python with `-Xfrozen_modules=off` or set
  `PYDEVD_DISABLE_FILE_VALIDATION=1`.
- **No input at the REPL / weird prompt:** the client config must use
  `"console": "integratedTerminal"` (prompt_toolkit needs a TTY).

---

## Sources

In-repo (🟢): `mcp_client.py` (env forwarding), `mcp_server.py` (`__main__` listener),
`.vscode/launch.json`, `main.py:40,44-49`.

SDK under `.venv` (🟢):
- `mcp/client/stdio/__init__.py:28-44` (`DEFAULT_INHERITED_ENV_VARS`), `:51`
  (`get_default_environment`), `:79` (`StdioServerParameters.env=None`), `:124`
  (anyio spawn), `:127` (env merge).
- `anyio/_backends/_asyncio.py:2601` (`asyncio.create_subprocess_exec`).
- debugpy `1.8.21`.

Verification: headless probe run in-session (output quoted in §2).
External: VS Code Python debugging — https://code.visualstudio.com/docs/python/debugging
; debugpy — https://github.com/microsoft/debugpy

# Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `dial tcp 172.217.x.x:443 ... failed to respond`, or `Please sign in` | the CLI has no proxy | `register_agy_mcp.py --proxy http://127.0.0.1:7890` (the Go CLI ignores system proxies) |
| A call hangs for minutes | waiting on a Google timeout, or a long agent job | check the proxy; turns have **no time limit by default**, so this is normal for real jobs |
| Empty answer, or `finished without any text` | the turn spent itself on tool calls, or hit the context/time limit | narrow the prompt, pass data via `files`/`diff`, add `no_web: true`, raise `timeout_sec`, or start a new session |
| Precise-looking numbers for "today" | with a broken browser agy cannot fetch live data — it invents them | let Codex fetch the data and hand it over, or share Codex's tools ([codex-tools.md](codex-tools.md)) |
| `denied_actions` / sandbox message | the sandbox refused a tool the agent needed | a real error, not an empty answer; pass `skip_permissions: true` or add an allow rule |
| Tools missing in the client | MCP servers load at session start | open a new thread; confirm the entry is still in the client config |
| Session "forgets" after a directory change | sessions are bound to a workspace | pass `conversation` explicitly to continue elsewhere |
| First call much slower than the rest | cold start of the session process (~7s) | expected; `AGY_MCP_PREWARM=1` pre-starts it |
| Context keeps getting slower/pricier | resuming resends history | `handoff: true` to compact, or `new_session: true` |
| `quota is nearly used up` | 5-hour/weekly window below the threshold | informational; `model: "auto"` moves to a group with headroom |
| No answer when driving the server from a script | closing stdin cancels a running turn after `AGY_MCP_SHUTDOWN_GRACE_SEC` (10s) | keep stdin open until the reply arrives |
| Daily cap reached | guard rail | wait, or set `AGY_MCP_MAX_CALLS_PER_DAY=0` |

## Known limits

- MCP tools are called by the client; they are not a model and never appear in a model picker.
- One call is one whole Antigravity session (its own system prompt and tools): good for real work,
  wrong for high-frequency, low-latency use.
- Resumed conversations resend history, so input tokens grow with turns.
- Programmatic use of a consumer subscription is your call; the guard rails only keep the traffic
  shape close to normal CLI use.

## Compatibility

| Component | Verified |
| --- | --- |
| Antigravity CLI | 1.2.0 on Windows |
| Platform | Windows, Python 3.12. macOS / Linux code paths exist and are covered by offline tests, but have not run on real hardware yet |
| Codex tool sharing | Codex-bundled `node_repl`: computed and web-fetched successfully |

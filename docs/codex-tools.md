# Reusing Codex's tools from agy

agy can be given Codex's own tool stack over MCP, so its agent gets file, shell and **browser**
abilities without installing anything itself.

## Why not let agy install its own browser

The CLI bundles playwright-go and downloads a driver. That download fails here because the CLI asks
Playwright's retired CDN mirrors and gets 404:

```
failed to install playwright: could not install driver: ... 404 (404 Not Found)
  https://playwright.azureedge.net/builds/driver/playwright-1.57.0-win32_x64.zip
  https://playwright-akamai.azureedge.net/...   https://playwright-verizon.azureedge.net/...
```

The binary does honour `PLAYWRIGHT_DOWNLOAD_HOST` / `PLAYWRIGHT_DRIVER_PATH` / `PLAYWRIGHT_NODEJS_PATH`,
so `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.playwright.dev/dbazure/download/playwright` is the fallback fix.

## Register Codex's tools inside agy

```bash
python3 register_agy_mcp.py --list-codex-tools              # what Codex itself runs
python3 register_agy_mcp.py --share-codex-tools --dry-run   # preview the agy mcp add commands
python3 register_agy_mcp.py --share-codex-tools             # share all of them
python3 register_agy_mcp.py --share-codex-tools node_repl   # or pick by name
python3 register_agy_mcp.py --remove                        # drops them again
```

The script reads command, args and environment straight out of `~/.codex/config.toml`, so it cannot
drift from what Codex itself uses. Underneath it is plain CLI:

```bash
agy mcp add --env BROWSER_USE_AVAILABLE_BACKENDS=chrome,iab ... node_repl <path>\node_repl.exe
agy mcp list
```

## What was verified

| Check | Result |
| --- | --- |
| agy calls Codex's `node_repl` js tool | OK: `123*456` → `56088` |
| agy fetches the web through it | OK: fetched `https://example.com`, returned the title `Example Domain` (~26s) |

Ask explicitly the first time: *"Use the node_repl MCP tool to fetch <url> and report …"*.

## Caveats

- Codex's paths contain build hashes; **re-run `--share-codex-tools` after a Codex update**.
- This rides on a private Codex component. If it breaks, fall back to the Playwright download host
  above, or run the task in the Antigravity IDE.
- Those tools run with agy's permissions: keep the sandbox on and only share servers you trust.

## Long jobs that outlive the client

`antigravity_submit` starts the CLI **detached** (`agy -p … --output-format json`, stdout to
`~/.agy-mcp/jobs/<id>.out`), so the run is not tied to the MCP server process:

- the client may restart, the server may die — the job keeps going;
- `antigravity_job` reads `~/.agy-mcp/jobs/` from disk, spots a finished process, parses the JSON,
  records the conversation id back into the session store and returns the answer;
- the trade-off is deliberate: a detached run has **no progress notifications and cannot be
  cancelled**. Use a normal `antigravity_ask` (resident session process) when you want those.

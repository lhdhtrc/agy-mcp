# 让 agy 复用 Codex 的工具

通过 MCP 把 Codex 自带的工具栈交给 agy，它的 agent 就间接获得文件、命令与**浏览器**能力，
不用自己安装任何东西。

## 为什么不直接让 agy 自己装浏览器

CLI 内置 playwright-go 去下载驱动，本机必然失败——它请求的是 Playwright 已废弃的旧 CDN 镜像，全部 404：

```
failed to install playwright: could not install driver: ... 404 (404 Not Found)
  https://playwright.azureedge.net/builds/driver/playwright-1.57.0-win32_x64.zip
  https://playwright-akamai.azureedge.net/...   https://playwright-verizon.azureedge.net/...
```

二进制里确认它认 `PLAYWRIGHT_DOWNLOAD_HOST` / `PLAYWRIGHT_DRIVER_PATH` / `PLAYWRIGHT_NODEJS_PATH`，
所以兜底修法是把下载地址指到
`PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.playwright.dev/dbazure/download/playwright`。

## 把 Codex 的工具注册进 agy

```bash
python3 register_agy_mcp.py --list-codex-tools              # 看 Codex 自己在跑哪些 MCP 服务器
python3 register_agy_mcp.py --share-codex-tools --dry-run   # 先看要执行的 agy mcp add 命令
python3 register_agy_mcp.py --share-codex-tools             # 全部共享
python3 register_agy_mcp.py --share-codex-tools node_repl   # 或按名字挑
python3 register_agy_mcp.py --remove                        # 一并摘掉
```

脚本直接从 `~/.codex/config.toml` 读命令、参数与环境变量，因此不会和 Codex 自己用的配置漂移。
底层就是原生命令：

```bash
agy mcp add --env BROWSER_USE_AVAILABLE_BACKENDS=chrome,iab ... node_repl <路径>\node_repl.exe
agy mcp list
```

## 实测结果

| 验证项 | 结果 |
| --- | --- |
| agy 调用 Codex 的 `node_repl` js 工具 | 通过：`123*456` → `56088` |
| agy 经它抓取网页 | 通过：抓取 `https://example.com`，返回页面标题 `Example Domain`（约 26 秒） |

第一次最好显式点名，例如"用 node_repl 这个 MCP 工具抓取 <网址>，告诉我……"。

## 注意事项

- Codex 的安装路径里带构建哈希，**Codex 升级后要重跑 `--share-codex-tools`**。
- 这是挂在 Codex 私有组件上的用法，不代表官方支持；失效就退回上面的 Playwright 下载地址，
  或改在 Antigravity IDE 里跑。
- 这些工具以 agy 的权限运行：保持沙箱开启，只共享你信任的服务器。

## 能活过客户端重启的长作业

`antigravity_submit` 会把 CLI **脱离进程**启动（`agy -p … --output-format json`，stdout 重定向到
`~/.agy-mcp/jobs/<id>.out`），所以这一轮不绑在 MCP 服务器进程上：

- 客户端重启、服务器退出都不影响它继续跑；
- `antigravity_job` 从磁盘读 `~/.agy-mcp/jobs/`，发现进程已退出就解析 JSON、把 `conversation_id`
  写回会话表并返回答案——**换一个服务器进程也能取回结果**；
- 代价是明确取舍：脱离模式的作业**没有进度通知、也不能取消**。需要这两项时用普通的
  `antigravity_ask`（常驻会话进程）。

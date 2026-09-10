# 排障与兼容性

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `dial tcp 172.217.x.x:443 ... failed to respond` 或 `Please sign in` | CLI 没有代理 | `register_agy_mcp.py --proxy http://127.0.0.1:7890`（Go 程序不读系统代理） |
| 一次调用卡住很久 | 在等 Google 超时，或确实是长作业 | 先查代理；轮次**默认不限时**，长作业跑几分钟属正常 |
| 空回答 / `finished without any text` | 那一轮把预算花在工具调用上，或撞到上下文与时间上限 | 缩小问题、用 `files`/`diff` 把材料给进去、加 `no_web: true`，或开新会话 |
| 给出了"今天"的精确数字 | 浏览器不可用时 agy 取不到实时数据，只能凭记忆编 | 让 Codex 抓数据再交给它分析，或共享 Codex 的工具（见 [codex-tools.md](codex-tools.md)） |
| `denied_actions` / 沙箱提示 | 沙箱拒绝了它要用的工具 | 这是真错误而非空回答；传 `skip_permissions: true` 或加 allow 规则 |
| 客户端里看不到工具 | MCP 服务器在会话启动时加载 | 新开会话；确认客户端配置里条目还在 |
| 换目录后"失忆" | 会话绑定 workspace | 跨目录续接请显式传 `conversation` |
| 第一次调用明显更慢 | 会话进程冷启动（约 7 秒） | 正常；可用 `AGY_MCP_PREWARM=1` 预热 |
| 上下文越来越慢/越来越贵 | 续接会重发历史 | 用 `handoff: true` 压缩，或 `new_session: true` |
| 出现 `quota is nearly used up` | 5 小时/周余量低于阈值 | 只是提示；用 `model: "auto"` 切到还有余量的组 |
| 自己写脚本时收不到回答 | 关掉 stdin 后，在跑的轮次会在 `AGY_MCP_SHUTDOWN_GRACE_SEC`（默认 10 秒）后被取消 | 保持 stdin 打开直到收到响应 |
| 触发每日上限 | 护栏生效 | 等次日，或 `AGY_MCP_MAX_CALLS_PER_DAY=0` |

## 已知边界

- MCP 工具由客户端主动调用，它不是模型，也不会出现在模型选择器里。
- 一次调用 = 一整个 Antigravity 会话（含它自己的系统提示与工具）：适合真实作业，不适合高频低延迟接口。
- 续接会重发历史，input token 随轮次增长。
- 用消费级订阅额度做程序化调用是否可接受由你判断；护栏只是让流量形状更接近正常 CLI 使用。

## 兼容性

| 组件 | 已验证情况 |
| --- | --- |
| Antigravity CLI | 1.2.0（Windows） |
| 平台 | Windows + Python 3.12。macOS / Linux 的代码路径已按平台写好并有离线测试覆盖，但尚未实机运行 |
| Codex 工具共享 | Codex 内置 `node_repl`：计算与联网抓取均已实测通过 |

# agy-mcp

把本机的 [Antigravity](https://antigravity.google) CLI（`agy`）接成一个 MCP 服务器，
让 Codex、Claude 等 MCP 客户端可以直接调用它——用你已有的 Antigravity 账号额度（含 Google One AI Pro）
回答、读仓库、跑 agent，而**不必把 Google 凭据导出给任何中转**。

> 当前版本 v0.1.6，见 [Releases](https://github.com/lhdhtrc/agy-mcp/releases)。
>
> 兼容性：目前只在 **Windows** 实机验证过（agy 1.2.0）。macOS / Linux 的代码路径已按平台写好、
> 离线测试覆盖，但还没有实机跑过 `--self-test`；跑通后欢迎反馈。

- 单文件、纯 Python 标准库、零第三方依赖
- 凭据始终由 `agy` 自己保管（macOS 钥匙串 / Windows 凭据管理器），MCP 侧不接触 token
- 每个客户端会话复用一个常驻 `agy` 进程：首次调用约 7 秒，之后热轮通常 1~2 秒（视网络而定）
- 内置会话续接、上下文过长提醒、交接（handoff）、额度查询与调用节流

## 要求

| 项 | 要求 |
| --- | --- |
| Python | 3.11 或更高（macOS / Linux 用 `python3`，Windows 用 `python` 或 `py -3`）；3.9 / 3.10 需额外装 `tomli`（仅注册脚本用到） |
| Antigravity CLI | `agy --version` 有输出 |
| 网络 | 能访问 Google（中国大陆需要代理，见下） |
| MCP 客户端 | Codex 默认（`~/.codex/config.toml`）；其它客户端手动接入即可 |

## 安装

```bash
git clone <this-repo> && cd agy-mcp

# 一次性自检：路径 / 代理 / 版本 / 登录 / 额度 / 真跑一个小提问
python3 agy_mcp.py --self-test

# 只看状态（不消耗额度）：当日调用与 token、耗时统计、会话进程
python3 agy_mcp.py --status
```

`--self-test` 会逐步打印检查结果，最后给出 `self-test OK` 或失败项清单：

- 基础：agy 路径、代理、版本、登录、额度（额度那步不扣额度）
- 实测一轮（会花一点点额度），并校验**我们依赖的协议形状**：`result` 里是否有 `conversation_id` / `status` /
  `response`，stream 模式下是否有 `init` / `step_update`（嵌套）/ `result`

这几项是为了防"CLI 升级悄悄改字段"——`stream-json` 不是公开契约，形状一变本地实现就会静默退化。
`--skip-ask` 跳过实测那步，`--no-proxy-required` 把"没有代理"从失败降级为提示。

### macOS / Linux

1. 定位 CLI：`which agy`；常见位置是 `~/.local/bin/agy`。找不到就显式指定：

   ```bash
   export AGY_BIN="$HOME/.local/bin/agy"
   ```

2. 登录一次：终端直接运行 `agy`，走浏览器授权。OAuth token 存在 **macOS 钥匙串**（Linux 走 keyring，
   不可用时回退到文件），`agy-mcp` 不需要任何凭据配置。
3. 代理（见下节）。

### Windows

1. 默认安装在 `%LOCALAPPDATA%\agy\bin\agy.exe`，脚本会自动探测。
2. 登录一次：终端运行 `agy`。OAuth token 存在 **Windows 凭据管理器**。
3. 代理（见下节）。

配置里不想写两行（解释器 + 脚本路径）的话，用仓库里的 `agy-mcp.cmd` 包装，只写一行命令即可：

```toml
[mcp_servers.antigravity]
type = "stdio"
command = '''C:\tools\agy-mcp\agy-mcp.cmd'''
```

## 必须设置代理

`agy` 是 Go 程序，**不读 macOS / Windows 的"系统代理"设置**，只认 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量。
没设代理时的表现很有迷惑性：

```
dial tcp 172.217.118.4:443: connectex: ... failed to respond
Error: Please sign in to view available models. Launch the CLI without arguments to sign in.
```

**这个 "Please sign in" 通常不是没登录，而是连不上 Google**——网络不通时它先超时、再回落到登录提示。

直接验证（能列出模型即通）：

```bash
# macOS / Linux
export HTTP_PROXY=http://127.0.0.1:7890
export HTTPS_PROXY=http://127.0.0.1:7890
agy models

# Windows PowerShell
$env:HTTP_PROXY="http://127.0.0.1:7890"; $env:HTTPS_PROXY="http://127.0.0.1:7890"; agy models
```

端口按你自己的代理客户端填（Clash 常见 7890/7897，Surge 常见 6152）。注册脚本会把这些变量写进 MCP 条目，
所以客户端拉起的服务器进程会带上它们，并附加 `NO_PROXY=localhost,127.0.0.1,::1` 避免把本机回环也代理走。

## 注册到客户端

### 方式 A：脚本（推荐）

```bash
python3 register_agy_mcp.py --proxy http://127.0.0.1:7890
```

脚本会 upsert `~/.codex/config.toml` 里的 `[mcp_servers.antigravity]`（**写前备份、写前校验 TOML 可解析**），
幂等可重跑；`--dry-run` 只看改动，`--remove` 卸载，`--clear-env` 清空重置（默认会合并已登记的 env）。

注册后**新开一个客户端会话**（MCP 服务器在会话启动时加载）。

### 方式 B：同时使用 cc-switch

如果 Codex 配置由 [cc-switch](https://github.com/farion1231/cc-switch) 管理，同一条命令会**顺手把这台机器上的
cc-switch 数据库**（`~/.cc-switch/cc-switch.db` 的 `mcp_servers` 表）也写好——因为 cc-switch 以数据库为准，
会在切换供应商/模式时把启用项重新投影到 `~/.codex/config.toml`，只手改配置文件会被覆盖。
检测不到数据库时这一步自动跳过：

```text
cc-switch DB   : ~/.cc-switch/cc-switch.db -> skipped (no cc-switch DB)
```

### 方式 C：手写配置

macOS / Linux：

```toml
[mcp_servers.antigravity]
type = "stdio"
command = "python3"
args = ["/Users/you/agy-mcp/agy_mcp.py"]
startup_timeout_sec = 30
tool_timeout_sec = 900

[mcp_servers.antigravity.env]
AGY_BIN = "/Users/you/.local/bin/agy"
HTTP_PROXY = "http://127.0.0.1:7890"
HTTPS_PROXY = "http://127.0.0.1:7890"
NO_PROXY = "localhost,127.0.0.1,::1"
```

Windows：

```toml
[mcp_servers.antigravity]
type = "stdio"
command = 'C:\Python312\python.exe'
args = ['C:\tools\agy-mcp\agy_mcp.py']
startup_timeout_sec = 30
tool_timeout_sec = 900

[mcp_servers.antigravity.env]
AGY_BIN = 'C:\Users\you\AppData\Local\agy\bin\agy.exe'
HTTP_PROXY = 'http://127.0.0.1:7890'
HTTPS_PROXY = 'http://127.0.0.1:7890'
NO_PROXY = 'localhost,127.0.0.1,::1'
```

> `tool_timeout_sec` 给足：一次 `agy` 调用可能要跑几分钟，别被客户端默认工具超时掐断。

## 提供的工具

| 工具 | 说明 |
| --- | --- |
| `antigravity_ask` | 提问 / 下达任务；默认续接同一会话，回答返回纯文本 |
| `antigravity_quota` | 查看剩余额度（按模型组的周 / 5 小时窗口）；由 CLI 自身回答，**不扣额度** |
| `antigravity_submit` | **后台跑一轮**，立刻返回 job id（长任务不阻塞客户端） |
| `antigravity_job` | 查/列/清理后台任务（`action: get/list/forget`） |
| `antigravity_sessions` | 查看 / 遗忘本服务器跟踪的会话，并列出 CLI 本地已有的会话 |
| `antigravity_models` | 列出可用模型，返回结构化 JSON（`id` + `label`，id 可直接用于 `model` 参数） |
| `antigravity_agents` | 列出可用 agent |
| `antigravity_status` | 诊断：agy 路径、版本、工作目录、代理可见性、当日调用与 token、耗时 p50/p95、登录探测 |

## 会话连续性

默认 `session = "default"`：**同一会话名 + 同一 workspace** 的连续调用续接同一个 Antigravity 会话，
不会每次新开。

- 每个会话长期驻留一个 `agy` 进程（`--input-format stream-json`），多轮共用：
  冷启动约 6.9 秒（含鉴权与模型/额度初始化），**热轮约 1.4 秒**；空闲 15 分钟回收。
- 会话 id 存在 `~/.agy-mcp/sessions.json`；进程被回收或崩溃后，下次调用用 `--conversation <id>` 重新拉起，历史不丢。
- 会话表按 MCP 服务器实例隔离，因此**一个客户端会话对应一个 agy 会话**；同一线程重启服务器会沿用最近实例，
  除非检测到另一个实例仍活跃（`AGY_MCP_INSTANCE_WINDOW_SEC`，默认 120 秒）。
- 想退回"一次调用一个进程"：`AGY_MCP_TRANSPORT=oneshot`。

注意：续接会把该会话历史一起发给模型，**input token 随轮次增长**（一次实测中第二轮 input 从 14k 涨到 28k），
长会话既费额度也费时间。

**取消**：客户端中断一次调用（Codex 里按 Esc）会发 `notifications/cancelled`，服务器收到就立刻结束那一轮所对应的
`agy` 进程（常驻会话进程，或 oneshot / 额度 / 模型这类一次性调用的进程），不再继续烧额度，
也不再回一条没人要的响应；下次调用会自动接着同一会话继续。
工具调用在服务器内保持先进先出，所以不会出现两轮抢同一个会话。
只是查询用的只读工具（`status` / `models` / `agents` / `quota` / `sessions`）不排队，不会被长轮次堵住。

**改模型/强度不会打断正在跑的工作**：切换只在轮次之间生效——正在执行的 Antigravity 轮次会跑完，
旧进程被标记为待回收，空闲后立刻停掉，新轮次用新配置继续同一个会话（上下文不丢）。

**进度**：客户端请求里带 `progressToken` 时，服务器会把每一步转成 `notifications/progress` 发出去——
包括步骤类型与状态，以及**正在生成的那段文字**（例如 `step 2: agent_response ACTIVE — 1 2 3 4 5`），
长时间任务不会再看起来像卡死。默认节流到每 400ms 一条，可用 `AGY_MCP_PROGRESS_INTERVAL_MS` 调整。

## 切换会话与 handoff

### 评审改动（`diff: true`）

让 Antigravity 当第二个评审者时，**不要指望它自己跑 `git diff`**——实测在默认沙箱下这类任务几分钟都跑不完。
服务器改为在本地抓 diff 再作为文本传进去：

```
antigravity_ask(prompt="逐条评审这些改动，按 file:line 给结论", diff=true, cwd="/path/to/repo")
```

它会附上 `git status --short` 与 `git diff HEAD`（无提交时回退到暂存+未暂存），超过
`AGY_MCP_MAX_DIFF_CHARS` 会截断并注明；不是 git 仓库时会记一条说明并照常回答。想对比别的分支用 `diff_base: "main"`。

人工切换：让客户端带不同的 `session` 名调用即可（`session: "review"` / `session: "writing"`）；
**不指定就一直是同一个会话**。

上下文过长时会自动提示一次（默认超过 `AGY_MCP_LONG_CONTEXT_TOKENS=100000` input token）：

```
[agy-mcp] this Antigravity conversation now resends about 105k input tokens per turn (turn 7);
consider handoff: true to compact it into a fresh conversation, or new_session: true to drop the history
```

`handoff: true` 是本地实现的"分叉续接"：先在旧会话里要一份 ≤400 字交接摘要，再**新开一个会话**把摘要作为
前情提要发过去。新会话因此知道前因后果，而每轮重发的历史只剩摘要。

> Antigravity CLI 自身的 `/fork` 只在交互式界面里可用，print / stream 模式会拒绝由 CLI 处理的斜杠命令，
> 所以 headless 场景需要上面这套。

验证方式：在旧会话里记住一个口令，handoff 后在**新会话**（conversation id 不同）追问同一问题，应能正确答出该口令。

代价：handoff 会多花一轮（摘要轮仍是全量上下文），适合"还要继续聊好几轮"的场景；只差一两句就结束的话直接继续更划算。

## 额度查询

`antigravity_quota` 走 CLI 自己回答的 `/quota`，**不起 turn、不扣额度、不留会话**：

```
Gemini Models            Weekly Limit Remaining         99%
Gemini Models            Five Hour Limit Remaining      99%
Claude and GPT models    Weekly Limit Remaining         72%
Claude and GPT models    Five Hour Limit Remaining     100%
```

结果缓存 60 秒（`AGY_MCP_QUOTA_CACHE_SEC`）。

## 参数（`antigravity_ask`，除 `prompt` 外均可省略）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `prompt` | 必填 | 提示词 |
| `files` | 无 | 让 Antigravity 自己去读的文件路径列表（比把文件内容粘进 prompt 更省 token） |
| `diff` | `false` | 把**本地抓的**工作区 diff 作为上下文附上（评审改动用，不依赖 agent 自己跑 git） |
| `diff_base` | `HEAD` | 配合 `diff` 指定对比的 git ref |
| `session` | `default` | 会话名，同名 + 同 workspace 续接 |
| `new_session` | `false` | 重开会话 |
| `handoff` | `false` | 压成交接摘要后**开新会话**继续 |
| `conversation` | 无 | 直接指定要续接的会话 id |
| `continue_session` | `false` | 让 CLI 自己挑最近一个会话续接 |
| `cwd` | 当前工作目录 | 作为 Antigravity 会话的 workspace |
| `model` | `gemini-3.8-flash-high` | 模型 id（见 `antigravity_models`），或 `auto`：按剩余额度自动挑一组还有余量的模型 |
| `agent` | 无 | 透传 `--agent` |
| `effort` | 会话记忆 | `low` / `medium` / `high`；**改一次就一直生效**，`"default"` 清除。模型 id 自带强度，传 effort 会自动换成同族的对应档（如 `gemini-3.8-flash-high` → `flash-low`），不会报冲突 |
| `mode` | 无 | `plan` 或 `accept-edits` |
| `sandbox` | `true` | `--sandbox`，开启终端限制 |
| `skip_permissions` | `true` | 自动批准工具调用（headless 无法弹审批框，关掉就连文件都读不到） |
| `output_format` | `text` | `text`（返回解析后的回答）或 `json`（返回 CLI 原始 JSON） |
| `json_schema` | 无 | 透传 `--json-schema`（内联 schema 或文件路径），让回答结构化 |
| `timeout_sec` | `300` | 单次调用超时（另加 30 秒宽限） |
| `extra_args` | 无 | 追加任意 `agy` 原始参数 |

## 默认权限：自动批准 + 终端沙箱

headless 模式**无法弹出批准提示**，所以"不自动批准"等于"什么都做不了"——实测默认拒绝时 Antigravity 连读一个文件都会被拒
（`ViewFile` denied，返回空回答）。因此本服务器的默认是：

```
--sandbox --dangerously-skip-permissions
```

即**保留终端沙箱（命令仍受限制），但不再逐次请求批准**。这也是官方 CLI 在无人值守场景下的既定用法；
代价是 agent 可以在可访问范围内读写文件、执行沙箱允许的命令，所以：

- 需要严格限制时显式传 `skip_permissions: false`（配合 `cwd` 指向只读目录），但要接受"可能读不到文件"。
- 想把影响面收窄，用 `cwd` 把它圈在目标目录，而不是关掉批准。
- 想只读：`mode: "plan"` 会限制它不要动代码（注意 headless 下仍需上面的权限设置才读得到文件）。

其余安全边界：服务器只在 stdio 上跑 MCP 协议，日志走 stderr，不写任何凭据文件；
工具被拒时（CLI 仍返回 `status=SUCCESS` 但回答为空）会被识别成明确错误并列出 `denied_actions`，而不是空回答。

## 关于"反代 / 转 API"的取舍

把 `agy` 反代成 OpenAI 兼容接口，好处是能当模型用；代价是要维护一个常驻转发服务，并把 Google 凭据交给它，
而且这类做法在账号侧风险更高（非官方客户端、凭据离开官方存储、机器化流量形状）。

MCP 路线对前两条是结构性免疫：请求由官方 CLI 自己发出，OAuth 一直待在系统凭据存储里，
客户端只是触发一次本地进程调用。第三条靠护栏收敛（见下）。

## 护栏（默认开启）

| 护栏 | 默认 | 环境变量 |
| --- | --- | --- |
| 单飞锁：同一时刻只跑一个 `agy` 会话（跨进程文件锁），并发调用排队 | 开 | 等待上限 600 秒 |
| 两次会话之间的最小间隔（热轮只有约 1.4 秒，默认下限会盖过它，追求速度可降到 1~2 秒） | 5 秒 | `AGY_MCP_MIN_INTERVAL_SEC` |
| 每日调用上限，防止循环调用打光额度 | 200 次 | `AGY_MCP_MAX_CALLS_PER_DAY`（`0` = 不限） |
| 本地用量日志：时间、会话、会话 id、是否续接、模型、cwd、prompt 字符数、耗时、退出码、token 用量 | 开 | `AGY_MCP_STATE_DIR` |

用量日志默认**不写 prompt 正文**，只记长度。失败调用不计入每日上限。

## 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `AGY_BIN` | 自动探测 | `agy` 可执行文件路径 |
| `AGY_MCP_AGY_CMD` | 无 | 用整条命令前缀替换 `agy`（如 `wsl agy`、容器包装器、测试用假 CLI） |
| `HTTP_PROXY` / `HTTPS_PROXY` | 无（**一般必须设**） | `agy` 访问 Google |
| `NO_PROXY` | `localhost,127.0.0.1,::1` | 本机回环不走代理 |
| `AGY_MCP_TRANSPORT` | `stream` | `stream` = 每会话驻留进程；`oneshot` = 每次调用新进程 |
| `AGY_MCP_WORKER_IDLE_SEC` | `900` | 驻留进程空闲回收时间 |
| `AGY_MCP_INSTANCE_WINDOW_SEC` | `120` | 判定"另一个实例仍活跃"的时间窗 |
| `AGY_MCP_MIN_INTERVAL_SEC` | `5` | 两次调用最小间隔 |
| `AGY_MCP_MAX_CALLS_PER_DAY` | `200` | 每日调用上限（`0` = 不限） |
| `AGY_MCP_LONG_CONTEXT_TOKENS` | `100000` | 超过多少 input token 提醒 handoff（`0` = 关闭） |
| `AGY_MCP_QUOTA_CACHE_SEC` | `60` | 额度结果缓存时长 |
| `AGY_MCP_MODELS_CACHE_SEC` | `300` | `antigravity_status` 里模型列表的缓存时长 |
| `AGY_MCP_HANDOFF_PROMPT` | 内置提示词 | 覆盖 handoff 摘要提示词（内置版要求"用与原对话相同的语言"输出） |
| `AGY_MCP_SHUTDOWN_GRACE_SEC` | `10` | 客户端关闭连接后，等待在跑的工具调用收尾的秒数（超时则中止） |
| `AGY_MCP_AUTO_HANDOFF` | `0` | 设 `1` 时，上下文超过阈值的那次调用会**自动**压缩并换新会话 |
| `AGY_MCP_USAGE_ROTATE_MB` | `5` | 用量日志超过该大小就丢掉较旧的一半（`0` = 不轮转） |
| `AGY_MCP_PROGRESS_INTERVAL_MS` | `400` | 进度通知最小间隔（`0` = 不节流） |
| `AGY_MCP_MAX_PROMPT_CHARS` | `100000` | prompt 软上限；超了会提示改用 `files` 或让 agent 自己读 |
| `AGY_MCP_MAX_DIFF_CHARS` | `60000` | `diff: true` 时附上的 diff 上限，超出截断并注明 |
| `AGY_MCP_DEFAULT_MODEL` | `gemini-3.8-flash-high` | 不传 `model` 时用的模型（设为空则交给 CLI 自己的默认值） |
| `AGY_MCP_MODEL_PREFERENCE` | gemini 3.8 flash high → 3.1 pro high → claude sonnet 4.6 → claude opus 4.6 → gpt-oss | `model: "auto"` 的挑选顺序 |
| `AGY_MCP_QUOTA_WARN_PERCENT` | `10` | 5 小时 / 周余量低于该百分比时提示一次（`0` = 关闭） |
| `AGY_MCP_QUOTA_REFRESH_SEC` | `300` | 后台刷新配额与提示冷却的间隔 |
| `AGY_MCP_MAX_PARALLEL` | `1` | `>1` 时锁按会话粒度，允许多个不同会话并行（值为并发上限） |
| `AGY_MCP_PREWARM` | `0` | 设 `1` 时服务器启动即拉起默认会话进程，第一次提问不必等冷启动 |
| `AGY_MCP_STATE_DIR` | `~/.agy-mcp` | 会话 / 用量 / 锁文件目录 |
| `AGY_CLI_HOME` | `~/.gemini/antigravity-cli` | CLI 自身状态目录（一般不用改） |

## 状态文件

| 路径 | 作用 |
| --- | --- |
| `~/.agy-mcp/sessions.json` | 会话名 → 会话 id / workspace / 轮次（按实例隔离） |
| `~/.agy-mcp/state.json` | 当日调用次数、token 累计、最小间隔时间戳 |
| `~/.agy-mcp/usage.jsonl` | 每次调用一行（不含 prompt 正文） |
| `~/.agy-mcp/call.lock` | 跨进程单飞锁 |
| `~/.agy-mcp/workers.json` | 当前会话进程的 pid；服务器被强杀后，下次启动据此清理遗留进程 |
| `~/.gemini/antigravity-cli/` | `agy` 自身状态：会话库、缓存与日志 |

## 常用配方

| 场景 | 怎么调 |
| --- | --- |
| 长任务（分析、写长文、逐条评审） | `timeout_sec: 900`（默认 300 秒常被真实的 agent 作业撞到，撞到就会"空回答"）；`AGY_MCP_DEFAULT_TIMEOUT_SEC` 可改全局默认 |
| 长任务不阻塞 | 用 `antigravity_submit` 提交（立刻拿到 `job_id`），期间照常干别的，再用 `antigravity_job` 取结果 |
| 不需要联网的分析 | `no_web: true`——明确禁止浏览。实测浏览器类尝试全是无效功（driver 装不上），会让一轮跑上百步、5 分钟被超时掐断 |
| 评审改动 | `diff: true` + `prompt="逐条评审这些改动，按 file:line 给结论，指出风险与遗漏"` |
| 只出方案不改代码 | `mode: "plan"` + 说明"只给方案，不要改文件" |
| 省钱跑日常 | 不传 `model`（默认 `gemini-3.8-flash-high`），或用 `effort: "low"` 压思考预算 |
| 中途改强度 | 直接传一次 `effort: "low"`（或 `medium` / `high`）——从**下一轮**开始生效并一直保留；`effort: "default"` 恢复 CLI 默认 |
| 难题上强模型 | `model: "claude-opus-4-6-thinking"`（或 `gemini-3.1-pro-high`） |
| 不知道额度够不够 | `model: "auto"`：按 `antigravity_quota` 的组余量挑一个还有空间的模型，并在回答里说明选了什么 |
| 让它读指定文件 | `files: ["/abs/a.py", "/abs/b.md"]`，比把内容粘进 prompt 省 token |
| 换话题 / 压缩上下文 | 换 `session` 名；或用 `handoff: true` 压成交接摘要后开新会话 |

额度快用完时（5 小时或周余量低于 `AGY_MCP_QUOTA_WARN_PERCENT`，默认 10%）会在回答后附一句提示，
只提示不拦截；配 `model: "auto"` 就能自动绕到还有余量的那一组。

## 多账号 / 多实例

## 复用 Codex 的工具（浏览器等）

agy 自己装浏览器驱动会失败（它去的是 Playwright 已废弃的旧 CDN，404）。更干净的做法是**让它直接复用 Codex 的工具栈**——
把 Codex 自带的 `node_repl` MCP 服务器（带 `chrome,iab` 浏览器后端）注册进 agy：

```bash
python3 register_agy_mcp.py --share-codex-tools
```

这条命令会自动从 `~/.codex/config.toml` 读出 Codex 启动 `node_repl` 的全部参数（命令、参数、环境变量）再注册给 agy，
避免手抄；`--dry-run` 可以先看它要执行什么，`--remove` 会一并从 agy 里摘掉。

实测（agy 1.2.0 + Codex 内置 node_repl）：

```
提示：Use the node_repl MCP tool (its js tool) to compute 123*456, then reply with just the number.
回答：56088
```

要点：

- 让 agy 上网时**不要传 `no_web`**；权限默认已放行，一般无需额外配置。
- Codex 的安装路径里带构建哈希（`...\Codex\bin\<hash>\node_repl.exe`），**Codex 升级后要重跑一次** `--share-codex-tools`。
- 这是把 agy 的能力挂在 Codex 的私有组件上：能用，但属于非承诺接口，未来可能失效；真失效就退回修 Playwright
  （`PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.playwright.dev/dbazure/download/playwright`）。

**总的原则**：让 agy 尽可能复用 Codex 的能力（工具、浏览器、以后的插件），只有"推理"用它自己的模型。

想同时挂两个 Antigravity 账号（或个人 + 团队），给每个账号注册一个独立条目即可——
服务器状态、会话与用量都按 `AGY_MCP_STATE_DIR` 隔离，CLI 凭据按 `AGY_CLI_HOME` 隔离：

```toml
[mcp_servers.antigravity-work]
type = "stdio"
command = "python3"
args = ["/Users/you/agy-mcp/agy_mcp.py"]

[mcp_servers.antigravity-work.env]
AGY_CLI_HOME = "/Users/you/.gemini/antigravity-cli-work"
AGY_MCP_STATE_DIR = "/Users/you/.agy-mcp-work"
HTTP_PROXY = "http://127.0.0.1:7890"
HTTPS_PROXY = "http://127.0.0.1:7890"
```

如果两个账号需要不同的 CLI 二进制或包装脚本，再用 `AGY_MCP_AGY_CMD` 指过去即可。

## 排障

| 现象 | 处理 |
| --- | --- |
| `dial tcp 172.217.x.x:443 ... failed to respond`，或 `Please sign in` | 没设代理。用 `--proxy` 重跑注册脚本 |
| 调用卡住几分钟无输出 | 同上，多半在等 Google 超时 |
| 空回答 / `denied_actions` | 沙箱拒绝了它需要的工具。看提示里的权限名，需要就传 `skip_permissions: true`，或在 CLI 的 `settings.json` 加 allow 规则 |
| 客户端里看不到 `antigravity_*` 工具 | 客户端不热加载 MCP，新开会话；确认配置里的 `[mcp_servers.antigravity]` 还在 |
| 换了目录后对话"失忆" | 会话按 workspace 绑定，换目录会新开；跨目录续接请显式传 `conversation` |
| 第一次调用明显比后面慢 | 正常：首次要冷启动会话进程（约 7 秒），之后热轮通常 1~2 秒 |
| 上下文越来越慢、越来越贵 | 续接会重发全部历史；用 `handoff: true` 压缩后开新会话，或 `new_session: true` 直接重开 |
| 达到每日上限（`Daily Antigravity cap reached`） | 等次日，或调 `AGY_MCP_MAX_CALLS_PER_DAY` |
| 回答不是干净正文 | 传 `output_format: "json"` 看 CLI 原始返回 |
| macOS 上找不到 `agy` | `which agy`，或 `export AGY_BIN=...`；脚本按 `AGY_BIN` → `PATH` → `~/.local/bin/agy` 顺序探测 |
| 自己写脚本一次性喂完请求后没有回答 | 管道关闭时，仍在跑的轮次会在 `AGY_MCP_SHUTDOWN_GRACE_SEC`（默认 10s）后被取消；保持 stdin 打开直到收到响应 |
| 回答后多一句 `quota is nearly used up` | 5 小时/周余量低于阈值，只是提示；换 `model: "auto"` 或降低用量 |
| 回答是空的 / 提示 `finished without any text` | 那一轮把预算花在工具调用上了（浏览、读文件），或上下文过大。把数据直接给进去（`files` / `diff`）、缩小问题范围、加大 `timeout_sec`，或 `new_session: true` |
| 让它查"实时行情 / 今天的新闻"却给了看似精确的数字 | **别信**。agy 的浏览器工具在这台机器上装不起来（Playwright driver 下载 404），它拿不到实时数据，只会凭记忆编。这类数据先让 Codex 抓，再把结果交给它分析 |

## 已知边界

- MCP 工具只能被客户端**主动调用**，不能替代会话的默认模型，因此 Antigravity 不会出现在模型选择器里。
- 一次调用 = 一整个 Antigravity 会话（含其自身的系统提示与工具），不适合当高频、低延迟的模型接口。
- 会话历史会随续接一起发送，长会话的 input token 与耗时都会上升。
- 这是用消费级订阅额度做程序化调用，是否可接受请自行判断；护栏只是让流量形状更接近正常 CLI 使用。

## 开发

```bash
python3 -m py_compile agy_mcp.py register_agy_mcp.py
python3 test_agy_mcp.py     # 27 项离线测试：不需要网络、账号或 agy
```

测试通过 `AGY_MCP_AGY_CMD` 注入一个假 CLI，因此连"常驻会话进程 + 多轮协议"也能离线跑。
覆盖：答案提取、会话表按实例隔离、常驻进程多轮复用、oneshot 传输、handoff 换会话、**取消（Esc）**、
进度通知（含文字片段）、自动 handoff、`--self-test`、默认权限、`files`、prompt 护栏、结构化 models、
会话 token 累计、只读工具不排队、孤儿进程回收（含"不误杀无关进程"）。
本地 diff 抓取（含非 git 仓库的降级路径）也在其中。
`tests/fixtures/stream_turn.ndjson` 是录下来的**真实** stream 转写（已脱敏），用来防协议漂移：
改解析器时不用装 agy 也能发现回归。
CI（GitHub Actions）在 Linux / macOS / Windows 上跑同样的命令。

## 许可

[MIT](LICENSE)。

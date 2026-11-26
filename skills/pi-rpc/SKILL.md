---
name: pi-rpc
description: >-
  Use scripts/pi-rpc-ctl.sh (or run-cli.sh with roles configured via {pluginRoot}/scripts/pi-rpc-ctl.sh) to drive the persistent pi --mode rpc UDS daemon with hot session reuse, prompt-cache hits, and disconnect/timeout auto-abort.
---

# `pi-rpc` 常驻 Daemon 与 CLI 驱动指南

通过本地常驻 UDS Daemon（`scripts/rpc/pi-rpc-daemon.mjs`）与轻量 CLI（`scripts/pi-rpc-ctl.sh` -> `scripts/rpc/pi-rpc-ctl.mjs`）驱动常驻 `pi --mode rpc` 会话池。默认沿用 `~/.pi/agent/settings.json` 中的模型与 `thinking: "medium"`。

## 一、架构原理与生命周期

- **Swarm 标准接入**：在 `roles.json` 中配置 `mode: "cli"` 与 `"binary": "{pluginRoot}/scripts/pi-rpc-ctl.sh"`。Swarm `worker` / `reviewer` 通过 `scripts/run-cli.sh` + `run_command(WaitMsBeforeAsync: 500)` 后台运行；`pi-rpc-ctl.sh` 默认阻塞等待直至 `agent_settled` 后退出，由系统自动唤醒外层代理。
- **自动懒启动与并发防双起**：仅 `run` 与 `daemon start` 触发懒启动；CLI 通过原子锁目录 `${sockDir}/daemon.lock.d` 防并发双起，并自动清理死进程的僵尸 socket。默认 socket 路径按 `GBSTACK_PI_RPC_SOCK` -> `$XDG_RUNTIME_DIR/gbstack-pi-rpc/daemon.sock`（长度 `<= 100`）-> `/tmp/gbstack-pi-rpc-<uid>/daemon.sock` 解析，目录权限 `0700`、socket 权限 `0600`。
- **30 分钟空闲退出**：单会话沿用 `GBSTACK_PI_IDLE_TTL_MS`（默认 30 分钟）自动回收；当无活跃会话、无挂起 spawn 且无活跃客户端连接满 `GBSTACK_PI_DAEMON_IDLE_TTL_MS`（默认 30 分钟）时，daemon 优雅清理 `daemon.sock` / `daemon.pid` 并退出。
- **断连自动停写与信号取消**：`run` 默认开启 `abort_on_disconnect: true`（仅显式 `--wait-ms` 提前返回模式关闭）。当 `pi-rpc-ctl` 收到 `SIGTERM` / `SIGINT`（含 `run-cli.sh` 超时按进程组发送的终止信号）或 UDS 连接断开时，CLI 转发 `abort`（3 秒内等 `agent_settled`），daemon 亦会在 UDS `close` 时立即中止对应会话，确保 `pi` 立刻停写且不留孤儿写。

## 二、CLI 子命令（`scripts/pi-rpc-ctl.sh`）

| 子命令 | 常用参数 | 用途 |
|---|---|---|
| `run` | `--cwd <DIR>` `[--model <M>]` `[--thinking <L>]` `[--readonly]` `[--session-id <ID>]` `[--new-session]` `[--wait-ms <N>]` `[--baseline-json <JSON>]` `[--json]` | 从 `stdin` 读取 prompt 并运行；默认 `stdout` 仅输出 `reply` 纯文本，`stderr` 输出单行摘要 `[pi-rpc] session_id=<id> status=<status> duration_ms=<ms> tools=<n>` |
| `steer` | `--session-id <ID>` `--message <TEXT>`（或 `stdin`） | 向运行中会话发送中途纠偏指令 |
| `abort` | `--session-id <ID>` `[--wait-ms <N>]` | 清空排队消息并中止当前轮次，等待 `agent_settled` |
| `sessions list` | `[--json]` | 查看当前热会话列表（含运行中 `active_run` 摘要） |
| `sessions reset` | `--session-id <ID>` | 触发 `new_session` 清空对话上下文并保留热 `pi` 进程 |
| `sessions close` | `[--session-id <ID>]` | 关闭指定会话（省略时关闭全部会话） |
| `daemon status` | `[--json]` | 查看 daemon PID、运行时长、socket 与会话状态 |
| `daemon start` / `stop` | — | 手动预热启动或优雅停止 daemon 并清理全部子进程 |
| `preflight` | `[--check-auth --model <M>]` | 离线快检（Node >= 18、`resolvePiBinary()`、socket 父目录权限）；传 `--check-auth --model <M>` 时额外校验鉴权；成功输出 `Preflight OK: pi-rpc (<piBin>)` |

- `thinking` 支持 `"off"` | `"minimal"` | `"low"` | `"medium"`（默认） | `"high"` | `"xhigh"` | `"max"`。
- **退出码**：`0` 成功（`status === 'done'` 且无 `readonly_violation`）、`1` 运行错误或 `error`/`aborted`、`2` 参数或 `baseline` 校验失败、`4` `readonly_violation`、`127` 找不到 `pi` 可执行文件、`128+N` 信号取消。

## 三、热会话复用与安全边界

1. **热会话复用（Prompt Cache 命中）**：`run` 会在 `stderr` 输出 `[pi-rpc] session_id=<id> ...`。返工或连续追问时，通过 `run-cli.sh --session-id <ID>`（映射角色配置 `sessionArgs`）或 `pi-rpc-ctl.sh run --session-id <ID>` 复传同一 `session_id`，直接复用常驻 `pi` 进程与历史上下文，大幅提升 `cacheRead` 命中率。
2. **新任务与空闲可写会话自动轮换**：新独立任务省略 `--session-id`（或显式传 `--new-session` 映射 `newSessionArgs`）。当同 `cwd` 已存在 `idle` 态可写会话且 `model` 相同时，daemon 自动执行 `new_session` 清空旧上下文并**轮换新 `session_id`**（使旧 ID 失效，杜绝跨任务串上下文）；若该会话仍在 `running`/`busy`/`aborting` 态，则严格拒绝并发写。
3. **4 键 `baseline` 与只读强制**：写入任务传 `--baseline-json`（仅接受 `gitCleanHead`、`fileSha1`、`plannedNew`、`allowedPaths`）；只读任务传 `--readonly`，将 `pi` 工具集限制为 `read,grep,find,ls` 并在返回前校验工作区零改动。同 `cwd` 仅允许 1 个活跃可写会话，多路并发写必须使用各自独立的 `git worktree`。

## 四、只读交叉审查与 Gemini 反过度防御裁决

- **只读审查调用**：通过 `reviewer` 角色（配置 `--readonly`，例如 `--model openai-codex/gpt-6.2-astra --thinking high`）执行交叉审查时，在提示词中明确要求**仅报告有真实运行时触发路径与实证支撑的问题**，禁报假想边界与纯防御性兜底建议。
- **Gemini 反过度防御裁决**：收回 GPT 审查结果后，禁止原样照单派发返工。必须由 Gemini 主控（Coordinator，必要时结合 `reviewer-gemini`）逐条核验真实触发路径、实际风险收益与新增复杂度/维护成本，过滤并驳回无实证空值/兜底保护、过度包装或低 ROI 意见并记录理由，仅将经裁决值得做的阻塞项派发修复。

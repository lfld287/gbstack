---
name: pi-rpc
description: >-
  Use the persistent gbstack_pi-rpc MCP tools (pi_run, pi_wait, pi_steer, pi_abort, pi_sessions) from the main Agent, Coordinator, or MCP-inheriting subagents to drive persistent pi --mode rpc sessions with hot session reuse and prompt-cache hits.
---

# `pi-rpc` 常驻会话驱动指南

使用插件内置 MCP Server `gbstack_pi-rpc` 提供的 5 个 `eager` 工具，直接驱动常驻 `pi --mode rpc`（默认沿用 `~/.pi/agent/settings.json` 中的模型、`thinking: "medium"`、`wait_ms: 60000`），免外部 CLI 预检与外层代理开销。

## 一、适用范围与 Gate-M 平台边界

- **可直接调用的会话**：顶层主 Agent、Swarm Coordinator，以及显式继承 MCP 工具的 `invoke_subagent` / `agy --agent`。
- **不可调用的会话**：`swarm{action:"add"}` 派生的 Swarm worker **不继承**任何 MCP 工具（无 `mcp_gbstack_pi-rpc_*` 也无 `call_mcp_tool`）。因此 `mode: "mcp"` 角色由 Coordinator 直接调用本组工具，禁止经 `swarm{action:"add"}` 再套一层外层代理。

## 二、5 个 `eager` 工具精确签名

| 工具名 | 必填参数 | 可选参数 | 用途 |
|---|---|---|---|
| `mcp_gbstack_pi-rpc_pi_run` | `cwd`, `prompt` | `session_id`, `thinking`, `readonly`, `model`, `wait_ms`, `baseline` | 启动新会话或复传 `session_id` 续跑 |
| `mcp_gbstack_pi-rpc_pi_wait` | `session_id` | `wait_ms`（默认 `60000`） | 等待 `status: "running"` 的轮次收敛（完成后幂等） |
| `mcp_gbstack_pi-rpc_pi_steer` | `session_id`, `message` | — | 向运行中会话发送中途纠偏指令 |
| `mcp_gbstack_pi-rpc_pi_abort` | `session_id` | `wait_ms`（默认 `10000`） | 清空排队消息并中止当前轮次，等待 `agent_settled` |
| `mcp_gbstack_pi-rpc_pi_sessions` | `action` (`list` \| `reset` \| `close`) | `session_id`（`reset` 必填，`close` 可选） | 查看会话、`new_session` 清上下文或关闭进程 |

- `thinking` 支持 `"off"` | `"minimal"` | `"low"` | `"medium"`（默认） | `"high"` | `"xhigh"` | `"max"`。
- 仅在明确需要覆盖 `~/.pi/agent/settings.json` 时传 `model`。

## 三、热会话复用与异步接力

1. **热会话复用（Prompt Cache 命中）**：保存 `pi_run` 返回的 `session_id`；返工、追问或连续小步迭代时复传同一 `session_id`，直接复用常驻进程与历史上下文，显著降低首字延迟与输入 token。
2. **异步接力**：`pi_run` 若因超过 `wait_ms` 返回 `status: "running"`，不要重开会话——用 `pi_wait(session_id)` 继续等待；发现方向偏离时用 `pi_steer(session_id, message)` 纠偏；超时或失控时用 `pi_abort(session_id)` 中止。
3. **进程复用清上下文**：同一工作区切换独立任务时，调 `pi_sessions(action="reset", session_id=...)` 触发 `new_session`，保留已加载的 `pi` 进程并清空对话上下文；不再需要时调 `pi_sessions(action="close", session_id=...)` 释放。

## 四、安全边界与并发隔离

- **写入任务必传 4 键 `baseline` + `allowedPaths`**：`baseline` 仅接受 `gitCleanHead`、`fileSha1`、`plannedNew`、`allowedPaths` 四个键。工具层在写前校验基线并在写后检查 `out_of_scope_files`，越界即拒绝/标记。
- **只读任务必传 `readonly: true`**：将 `pi` 工具集限制为 `read,grep,find,ls`，并在返回前核验工作区未被篡改。
- **同 `cwd` 仅允许 1 个可写会话**：禁止在同一 `cwd` 上并发运行多个可写 `pi` 会话；多路并行写入必须各自使用独立的 `git worktree` 作为 `cwd`。

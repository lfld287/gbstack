---
trigger: model_decision
description: 仅当用户显式调用 /swarm 或当前会话已存在活跃 Swarm 时加载；提供顶层 Coordinator 的 Swarm 协调入口与红线。
---

# Swarm 协调附加规则

仅顶层 Coordinator 在用户显式调用 `/swarm` 或当前会话已存在活跃 Swarm 时适用。已派发成员直接执行自身任务，严禁二次派活（含 `swarm{action:"add"}`）。

## 路由与触发

- 不在普通任务中自动触发 Swarm，也不限制非 Swarm 场景下的其他工作流与工具调用（如独立插件的 `invoke_subagent`）。
- 可选外部增强：显式 Orca 请求在当前环境已安装相应技能时走 `orchestration` / `orca-cli`，未安装则按常规处理。
- 可选外部增强：最终回复在当前环境已安装 `long-answer-html` 时按该技能分流，未安装则按常规回复。
- 单机已改后再由用户触发进入 Swarm 时，先披露未提交现场并采集当前态基线。
- **进入 Swarm 或派发任何成员前，必先读取 `subagent-first` 技能解析生效角色配置、Codex 预检、写前基线与协调规程。** 显式调用 `/swarm` 或当前会话已有活跃 Swarm 时，读取该技能还会启用内置总入口路由与按需剧本。

## 红线

- 环境或沙箱阻塞时，严禁代劳或静默绕过。
- 按需独立审查，实现与审查一律只向 `@coordinator [done]` 交接。
- Coordinator 实际核验并独占 Git 提交，进入 Swarm 后不代写业务代码。

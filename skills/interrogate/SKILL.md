---
name: interrogate
description: >-
  对抗审查、多模型审查、代码压力测试或找盲点时使用。英文触发包括 `interrogate`、`adversarial review`、`multi-model review`、`challenge this`、`stress test this code`、`find blind spots`、`tear this apart`。
---

# 对抗审查

用独立视角挑战实现，只报告经验证的发现。严禁直接改代码修复。

## 1. 界定意图（Frame）

- 优先审查用户指定的目标。未指定时，以基准分支与 `HEAD` 的 merge-base 为起点，对照当前工作树，纳入范围内的暂存、未暂存和相关未跟踪内容。
- 用自己的一句话重述预期行为和边界。意图不明时先向用户确认，再开展审查。

## 2. 隔离对抗（Challenge）

- 让对抗者独立检查同一快照和意图。使用只读环境或隔离临时副本、worktree。隔离环境须拷入范围内的未提交变更。
- 顶层按 [`subagent-first`](../subagent-first/SKILL.md) 第二节定位 `<PLUGIN_ROOT>`，运行 `<PLUGIN_ROOT>/scripts/resolve-roles.sh --cwd <WORKDIR> slot interrogate` 解析 `slots.interrogate`。使用 `review.memberRoles` 中的只读成员，在 brief 中附各自非空的 `promptPath` 并要求先读。按成员的 `mode` 派发（`native` 取 `native.model`；`cli` 取 `cli.outerModel` 并在 brief 写明 `执行模式: CLI`、`ROLE_ID` 与 `PLUGIN_ROOT`），`requiresCliPreflight` 为真时运行 `<PLUGIN_ROOT>/scripts/preflight-cli.sh --cwd '<WORKDIR>' --review <review.id>`（或对 `review.cliRoles` 逐个传 `--role <id>`；不可将槽位名 `interrogate` 直接传给 `--review`）按协调手册预检。
- 成员在任何目录（包括隔离副本和 worktree）都严格只读。禁止落盘脚本、测试夹具或临时产物，只执行无写入副作用的只读检查命令。确需落盘时，由具备写入授权且已核对有效基线的角色在隔离环境预先准备。
- 槽位不可用时，报告未覆盖的审查。成员不可用或低配额导致缺家时按 `review.onMissingProvider`，成员失败或证据不全时按 `review.onIncomplete`，回退须遵守 `review.allowSameProviderFallback` 和提供商数量门槛。按协调手册第五节报告 `未审完:`，严禁静默替换或按完整审查放行。Swarm 仅由顶层 Coordinator 派发，成员只完成自身审查并交回 Coordinator。
- 单机已派发成员（如 `invoke_subagent` 子代理或 `run_workflow` 步骤）只在当前上下文内完成对抗验证并回报调用方，禁止二次派活。顶层已获调用授权且宿主能按解析配置派发时，可用 `invoke_subagent` 在隔离副本或 worktree 中独立挑战，例如分别检查边界输入、状态一致性和回归。无法独立调用时分视角检查，如实说明来源和缺失的异构覆盖。
- 每位对抗者须在自身权限内实际运行测试、复现脚本或检查命令举证。每项只交标题、精确命令、实际输出和意图期望。

## 3. 复跑验真（Verify）

- 在同一快照和等效环境中逐条复跑命令。只有实际输出确实显示失败且该失败破坏意图时才保留。
- 合并同根因项并保留全部来源。纳入所有对抗者去重后经筛选保留的已验证发现，禁止按来源或排名取舍。
- 剔除未实际运行的推测、复跑未证实或未破坏意图的项、纯风格／命名／结构抱怨、仅在假设性未来变更后才触发的风险、无运行时证据的过度防御性建议（多余空值检查、异常吞咽、假想分支、过度包装），以及修复收益低于引入成本的低价值改动。

## 输出

1. **意图重述**。一句话写出预期行为与边界。
2. **已验证发现**。每项含标题、精确命令、实际输出、如何破坏意图及发现来源。无则写「无已验证发现」。
3. **已剔除项**。按来源列出剔除条数及原因。

---
name: council
description: >-
  仅在用户显式触发 `/council` 或点名 `council` 时使用。常规 Review Gate 与普通代码或 PR 审查不自动升级。默认由三家提供商的成员独立只读评审代码、PR、方案或设计，可经裁决和最小修复复审至收敛。
---

# 多模型评议

仅在用户显式触发 `/council` 或点名 `council` 时使用。常规 Review Gate 与普通代码或 PR 审查不自动升级。常规单轮只读复跑验真（非 `council`）委托 [`interrogate`](../interrogate/SKILL.md)。

## 1. 准备快照

- 用一句话界定目标与边界。代码审查未指定范围时，以基准分支与 `HEAD` 的 merge-base 对照当前工作树，纳入暂存、未暂存及相关未跟踪文件。
- 仅在用户要求时，先处理已定本地范围内的已知问题并跑通检查（Swarm 下按写前基线派 `worker` 执行，不直接回复远端 PR 评论），再交付一致快照。

## 2. 派发只读成员

按 [`subagent-first`](../subagent-first/SKILL.md) 第二节定位 `<PLUGIN_ROOT>`，运行 `<PLUGIN_ROOT>/scripts/resolve-roles.sh --cwd <WORKDIR> slot council`。槽位为 `null` 时查询退出 `3`，报告不可用并停止本次评议。解析失败时报告错误并停止派发。

- 用 `review.memberRoles` 派发严格只读的成员。依各自的 `mode` 取 `native.model` 或 `codex.profile`（另取 `codex.outerModel` 驱动外层），在 brief 中附非空的 `promptPath` 并要求先读，禁止二次派活。`review.requiresCodexPreflight` 为真时，按 `review.codexProfiles` 核对所需 profile，并按协调手册预检。
- 默认配置要求三家不同提供商。用户可点名覆盖成员或标为 `non-blocking`。派发前按 `review.requireDistinctProviders` 和 `review.minDistinctProviders` 核对实际覆盖。成员不可用、预检失败或低配额导致缺家时，按 `review.onMissingProvider` 报告缺项，默认停下交用户决定。回退遵守 `review.allowSameProviderFallback` 和协调手册的预检规则，严禁静默降级凑数。

- **Swarm**　仅顶层 Coordinator 调度。完成所需预检后，并行用 `swarm{action:"add"}` 派发解析后的成员。
- **单机**　已派发子代理禁止二次派活，向调用方报告该限制。顶层仅在环境支持按指定配置派发独立只读代理时并行派发，例如通过已安装的 `orchestration`。无法达到提供商覆盖要求时如实报告并建议切换 `/swarm` 或 `orchestration`。严禁用单模型角色扮演模拟异构评审。

## 3. 裁决、修复与复审

1. 每轮让成员并行独立只读审查同一固定快照。成员返回即核对代码与事实，整理有效发现与分歧。所有阻塞成员本轮全部返回前，禁止修改同一工作区文件。修复前若仍有 `non-blocking` 成员在同一工作区运行，须先终止这些成员，或确认已无成员继续读取该工作区。其迟到结果仅作参考。
2. 主控逐条判断是否值得改，优先核查运行时证据，反驳超出目标的建议和无运行时证据的防御兜底。改变公共接口、扩大范围或执行不可逆操作前，停下请用户拍板。
3. 任一阻塞成员本轮失败或返回 `未审完:` 时，按 `review.onIncomplete` 停止收敛循环，保留 `未审完:` 及相关证据，默认交用户拍板。仅当全部阻塞成员完整完成本轮并满足提供商覆盖要求，才可在全员满意或无值得修的阻塞项时按收敛结束。轮次上限取 `review.maxRounds`（默认配置为 **3 轮**），用户明确指定时从其要求。达上限仍未收敛时停止循环，汇总剩余分歧交用户拍板。
4. 显式触发 `/council` 默认授权在已定本地工作区范围内做最小修复并复审。若用户要求仅只读评审或讲解 PR，或命中只读 [`pr-review`](../subagent-first/playbooks/pr-review.md) 场景，从准备快照起保持只读，首轮汇报结论后待用户拍板再转入修复。仍有轮次且确认需改时，等全部阻塞成员停审后做最小修复并复跑验证。Swarm 由 Coordinator 采集写前基线后派 `worker` 修改，Coordinator 不代写业务代码。随后将修复后的同一快照交全部阻塞成员进行下一轮（Round N+1）。

## 输出

- 初审和每次完整复审各计 1 个 Round。代码审查要求成员每轮给 `X/10`，缺分填 `no score`，严禁编造。`non-blocking` 成员不记分、不入表。纯方案或设计评议可省略评分表。
- 默认仅在最终回复中给出 `## Council review`。仅在用户明确要求更新 PR 时，才编辑已有 PR 描述，在末尾模型或署名行之前写入或原地更新该区块。严禁发 PR comment，未获明确要求不得修改远端 PR。
- 按实际轮数调整表列，同步列出各轮关键采纳、驳回的理由和未决分歧。

```markdown
## Council review

Rounds: <N>

| Delegate | Round 1 | Round 2 |
| --- | --- | --- |
| <阻塞成员> | <X/10 或 no score> | <X/10 或 no score> |

- Round 1 <关键采纳及理由>、<关键驳回及理由>
- Round 2 <关键采纳及理由>、<关键驳回及理由>
- 未决分歧 <分歧与待决事项，或无>
```

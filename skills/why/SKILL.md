---
name: why
description: >-
  用于「为什么这样设计」（why does X work this way、why we picked Y）、设计动机（design rationale）、回归原因（regression cause）、事后复盘（postmortem）或有数据支撑的阈值由来（threshold rationale）。运行时行为用 how。
---

# 从记录解释设计决策

严格只读。设计动机以历史记录为准。当前代码仅用于定位目标符号和变更。

1. 找到引入目标变更的 commit，再顺时间追踪后续历次修改，查对应 PR 讨论和变更记录。线索稀薄时再查 ADR、issue、commit trailers 或原始数据。所有 Git 命令带 `GIT_OPTIONAL_LOCKS=0`，产出 diff 的命令加 `--no-ext-diff --no-textconv`。
2. 单机直接检索。Swarm 由 Coordinator 派 `scout` 搜集记录和原始引用，成员直接执行并交接。汇总时保留来源与证据缺口。
3. 解释当时的决策、约束与权衡，区分最初理由和后续调整。每项历史结论引用原作者原话及出处，或给出精确 commit SHA 及提交信息或相关讨论。阈值由来须引用记录中的数据或实验。
4. 自己的推论一律标「推断」并说明依据。历史材料答不到的部分写明「记录未说明」。禁止把从当前代码反推的动机写成历史事实。

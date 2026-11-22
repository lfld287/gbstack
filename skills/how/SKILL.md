---
name: how
description: >-
  用于「X 如何工作」（how does X work）、改码前走查（code walkthrough）和模块归属或分层判断（where should this live、which package owns this、is this the right layer）。解释架构、运行时流向与职责边界。设计动机和历史决策用 why。
---

# 理解代码如何工作

严格只读。面向不熟悉该区域的资深工程师，建立读完就能自信动手的心智模型。所有代码结论附 `path:line`。

1. 范围有歧义时，先说明准备解释的子系统、入口或边界，随即开始探索。用户纠偏后按新范围继续。
2. 围绕问题做有界搜索和阅读。从实际入口沿调用链追踪状态归属、数据流与控制流，核对关键分支和跨层交接。用接口、依赖方向和实际职责判断模块归属。Git 查询一律带 `GIT_OPTIONAL_LOCKS=0`。
3. 单机直接探索。用户点名且工具可用时可用 `invoke_subagent` 派发只读探索。Swarm 由 Coordinator 派 `scout` 执行，要求交付可直接呈现的架构解释。成员直接执行并向 Coordinator 交接。
4. 按问题组织心智模型与运行时流向，不套固定模板。Swarm 以 `scout` 的原稿为主体，Coordinator 只补必要上下文或微调表述，严禁大幅改写。

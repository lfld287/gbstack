---
name: subagent-first
description: >-
  顶层 Coordinator 的 Swarm 协调手册，涵盖生效角色配置、外部 CLI 预检、写前基线、审查验收与故障恢复。
---

# Swarm 协调规程

## 一、Coordinator 职责与单一调度权

- 仅顶层 Coordinator 拆解、派发、验收与集成。进入 Swarm 后不遍读全仓、不代写业务代码。
- Swarm 成员（含 `monitor`、`narrator`）、`invoke_subagent` 子代理、`run_workflow` 的 `agent()` 步骤和 Orca worker 直接执行自身任务，严禁二次派活（含 `swarm{action:"add"}`）。
- 汇报先说现状和结论，保留阻塞与待决项。已定范围内的细节自主处理，扩大目标或影响面、改变公共接口或技术取舍交用户拍板。

## 二、生效角色配置与外部 CLI 会话级预检

从已加载的 `subagent-first/SKILL.md` 所在目录解析 `../..` 得到绝对路径 `<PLUGIN_ROOT>`。生效配置（Schema v2，`schemaVersion: 2`，不再支持 v1）是派发角色、模型与审查组的唯一配置来源，按以下顺序合并，后者覆盖前者。

1. `<PLUGIN_ROOT>/config/roles.default.json`
2. `${GBSTACK_USER_CONFIG_DIR:-$HOME/.gemini/config/gbstack}/roles.json`
3. `<repo>/.agents/gbstack/roles.json`（安全边界：默认禁止项目层定义或覆写非 `null` 的 `roles.*.cli`，包括其中的 `rework` / `fallback`，防止不可信仓库注入命令；确需项目级定义时须显式传 `--allow-project-cli` 或设 `GBSTACK_ALLOW_PROJECT_CLI=1`）

会话开始时，Coordinator 必须运行 `<PLUGIN_ROOT>/scripts/resolve-roles.sh --cwd <WORKDIR> dump`。退出码 `1` 表示配置校验失败，停止派发并报告原始错误。解析器报错时先处理错误再派发。派发前以同一 `--cwd` 用 `slot <name>` 解析槽位、`role <id>` 查询角色、`review <id>` 查询审查组，并可用 `cli-roles [--format json|lines]` 列出当前启用的 CLI 角色。`slot` 的 `kind` 指明读取 `role` 还是 `review`，后者用 `review.memberRoles` 获取成员。退出码 `3` 表示查询项缺失或已禁用，按不可用处理并报告受影响的派发。

| 槽位 | 剧本或技能中的用途 |
|---|---|
| `coordinator` | 顶层 Coordinator |
| `recon` | `scout` 只读取证 |
| `plan` | `plan` 设计与规划 |
| `implement` | `worker` 实现与返工 |
| `monitor` | `monitor`，仅多路并发写或长耗时多阶段接力时按需启用 |
| `review` | 常规 Review Gate 和 PR 的独立 `reviewer` |
| `interrogate` | 对抗审查的 `reviewer` |
| `council` | 显式 [`council`](../council/SKILL.md) 的异构 `reviewer` |

用户可在 `roles.json` 增加、移除或覆盖角色，移除后同步调整引用。自定义角色 ID 可直接通过 `role <id>` 解析并派发。用户显式指定某种外部 CLI 审查（如 `codex`、`pi`、`omp`、`claude`）时，不再通过固定槽位，而是直接按 `resolve-roles.sh role <id>` 或自定义 `review <id>` 解析派发。

- 按解析结果中的 `mode`（`"native"` | `"cli"`）、`readonly`、`workspace` 派发，并发写优先用已配置的 `concurrentWorkspace`，只用 `share` / `branch` 隔离写入。Native 派发显式传 `native.model`。CLI 派发按 `CLI_PROFILE`（`default` | `rework` | `fallback`）显式传生效 profile 的 `outerModel` 作为薄 Swarm 外层模型；未覆写时沿用基础 `cli.outerModel`（内置角色用 `gemini-3.8-flash-medium`）。切换 profile 时同步调整外层模型。外层调用 `<PLUGIN_ROOT>/scripts/run-cli.sh --profile <profile>` 驱动内层 CLI，runner 和外层均不得自行重试或切换 profile。型号不可用时先用 `swarm{action:"models"}` 核对并报告，低配额按第五节切换。成员名使用小写 `[a-z0-9_-]`，最长 24 字符。
- Brief 开头附非空的 `promptPath` 绝对路径，并要求成员先读该文件。写明 `执行模式: CLI` 或 `执行模式: Native`。CLI 模式另填 `ROLE_ID`、`PLUGIN_ROOT` 和可选的 `CLI_PROFILE: <profile>`（省略时为 `default`），写入任务附 `--baseline-json`（仅允许 `gitCleanHead`、`fileSha1`、`plannedNew`、`allowedPaths` 四个键，未知键会被 `run-cli.sh` 以退出码 `2` 拒绝）及可选 `--add-dir` / `--timeout`，外层隔离写先 `pwd` 取 `--cwd`。
- 解析结果的 `requiresCliPreflight` 为真时，在派发前对每个实际使用的 CLI 角色（`cliRoles`）或审查组执行只读预检：运行 `<PLUGIN_ROOT>/scripts/preflight-cli.sh --cwd '<WORKDIR>' (--role <ROLE_ID> | --review <REVIEW_ID> | --all) [--profile <profile>]`。`--profile` 支持 `default`（缺省）、`rework`、`fallback`、`all`；`--review` 必须传 `slot` 解析出的 `review.id`（不可直接传槽位名）。退出码语义：`0` 通过、`1` 预检或配置失败、`2` 参数错误或显式指定的角色未配置 fallback、`3` 缺失或已禁用、`127` 找不到可执行文件（生效 profile 的 `binary` 及 `fallbackBinaries`）。显式使用 `--role <ROLE_ID> --profile fallback` 时，若该角色未配置 `cli.fallback`，退出 `2`。批量使用 `--review <REVIEW_ID>` 或 `--all` 搭配 `--profile fallback` 时，只预检目标范围内已配置 `cli.fallback` 的 CLI 角色，跳过未配置的角色；若没有符合条件的角色，输出跳过提示并退出 `0`。同会话按 `(ROLE_ID, profile)` 缓存通过结果，切到未检组合、报错或环境变化时重检。跳过的角色不得记入 `(ROLE_ID, fallback)` 预检通过缓存。
- 预检失败先报告具体缺项与退出码，再按顺序处理：① 已配置 `cli.fallback`、当前未在用 `fallback`，且用户未锁定主工具/主模型时，先运行 `<PLUGIN_ROOT>/scripts/preflight-cli.sh --cwd '<WORKDIR>' --role <ROLE_ID> --profile fallback`；通过后在 brief 指定 `CLI_PROFILE: fallback`，让 runner 用 `--profile fallback`，并按生效的 `outerModel` 切换外层模型。② 没有可用的 CLI fallback 时，仅当 `cli.fallbackToNativeOnPreflightFail` 为真、有 `native` 配置且用户未锁定 CLI 执行模式时才改走 Native。审查回退均须满足第四、五节的提供商约束；无可用回退时停止派发该角色并报告，显式指定的 CLI 审查或 `council` 缺少所需成员时停止该轮派发。显式选择 Native 时免外部 CLI 预检。

## 三、并行协作、轻量基线与自包含 Brief

- 开局用 `swarm{action:"title"}` 命名；若环境提供 `swarm_pane`，则其与 `narrator` 互斥；若 `swarm{action:"add"}` 返回 `open_board`，首轮回复末行原样贴出最新 `open_board`。
- 同阶段无依赖任务同轮并行 `add`，有依赖等上游，同文件禁并发写。首轮 `add` 后结束本轮等待 `@coordinator` 唤醒，后续轮次用 `swarm_wait{since}` 等待，禁轮询。用 `#seq` 引用前文，需唤醒才用 `@name`。
- Brief 自包含最新需求、验收标准、允许与禁止路径、关键事实、绝对工作区路径与 `cwd`、交付物、指定验证命令或人工判据、默认 10 分钟软期限。
- Brief 写明审查判定、统一向 `@coordinator [done]` 交接、禁二次派活、禁 Git 提交推送、禁顺手重构。额外改动须先报理由获批。
- 干净 Git 用 `GIT_OPTIONAL_LOCKS=0 git status --porcelain` 成功且输出为空、`GIT_OPTIONAL_LOCKS=0 git rev-parse HEAD` 的完整 SHA 和允许路径作为 HEAD 中可读已跟踪文件的基线。可自行采集，无需单派 `scout`。
- 脏 Git 中，已有允许的已跟踪文件记 `shasum -a 1`，或同时附完整 `HEAD` SHA 和能证明当前状态的 `GIT_OPTIONAL_LOCKS=0 git diff HEAD --no-ext-diff --no-textconv` 输出。未跟踪文件（含忽略文件）及非 Git 目录中的已有允许文件统一记 `shasum -a 1`。标记「拟新建」，由 `worker` 写前确认不存在。单机改后升级先披露未提交现场，再采当前态基线。基线缺失、不全或失效时禁止派发写入，`worker` 在实际工作区重核。

## 四、审查门禁、验收、逐块回退、独占提交与清理

- **Review Gate** 派发前判定，验收发现风险可补派。局部、常规或有自动化测试／构建兜底的改动默认不审。高风险、复杂、涉及核心逻辑或接口、安全、并发、迁移、需验证的行为缺少自动验证，或用户要求时，按 `slots.review` 并行派独立只读成员交叉核验。用户显式指定某种外部 CLI 审查时按 `resolve-roles.sh role <id>` 或自定义 `review <id>` 解析派发，显式 `council` 时用 `slots.council`，对抗审查用 `slots.interrogate`。用户显式锁定单一路审查时仅派该路，并在缺少异构覆盖时标 `未审完:`。所有审查均重点防范无运行时证据的防御兜底（多余空值检查、异常吞咽、假想分支、过度包装）。实现与审查均只向 `@coordinator [done]` 交接。
- 按槽位关联的 `reviews.*` 的 `members` 派发，使用解析后的 `memberRoles`。按 `requireDistinctProviders` 和 `minDistinctProviders` 核对实际审查的提供商覆盖，CLI 成员以结果摘要中的生效 `provider` 为准，回退受 `allowSameProviderFallback` 约束。出现缺家或未审完时分别按 `onMissingProvider` 和 `onIncomplete` 处理（见第五节）。复审遵守 `maxRounds` 及下述返工限制。
- 区分「已返回／待验收」「未通过」「已完成」。核验实际 diff、指定命令真实退出码与输出或人工证据。并发写先合入主工作区再复验总 diff，按需派审。Coordinator 汇总审查时逐条判断**是否值得做**，核验运行时证据和真实触发风险，权衡修复收益、引入的复杂度与维护成本。对任一审查方（含显式调用的外部 CLI 审查方）提出的无证据防御兜底或低收益项，直接驳回并记录理由。公共接口或行为边界有分歧时交用户拍板。只派 `worker` 修复有实证且值得做的阻塞项，解决后复验。
- 越界先问理由，按基线逐块回退，禁整文件回退。归属不清则保留并请用户决定。回退后复核改动并重跑验证。
- 非审查收口返工（如同因卡死或未推进）确认停写后，单任务最多重试 1 次，仍失败即停止并上报。非 `council` 审查已有实证推进、只剩经裁决值得修的独立审查局部阻塞项时，单任务另给最多 1 次审查收口返工，不占前述重试额度。收口后仍有任意阻塞项、未推进或复验失败即停止并上报。显式 `council` 多轮评议按 [`../council/SKILL.md`](../council/SKILL.md) 的轮次上限执行，其间未推进或复验失败仍受前述非审查返工 1 次上限约束。两类额度均为单任务累计上限，互不挪用，错误变化、新增阻塞项、刷新基线或切换模型均不重置额度。返工 brief 带当前有效基线。
- Native 返工保持原角色、成员名与工作区，必要时用 `swarm{action:"model", name, model}` 原地切到 `native.reworkModel`，省略时用 `native.model`。
- CLI 返工唤醒原成员并沿用原 `ROLE_ID`。任务若已切到 `fallback`，brief 继续用 `CLI_PROFILE: fallback`，不切回已失败的 profile；否则指定 `CLI_PROFILE: rework`。未配置 `cli.rework` 时，runner 沿用基础 `cli`（`effectiveProfile: default`）。有 `cli.rework` 且 `(ROLE_ID, rework)` 尚未通过会话预检时，先用 `--profile rework` 预检，失败按第二节处理。若生效的 `outerModel` 与当前外层模型不同，Coordinator 用 `swarm{action:"model", name, model}` 原地切换。
- Coordinator 验收后独占 `git add -- <明确路径>` 和 `git commit`。未授权不得 `push`。
- 集成验收后主动 `kill` 无后续任务的完工业务成员，先确认 `waiting`，需提交时等 `git commit` 成功。只读交付消费后可 `kill`。异常、取消或驳回的隔离工作区先披露并确认去留再 `kill`。
- 有 `monitor` 时在最终汇报前向面板发送无 `@` 的 `[done] <summary>`。

## 五、环境阻塞与运行期故障恢复

- 宿主、沙箱、权限或网络等环境阻塞时，外层和 Coordinator 禁止代劳或静默切 Native 绕过限制。上报原始报错、卡点、停写状态、现场残留和选项，等用户拍板。未授权不清理。
- 低配额时，Coordinator 优先用 `swarm{action:"model", name, model}` 原地切到已配置的 `native.lowQuotaFallback` 或当前生效 CLI profile 的 `outerLowQuotaFallback` 并告知用户。CLI profile 未覆写时沿用基础 `cli.outerLowQuotaFallback`，显式 `null` 则清除。外层切换不改变 `ROLE_ID`、内层 profile、CLI 或提供商。Native 审查成员切换前按 `native.lowQuotaFallbackProvider` 核对切换后的覆盖。只有 `allowSameProviderFallback` 明确为真时才可用已由其他成员覆盖的提供商兜底，且仍须满足适用的提供商数量门槛。无法确认或满足覆盖时按缺家处理。
- 缺家时执行 `onMissingProvider`，成员失败、证据不全或未覆盖指定范围时执行 `onIncomplete`。`block` 停下交用户决定，`mark-incomplete` 保留已有获授权结果并标 `未审完:`，说明缺项和原因。两种情况下均不得按完整通过放行，也不得静默替换成员。显式 `council` 另按其[收敛规则](../council/SKILL.md) 执行。
- CLI 内层故障时，先确认进程退出和停写，核对 `GBSTACK_CLI_RESULT_JSON`（`profile`、`effectiveProfile`、`provider`、`exitCode`、`timedOut`、`emptyOutput`、`readonlyViolation`、`changedFiles`、`outOfScopeFiles`）并只读核对残留，如实报告实际退出码与报错。只读审查失败、超时、空输出或违反只读要求，必须标 `未审完:`，严禁外层代审或代写。`changedFiles` 非空时，立即保留现场并报告，禁止任何回退。环境阻塞按本节首条处理。
- 仅当相对基线零改动（`changedFiles` 为空且 diff 为零），并确认是配额、限流、登录失效或服务暂不可用等瞬时故障而非环境阻塞时，Coordinator 才能按顺序回退：① 已配置 `cli.fallback`、当前未在用 `fallback`，且用户未锁定主工具/主模型时，按第二节预检 `--profile fallback`，通过后切换 brief 和 runner 的 profile，并按需切换外层 `outerModel`。② 没有可用的 CLI fallback 时，仅当 `cli.fallbackToNativeOnTransientFailure` 为真、有 `native` 配置且用户未锁定 CLI 执行模式时，才用 `native.model`（返工优先用 `native.reworkModel`）回退。审查回退须满足提供商覆盖和回退约束，否则按缺家或未审完处理。

## 六、外部接口与汇报规范

- 可选外部增强：显式 Orca 调度在已安装 `orchestration` 时遵循该技能，工作树、终端或单向交接在已安装 `orca-cli` 时遵循该技能。未安装相应技能时按常规处理。仅点名才用 `invoke_subagent` / `run_workflow`，仍守基线、预检、审查及验收，无法精确指定模型时告知。
- 进度用状态清单，改动用两列表，方案用横向对比表。派发结束注明「尚未验收」。
- 单轮墙钟 >5 分钟且本轮工具调用 >5 次时，开头各用一句写 `当前需求` / `走过的路` / `现在的状态` / `待你决定`（无则写「无」）。后台等待时间不计。
- 可选外部增强：最终回复在当前环境已安装 `long-answer-html` 时按该技能分流，未安装则按常规回复。

## 七、总入口路由与核心工程底线

### 核心工程底线

1. **需求与最小改动先行。** 重述用户目标与满足它的最小可行改动，优先删减，克制新增抽象与分层。
2. **领域与数据结构先行。** 写逻辑前先定核心类型、状态机、表／注册表与并发共享边界，消除散落布尔值与跨文件隐式假设。
3. **先减后加。** 先删死代码、单调用方包装与孤儿引用，再在更简单的结构上构建。
4. **降低读者认知负担。** 控制调用层数与隐式状态，缩小可变作用域，拆成可验证的小单元逐个推进。
5. **用实证验收。** 用真实运行界面或产物证明有效。编译通过、lint 或单测都不足以独立验收运行行为。
6. **修复根因。** 沿症状回溯到系统首次出错点并修复，无运行时证据支撑的防御兜底一律回滚。
7. **范围内可逆工作自主推进。** 改变公共接口、扩大影响面或执行不可逆操作前，停下请用户拍板。
8. **诚实说 No。** 发现前提错误、需求矛盾或无证据声称时，直接反驳并给出真实判断。

### 按需剧本

匹配任务后，**先读对应剧本再拆任务与写 brief**。初始上下文不全量预读八篇。若八篇均不匹配，按常规 `subagent-first` 拆解。

剧本沿用前六节的分工与门禁。Coordinator 按第二节将剧本中的角色引用解析为槽位，落实写前基线和 Review Gate，并在多路并发写或长耗时多阶段接力时按需启用 `monitor`。验收后由 Coordinator 独占 `git add -- <明确路径>` 与 `git commit`。

| 剧本 | 触发场景 | 路径 |
|---|---|---|
| Bug 修复 | 已有行为出现错误或回归 | [`playbooks/bug-fix.md`](playbooks/bug-fix.md) |
| 功能开发 | 新增能力或改变预期行为 | [`playbooks/feature.md`](playbooks/feature.md) |
| 重构 | 保持行为并调整代码结构 | [`playbooks/refactoring.md`](playbooks/refactoring.md) |
| 调研 | 只读查证结构、运行链路或设计动机 | [`playbooks/investigation.md`](playbooks/investigation.md) |
| PR 讲解与审查 | 讲解 PR 做了什么（how/why）并审查 bug 与改进点 | [`playbooks/pr-review.md`](playbooks/pr-review.md) |
| 技能编写 | 在任意仓库或插件中编写或修订 `SKILL.md` | [`playbooks/authoring-a-skill.md`](playbooks/authoring-a-skill.md) |
| 性能问题 | 定位性能瓶颈或验证性能优化 | [`playbooks/perf-issue.md`](playbooks/perf-issue.md) |
| 原型 | 用一次性实验比较候选设计 | [`playbooks/prototype.md`](playbooks/prototype.md) |

### 独立高频技能速查

以下技能可在单代理模式独立触发，也可按需在 Swarm brief 中按名称或路径引用。

| 技能 | 定位 | 路径 |
|---|---|---|
| `how` | 代码结构与运行链路追踪 | [`../how/SKILL.md`](../how/SKILL.md) |
| `why` | Git 历史与设计动机溯源 | [`../why/SKILL.md`](../why/SKILL.md) |
| `teach` | 面向人的系统讲解与概念梳理 | [`../teach/SKILL.md`](../teach/SKILL.md) |
| `architect` | 跨边界模块定型与接口设计 | [`../architect/SKILL.md`](../architect/SKILL.md) |
| `blast-radius` | 变更影响面与依赖波及分析 | [`../blast-radius/SKILL.md`](../blast-radius/SKILL.md) |
| `interrogate` | 有争议设计的对抗式拷问与收敛 | [`../interrogate/SKILL.md`](../interrogate/SKILL.md) |
| `unslop` | 散文、报告与回复去水精炼 | [`../unslop/SKILL.md`](../unslop/SKILL.md) |
| `ketchup` | 长会话或中断后快速上下文追赶 | [`../ketchup/SKILL.md`](../ketchup/SKILL.md) |
| `correct` | 纠偏与错误前提反驳后的路径修正 | [`../correct/SKILL.md`](../correct/SKILL.md) |
| `sharpen` | 需求、指令与验收标准的精准化打磨 | [`../sharpen/SKILL.md`](../sharpen/SKILL.md) |
| `council` | 三家异构模型多轮评审与收敛 | [`../council/SKILL.md`](../council/SKILL.md) |

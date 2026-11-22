---
name: subagent-first
description: >-
  顶层 Coordinator 的 Swarm 协调手册，涵盖授权模型、Codex 预检、写前基线、审查验收与故障恢复。
---

# Swarm 协调规程

## 一、Coordinator 职责与单一调度权

- 仅顶层 Coordinator 拆解、派发、验收与集成。进入 Swarm 后不遍读全仓、不代写业务代码。
- Swarm 成员（含 `monitor`、`narrator`）、`invoke_subagent` 子代理、`run_workflow` 的 `agent()` 步骤和 Orca worker 直接执行自身任务，严禁二次派活（含 `swarm{action:"add"}`）。
- 汇报先说现状和结论，保留阻塞与待决项。已定范围内的细节自主处理，扩大目标或影响面、改变公共接口或技术取舍交用户拍板。

## 二、授权模型矩阵与 Codex 会话级预检

下表是唯一授权矩阵，覆盖 Swarm 默认跟随协调者模型的限制。

| 角色 | 默认／指定模型 | Gemini 低配额原地切换 | 工作区与触发条件 |
|---|---|---|---|
| `Coordinator` | 会话默认 `gemini-4-hydrogen-high` | — | 主工作区 |
| `scout` | `gemini-4-hydrogen-medium` | — | `inherit` |
| `plan` | `gemini-4-neon-xhigh` | `claude-opus-5-5-high` | 原生，`inherit` |
| `worker` | Codex 外层 `gemini-4-hydrogen-medium`，Native 首轮 `gemini-4-hydrogen-high`，Native 返工原地切 `gemini-4-neon-high` | `claude-sonnet-5-5-high` | 单写 `inherit`，并发写 `share` / `branch`；Native 返工沿用原成员名与 `agents/worker.md` |
| `reviewer` | Codex 外层 `gemini-4-hydrogen-medium`，Native `gemini-4-neon-xhigh` | Codex 外层切 `claude-sonnet-5-5-high`，Native 切 `claude-opus-5-5-high` | `inherit` |
| `monitor` | `gemini-3.8-flash-low` | — | `inherit`，仅多路并发写或长耗时多阶段接力时按需启用 |

- Codex profile 用 `worker-codex`（常规）、`worker-codex-xhigh`（复杂、高风险或返工）或 `reviewer-codex`（`gpt-6.2-astra` xhigh，只读）。
- 派发 `worker` / `reviewer` 前只读预检。根据当前加载的 `subagent-first` `SKILL.md` 的实际路径向上两级（从所在目录解析 `../..`）取得绝对路径 `<PLUGIN_ROOT>`，运行 `<PLUGIN_ROOT>/scripts/check-codex.sh <profile>`，或做等效的只读检查。
- 等效检查先用 `command -v codex` 找可执行文件，找不到可执行文件时尝试 `$HOME/.local/bin/codex`。两处均无可执行文件时明确报告并停止本次派发（脚本退出码 `127`）。`codex login status` 须退出 0 且输出含 `Logged in`，所需 profile 须通过 `test -f` 和 `test -r` 检查 `${CODEX_HOME:-$HOME/.codex}/<profile>.config.toml`。在只读环境中执行等效检查，或像随附脚本一样为 Codex 发布版的登录状态查询将 `TMPDIR` 临时设为 Codex home 的物理绝对路径，以阻止创建 PATH 别名。
- 预检通过后同会话缓存，仅切未检 profile、报错或环境变化时重检。
- 默认预检通过走 Codex。登录或 profile 检查失败时报告具体缺项并走 Native，环境阻塞按第五节处理。显式 Native 或指定原生模型时跳过预检。显式 Codex 预检失败即停报具体缺项，严禁静默回退。
- 角色文件相对于当前技能目录为 `../../agents/<role>.md`。在 Swarm 业务 brief 开头写入解析后的绝对路径 `<PLUGIN_ROOT>/agents/<role>.md` 并要求成员先读该文件。实现与审查填 `执行模式: Codex` 或 `执行模式: Native`，Codex 另填 profile 与是否为非 Git 目录，隔离写先 `pwd` 取 `-C` 路径。
- 派发时显式传 `model`（若指定型号不可用，先用 `swarm{action:"models"}` 核对并上报用户确认，禁擅自改派未授权型号；低配额切换按第五节执行）。成员名使用小写 `[a-z0-9_-]`，最长 24 字符。

## 三、并行协作、轻量基线与自包含 Brief

- 开局用 `swarm{action:"title"}` 命名；若环境提供 `swarm_pane`，则其与 `narrator` 互斥；若 `swarm{action:"add"}` 返回 `open_board`，首轮回复末行原样贴出最新 `open_board`。
- 同阶段无依赖任务同轮并行 `add`，有依赖等上游，同文件禁并发写。首轮 `add` 后结束本轮等待 `@coordinator` 唤醒，后续轮次用 `swarm_wait{since}` 等待，禁轮询。用 `#seq` 引用前文，需唤醒才用 `@name`。
- Brief 自包含最新需求、验收标准、允许与禁止路径、关键事实、绝对工作区路径与 `cwd`、交付物、指定验证命令或人工判据、默认 10 分钟软期限。
- Brief 写明审查判定、统一向 `@coordinator [done]` 交接、禁二次派活、禁 Git 提交推送、禁顺手重构。额外改动须先报理由获批。
- 干净 Git 用 `GIT_OPTIONAL_LOCKS=0 git status --porcelain` 成功且输出为空、`GIT_OPTIONAL_LOCKS=0 git rev-parse HEAD` 的完整 SHA 和允许路径作为 HEAD 中可读已跟踪文件的基线。可自行采集，无需单派 `scout`。
- 脏 Git 中，已有允许的已跟踪文件记 `shasum -a 1`，或同时附完整 `HEAD` SHA 和能证明当前状态的 `GIT_OPTIONAL_LOCKS=0 git diff HEAD --no-ext-diff --no-textconv` 输出。未跟踪文件（含忽略文件）及非 Git 目录中的已有允许文件统一记 `shasum -a 1`。标记「拟新建」，由 `worker` 写前确认不存在。单机改后升级先披露未提交现场，再采当前态基线。基线缺失、不全或失效时禁止派发写入，`worker` 在实际工作区重核。

## 四、审查门禁、验收、逐块回退、独占提交与清理

- **Review Gate** 派发前判定，验收发现风险可补派。局部、常规或有自动化测试／构建兜底的改动默认不审。高风险、复杂、涉及核心逻辑或接口、安全、并发、迁移，需验证行为缺少自动验证，或用户要求时派独立 `reviewer`。实现与审查均只向 `@coordinator [done]` 交接。
- 区分「已返回／待验收」「未通过」「已完成」。核验实际 diff、指定命令真实退出码与输出或人工证据。并发写先合入主工作区再复验总 diff，按需派审。审查阻塞项须全部解决。
- 越界先问理由，按基线逐块回退，禁整文件回退。归属不清则保留并请用户决定。回退后复核改动并重跑验证。
- 确认停写后单任务最多重试 1 次，仍失败则上报。返工 brief 带当前有效基线。Native 用 `swarm{action:"model", name, model:"gemini-4-neon-high"}` 原地切换并沿用 `worker.md`；Codex 唤醒原 `worker` 并切 `worker-codex-xhigh`。
- Coordinator 验收后独占 `git add -- <明确路径>` 和 `git commit`。未授权不得 `push`。
- 集成验收后主动 `kill` 无后续任务的完工业务成员，先确认 `waiting`，需提交时等 `git commit` 成功。只读交付消费后可 `kill`。异常、取消或驳回的隔离工作区先披露并确认去留再 `kill`。
- 有 `monitor` 时在最终汇报前向面板发送无 `@` 的 `[done] <summary>`。

## 五、环境阻塞与运行期故障恢复

- 宿主、沙箱、权限或网络等环境阻塞时，外层和 Coordinator 禁止代劳或静默切 Native 绕过限制。上报原始报错、卡点、停写状态、现场残留和选项，等用户拍板。未授权不清理。
- Gemini 低配额按矩阵用 `swarm{action:"model", name, model}` 原地切换并告知用户，Codex profile 沿用。
- Codex 内层故障先确认进程退出、停写，只读核对残留并报告退出码及报错。显式锁定 Codex 时保留现场上报。默认路由仅在配额、限流、登录失效或服务暂不可用且确认退出后允许 `reviewer` 切 Native；`worker` 还须确认相对基线零改动。有部分改动则保留现场上报。切 Native 时须同步按矩阵切换原生模型，`reviewer` 用 `gemini-4-neon-xhigh`，`worker` 用 `gemini-4-hydrogen-high`。Gemini 低配额时按矩阵切换对应的 Native Claude 模型。

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

匹配任务后，**先读对应剧本再拆任务与写 brief**。初始上下文不全量预读七篇。若七篇均不匹配，按常规 `subagent-first` 拆解。

剧本沿用前六节的分工与门禁。Coordinator 按需派 `scout` 与 `plan`，为 `worker` 和 `reviewer` 按矩阵选 Codex／Native，落实写前基线和 Review Gate，并在多路并发写或长耗时多阶段接力时按需启用 `monitor`。验收后由 Coordinator 独占 `git add -- <明确路径>` 与 `git commit`。

| 剧本 | 触发场景 | 路径 |
|---|---|---|
| Bug 修复 | 已有行为出现错误或回归 | [`playbooks/bug-fix.md`](playbooks/bug-fix.md) |
| 功能开发 | 新增能力或改变预期行为 | [`playbooks/feature.md`](playbooks/feature.md) |
| 重构 | 保持行为并调整代码结构 | [`playbooks/refactoring.md`](playbooks/refactoring.md) |
| 调研 | 只读查证结构、运行链路或设计动机 | [`playbooks/investigation.md`](playbooks/investigation.md) |
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

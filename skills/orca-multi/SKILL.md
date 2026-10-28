---
name: orca-multi
description: 用 Orca 调度 Codex 等执行 agent 自动完成开发任务。用户说“orca-multi <任务>”、要求把任务派给子 agent / 多 agent 并行 / 用 Orca 执行时使用。本会话作为调度端：拆任务、写合同、自动启动执行、校验、打回或验收，最后经用户确认合并。
---

# orca-multi

本会话是**调度端**：理解任务、拆分、写合同、派工、验收，不亲自写业务代码。
执行 agent 只通过 Orca 启动，终端在 Orca 里人工可见。命令行工具：

```bash
OM=~/.claude/skills/orca-multi/scripts/orca-multi   # 在项目仓库目录里运行
```

## 用户说「orca-multi <任务>」时，按下面自动执行

1. **理解任务**：读相关代码、项目规则（AGENTS.md / CLAUDE.md）和当前 `git status`。需求、权限或数据口径不清楚时先问用户，不要猜。
2. **拆分**：
   - 小任务不拆，一个合同。
   - 互不依赖、文件范围不重叠的部分拆成多个任务并行。
   - 有先后依赖的，后一个任务在合同里写 `reuse: <前一个任务号>`，复用同一工作区和终端。
3. **写合同**：每个任务写一份 `<runs_dir>/<任务号>/task.md`，格式见 `templates/task.md`。
   - `runs_dir`：项目有 `.ai-dev/` 时为 `.ai-dev/runs`，否则为 `.orca-multi/runs`。
   - `allowed_files` 要写精确；只读任务（调研、评审）写 `allowed_files: []`。
   - `verify` 写能自动判断成败的命令，只写当前仓库真实存在、确实需要的检查。
   - 任务号格式：`T-YYYYMMDD-简短英文名`。
   - 推理强度按任务大小定：小改动写 `tier: FAST`（low），一般任务不写（medium），跨前后端、改规则或权限的写 `tier: HEAVY`（high）。合同里写 `effort` 时优先于 `tier`。
   - 参考材料（原型、截图等）放在同一任务目录里，新 worktree 启动时会一起复制过去。
4. **检查合同、看计划**：
   - `$OM lint <任务号...>`：启发式检查，提示“引用了被改文件、但没写进 allowed_files 的测试”和“verify 里写到、但不在 allowed_files 里的文件”（按相关度只列前 12 个）。看到真正会受影响的测试就补进 `allowed_files`，测试目录可用通配（如 `backend/tests/worklog_api/**`）。只提示不拦截，加 `--strict` 时有提示则退出码为 1。
   - `$OM baseline <任务号...>`（建议用于后端/前端改动任务）：在主工作区当前提交上先跑一遍 verify，记录“改动前就失败”的命令；之后 `run` 会把它们写进派工说明，执行 agent 不用自己踩一遍。主工作区有进行中的任务时拒绝运行；会产生构建产物，耗时等于 verify 的耗时；引用本任务将新建文件的命令在基线里失败属正常。
   - `$OM plan <任务号...>`，确认每个任务用哪个工作区。规则见下文“工作区怎么选”。
5. **自动执行**：运行 `$OM run <任务号...>`。命令放后台跑，跑完会通知，期间不要反复查看。同一时间只能有一个 `run` 在等消息（Orca 限制）：已有 run 在跑时又要派新任务，先等它结束，再用一个 run 带上全部进行中的任务号；否则后启动的 run 会以退出码 2 结束（任务已启动，不受影响）。它会依次：
   - 先把主工作区任务目录里后来新增或改动的附件同步到仍在工作的 worktree（创建时只复制一次，之后靠这一步和 `reply` 时的同步）；
   - 有名额就启动，名额满了就排队；**已结束（done）或已放弃的任务不会被重新启动**：对它们再 `run` 只提示并以退出码 1 结束，确需重做（例如放弃后改了合同重来）加 `--redo`；检查进度只用 `status`，不要对已完成的任务再跑 `run`；
   - 等执行 agent 回报；
   - 校验改动范围（guard），再跑合同里的 verify；
   - 不通过就把原因自动发回同一个 agent 修改（默认最多 2 轮）；通过则验收。
6. **处理 run 的结果**：
   - 退出码 `0`：全部验收。
   - 退出码 `3`：执行 agent 提了新问题。退出前，同一批消息里其他任务的交付、验收和排队启动都已处理完；提问已记入 `<runs_dir>/.asked.json`。调度端能答就 `$OM reply <消息ID> "回答"`，然后再次 `run`；涉及需求决定的，先问用户。已上报、尚未回答的提问不会让下一次 `run` 再退出（只提示），所以可以先继续 `run`、稍后再答。
   - 要改进行中任务的合同（扩 allowed_files、加 verify）时，用 `$OM amend <任务号> --allow <glob> --verify-add "<命令>" --note "原因"`：同步主工作区与 worktree 两份合同并通知执行 agent，不要手工改两份。
   - 退出码 `1`：有任务需人工处理（越界、主工作区被改、打回轮数用完、agent 报失败）。把原因告诉用户，按用户决定 `reject`、`abandon` 或修改合同后重来。
   - 退出码 `2`：等待超时。这只是检查点，再次 `run` 继续。
7. **复核**：读每个已验收任务的 report.md 和真实改动，确认符合目标，不能只看命令通过。
8. **收尾**：向用户汇报，分清已完成、已验证、未验证、需人工处理。
   - worktree 任务：经用户同意后才 `$OM finish <任务号> --message "<英文提交信息>" --yes`，会提交、`--no-ff` 合并、回收 worktree，然后在干净的临时目录里用合并后的代码重跑 verify（不受主工作区未提交改动影响；失败只报告不回退）。
   - 主工作区任务：`$OM finish <任务号> --yes` 只结束任务、释放终端，改动保持未提交。
9. **回收**：`$OM gc` 列出残留，`$OM gc --apply` 回收已结束任务的 worktree。

## 工作区怎么选（`plan` / `run` 自动决定）

- 主工作区同一时间只给一个任务用；主工作区任务交付后（待验收/已验收）也仍占用，直到 `finish` 结束才释放。
- 只读任务：主工作区空闲就在主工作区做，否则新建 worktree。
- 可写任务：同时满足下面三条才在主工作区做，否则新建 worktree：
  1. 主工作区空闲；
  2. `allowed_files` 不碰主工作区里已有的未提交改动；
  3. 文件范围不和其他进行中的任务重叠。
- 合同里可以用 `workspace: main | worktree` 强制指定。
- 新 worktree 由 Orca 自己创建（`orca worktree create`），位于 Orca 的工作区目录（如 `~/orca/workspaces/<项目>/<任务号>`），分支名为任务号，创建即登记、挂为当前项目子节点，在侧边栏可见。不要再用 `git worktree add` 自建到仓库内 `.worktrees/`：那样 Orca 登记有延迟（`selector_not_found`），且侧边栏看不到。依赖目录（node_modules、.venv 等）用符号链接共享。
- 复用（`reuse`）链上前序任务的过程文件（`<runs_dir>/**`）不算越界、不进提交；`finish` 时拷回主工作区留档。
- 已验收后若调度端又调整了 worktree 文件，重新 `guard` + `verify` 后可再次 `accept`。
- worktree 任务的 guard 不把主工作区 HEAD 沿同一分支前进（调度端合并其他任务、提交调度文档）算作越界；只有历史被改写或切换分支才拦下。
- `finish` 合并主工作区时加锁（`<runs_dir>/.merge.lock`），并发的 finish 会排队，避免两次合并交错。
- `run` 会重新启动已 `abandon` 的任务；等待时一并签收心跳，避免心跳积压反复提醒协调会话。
- 合并后验收失败时，完整输出存在 `<runs_dir>/<任务号>/evidence/post-merge-verify-<n>.out`。
- 硬上限：worktree 最多 5 个，同时运行的 agent 最多 4 个。超出就排队，不会多建。

## 手动入口（需要人工干预时用）

`lint`、`baseline`、`status`、`view <id> [--open]`（看终端）、`send <id> "指令"`、`amend <id> --allow/--verify-add/--note`（改合同并通知）、`reply`、`wait`、`guard`、`verify`、`accept`、`reject <id> "原因"`、`finish`、`abandon <id> --yes`（worktree 任务先存补丁再丢弃；主工作区任务不自动还原文件）、`gc`。

## 项目配置（可选）

`<项目>/.orca-multi/config.json`，已自动加入 `.git/info/exclude`，不进提交。可覆盖的字段：

| 字段 | 含义 |
|---|---|
| `max_worktrees` | worktree 上限 |
| `max_active_workers` | 同时运行的 agent 上限 |
| `workspace` | 默认工作区：auto / main / worktree |
| `agent`、`model`、`effort` | 执行 agent 及其模型、推理强度（默认 codex、gpt-6.1-sol、medium）。只有 claude/codex/cursor/antigravity/muse 接受 model+effort，omp 只接受 model；zcode 等其他 agent 使用自身配置的模型（如在 zcode 里用 `/model` 切到 GLM-5.3-Flash），合同里的 model/effort 会被忽略并提示。**已知限制（2026-10-23）**：当前 Orca 对 zcode 的就绪检测超时（`worker-show` 显示 `failed / agent_readiness`），新起与附加终端两种方式都无法注入任务说明，暂勿把任务派给 zcode |
| `link_paths` | 用符号链接共享给 worktree 的依赖目录 |
| `max_fix_rounds` | 自动打回的最多轮数 |
| `runs_dir` | 合同与证据目录 |
| `agent_notes` | 项目特有说明，原样附在派工说明末尾 |

## 在 worktree 里做浏览器预览
- worktree 的依赖目录是指向主仓库的符号链接。Vite 默认不允许读取项目根目录以外的文件，字体、图标等资源会返回 403，页面看起来像“图标坏了”。
- 预览时用一份放在仓库外的临时配置，在 `server.fs.allow` 里加入主仓库的 `node_modules` 路径。不要把这份配置写进 worktree，否则 guard 会把它算作任务改动。

## 风险与边界

- Orca 启动的 Codex 是 `danger-full-access` 且不需要审批（用户已于 2026-10-21 接受）。guard 能发现仓库内越出范围的写入，仓库之外的写入发现不了。
- 合同里不要写密钥、数据库凭据等敏感信息。
- 提交、合并、推送都需要用户明确同意；本工具从不推送。
- `main`、`master` 是受保护分支，只能由用户手动提交：主工作区在这两个分支上时，`finish` 直接拒绝合并。
- 测试：`python3 -m unittest discover -s ~/.claude/skills/orca-multi/scripts`

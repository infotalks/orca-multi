# 变更记录

## 2026-10-28：按 DeptAdmin 调度复盘增加 lint、baseline、附件同步和重启保护

来源：DeptAdmin 项目一天内连续调度 4 个任务的复盘：全部一次验收通过、0 次打回，但执行 agent 共提问 6 次（其中 5 次是合同缺口或工具没同步附件，1 次是合同没写清共用模式下后端怎么启动）；另有 1 次对已完成任务误执行 `run`，把它们重新启动。

涉及文件：`skills/orca-multi/scripts/orca_multi.py`、`skills/orca-multi/scripts/test_orca_multi.py`（新增 11 个测试，共 43 个通过）、`skills/orca-multi/SKILL.md`。

### 1. 合同检查 `lint`（解决漏列受影响的测试和夹具，2 次提问）

- **现象**：`allowed_files` 只列了要改的源文件和新增测试，没列“因这次改动会失败的现有测试和夹具”，执行 agent 跑到失败后只能提问等批准。
- **新增**：`lint <任务号...>`，启发式提示两类问题：allowed_files 里的源文件在测试文件里被引用、但测试文件不在 allowed_files 里（按引用相关度排序，只列前 12 个，其余报数量）；verify 命令里写到的文件路径既不存在、也不在 allowed_files 里（识别 `cd 目录 &&` 前缀）。忽略过短或过于通用的文件名，不扫描运行目录里的历史文件。只提示不拦截，`--strict` 时有提示退出码为 1。
- **局限**：按名字匹配，有噪音，也找不全动态拼接路径的引用。
- **测试**：`test_lint_flags_unlisted_tests_and_verify_paths`、`test_lint_resolves_cd_prefix_ignores_runs_dir_and_caps_results`、`test_lint_ignores_generic_and_short_stems`。

### 2. 基线 `baseline`（解决改动前就存在的失败，1 次提问）

- **现象**：仓库里早已存在的测试失败（夹具缺口），执行 agent 要跑完整个红灯才发现，并提问确认是不是自己引入的。
- **新增**：`baseline <任务号...>` 在主工作区当前提交上先跑一遍合同 verify，结果写入任务目录的 `baseline.json`（不复制给执行 agent）。之后 `run` 启动任务时把“改动前就失败的命令及首个失败原因”写进派工说明；全部通过也会说明。每条命令可设超时（默认 1800 秒，超时记为失败）。主工作区有进行中的任务时拒绝运行。引用本任务将新建文件的命令在基线里失败属正常，派工说明里已注明。
- **测试**：`test_baseline_records_failures_and_goes_into_spec`、`test_baseline_all_pass_is_noted_and_not_copied_to_worktree`、`test_baseline_refuses_when_main_workspace_busy`、`test_baseline_timeout_is_recorded_as_failure`。

### 3. 任务目录附件同步（解决参考材料到不了 worktree，2 次提问）

- **根因**：worktree 创建时只复制一次任务目录；之后放进主工作区任务目录的参考材料不会到达执行 agent。
- **修复**：新增 `sync_task_files`，每次 `run` 开始时、每次 `reply` 之后，把主工作区任务目录里新增或有改动的附件同步到仍在工作的任务的 worktree；调度端自己的文件（state、snapshot、guard、verify、report、baseline）不同步；内容相同则不重复写。
- **测试**：`test_task_dir_attachments_sync_to_worktree_on_run_and_reply`、`test_sync_noop_for_main_workspace_task`。

### 4. 已结束或已放弃的任务不再自动重启（解决误操作）

- **根因**：`auto_run` 把已放弃（abandoned）的任务视为需要重新启动，`spawn` 也允许重派 done 任务。对已完成并回收的任务再跑 `run`（例如想“检查进度”）会新建 worktree 并启动执行 agent。
- **修复**：默认不重启：`run` 对这类任务只提示并以退出码 1 结束，`spawn` 抛出错误；需要重做时显式加 `--redo`（`run` 和 `spawn` 都支持）。检查进度只用 `status`。
- **测试**：`test_run_respawns_abandoned_task`（改为先断言默认不重启，再断言 `--redo` 重启）、`test_spawn_done_task_needs_redo`。

### 5. 派工提示词补充三句

- 汇报文件 `report.md` 和任务目录是 allowed_files 的唯一例外。
- verify 里的命令与规则冲突（如安装依赖、改写仓库文件）时不要执行，用 `ask` 提问；允许用只读的 `git status` / `git diff` 检查改动范围。
- 不要安装依赖，也不要修改依赖目录（node_modules、.venv 等，可能是指向主仓库的软链接）。
- **测试**：`test_spec_states_report_exception_dependency_ban_and_verify_conflict_rule`。

### 评审结论（未采纳的部分）

外部对派工提示词的评审认为“把越权检查、验收、成功判定交给 Agent 自己”。核对源码后不成立：`guard` 对比任务启动快照，`verify` 由调度端自己在 worktree 里重跑合同命令，验收结果绑定候选文件哈希，Worker 的 `worker_done` 只触发验收。因此没有整体替换提示词，也没有把心跳、记录初始 Git 状态等职责交给 Agent。

### 备注

- 之前误以为“run 遇到提问退出码是 0”，是调用时接了 `| tail`，管道吞掉了真实退出码；退出码 3 的行为没有问题。
- 已知未解决：对已过期的旧提问执行 `reply` 时 Orca 会拒绝，工具在回复成功后才记录“已回答”，所以这类提问会一直显示为“仍有 N 个提问未回答”。尚无忽略命令。

## 2026-10-23（三）：zcode 实测结果与 reject 参数修正

- **实测**：在 AIOps 项目首次把真实任务派给 zcode（v3.14.4 和升级后的 v3.16.2，模型 bigmodel/GLM-5.3-Flash）。`worker-start --agent zcode` 与“先有 zcode 终端、再 `worker-start --terminal` 附加”两种方式，Orca 均在 `agent_readiness` 阶段超时（`worker-show`：`failed / agent_readiness / lastFailure=timeout`）。zcode 界面已就绪，但任务说明从未注入（`session 0 tokens`）。结论：当前 Orca 的 zcode 就绪检测与 zcode 界面不匹配，需 Orca/zcode 侧修复；在此之前不要把任务派给 zcode。已写入 SKILL.md 的已知限制。
- **暴露的 orca-multi 缺口**：Orca 已把派发判为 failed，orca-multi 仍显示 running（复盘 #4：启动后不探活），需人工 `worker-show` 才发现。留待 #4 修复。
- **修复**：`reject` 在原终端不可用、需另起新 agent 时，原先固定传 `--model/--effort`；改为复用 `agent_launch_args`，按 agent 决定参数（状态中“<agent> 自身配置”不再当作模型名传出）。测试 `test_reject_fresh_agent_uses_agent_specific_args`；共 32 个测试通过。
- **相关环境发现**：本机 `codex` 是 shell 函数（`CODEX_HOME=$HOME/.codex-cli`），Orca 启动的 Codex 读取 `~/.codex-cli/config.toml`。其中 `service_tier = "fast"` 导致执行端带 Fast；Orca 无法按次覆盖 service tier，只能改该配置。

## 2026-10-23（二）：支持 zcode 作为执行 agent

- **问题**：`spawn` 对所有 agent 固定传 `--agent <agent> --model <model> --effort <effort>`。Orca `worker-start` 只对 claude/codex/cursor/antigravity/muse 接受 `--model/--effort`，omp 只接受 `--model`，zcode 等其他 agent 使用自身配置的模型（`orca orchestration worker-start --help` 原文：“Other agents, including zcode, launch with the model from their own config”）。直接把 `agent` 设为 zcode，派工命令会带上不被支持的参数。
- **修复**：新增 `agent_launch_args`，按 agent 决定参数——上述五个传 model+effort，omp 只传 model，其余只传 `--agent`。状态里的 model 记为“<agent> 自身配置”、effort 记为空。合同显式写了 model/effort 而 agent 不支持时，提示参数被忽略。
- **用法**：合同写 `agent: zcode`，或在项目配置 `.orca-multi/config.json` 里设 `"agent": "zcode"`；模型在 zcode 内切换（如 `/model` 选 GLM-5.3-Flash）。同一批任务可以按合同分别指定 codex 或 zcode。
- **测试**：`test_agent_launch_args_by_agent`、`test_zcode_worker_start_omits_model_and_effort`；共 31 个测试通过，这 2 个新测试在修改前的脚本上都失败。
- **尚未验证**：还没有用真实 zcode 派过工，需在首次实际派工时确认 zcode 能正常启动，并按 orca-multi 的派工说明回报 `worker_done`/提问。

## 2026-10-23：按调度复盘（RETRO_20261023.md）修复 P0/P1 问题

来源：同一项目又连续调度约 20 个任务后的复盘。每条的现象、次数和根因见 `RETRO_20261023.md` 对应编号。

涉及文件：`skills/orca-multi/scripts/orca_multi.py`、`skills/orca-multi/scripts/test_orca_multi.py`（新增 6 个测试）、`skills/orca-multi/SKILL.md`。

### 1. 提问让整个 `run` 立刻退出（复盘 #1，≥37 次）

- **根因**：`auto_run` 收到 question/escalation 后直接 `return 3`，同一批里其他任务的验收和排队启动都没处理，整批消息也没签收，下次会重复投递。
- **修复**：先处理同一批消息里的 `worker_done`，把新提问写入 `<runs_dir>/.asked.json` 后签收整批，再执行一轮排队启动和待验收评估，最后才返回 3。已上报、尚未回答的提问不再导致后续 `run` 退出，只在启动时提示。
- **取舍**：仍保留 exit 3，因为调度端在后台运行 `run` 时，靠进程退出得知有新提问；改动目标是退出前不再丢下其他任务。
- **测试**：`test_question_still_processes_other_deliveries_and_is_reported_once`。

### 2. 调度端自己合并导致的 HEAD 前移被判“需人工”（复盘 #2，≥9 次）

- **根因**：worktree 任务的 guard 把主工作区快照里的 `HEAD` 也算作主工作区变化。
- **修复**：主工作区 HEAD 变化时，如果任务启动时的提交仍是当前 HEAD 的祖先（同一分支向前推进），就不算异常；只有历史被改写或切换了分支才拦下。主工作区未提交文件的检查不变。
- **测试**：`test_worktree_guard_accepts_main_fast_forward_but_flags_rewrite`。

### 3. 中途修改合同要手工改两份（复盘 #3，≥10 次）

- **修复**：新增 `amend <id> --allow <glob> --verify-add <cmd> --note <说明>`：校验后同时写主工作区和 worktree 两份合同，状态里记录 `contract_version`，任务在运行中时向执行 agent 发送变更说明。`key: []` 会被展开成列表，已存在的条目跳过。
- **测试**：`test_amend_updates_both_contract_copies_and_notifies_agent`、`test_add_list_items_expands_empty_list`。

### 4. `run` 不会重新启动已放弃的任务（复盘 #5）

- **根因**：`pending` 只包含没有状态文件的任务，已放弃任务的状态是 `done`。
- **修复**：`pending` 也包含 `abandoned` 的任务（`spawn` 本来就支持重派 `done` 任务）。
- **测试**：`test_run_respawns_abandoned_task`。

### 5. 并发 `finish` 没有互斥（复盘 #6）

- **修复**：worktree 任务 `finish --yes` 时对 `<runs_dir>/.merge.lock` 加排他锁（`fcntl.flock`），覆盖合并和合并后验收。另一个 finish 正在进行时会提示并排队等待；拿到锁后重新读取任务状态。
- **测试**：`test_merge_lock_is_exclusive`。

### 6. 心跳积压反复提醒协调会话（复盘 #8）

- **根因**：`check --wait --types` 只决定什么消息能唤醒等待，返回的总是整批 FIFO 消息。心跳不在唤醒类型里，因此积压到有其他消息时才被签收，其间 Orca 一直提示协调会话有未读消息。
- **修复**：唤醒类型加入 `heartbeat`，`run` 收到即签收。

### 验证

- 全部 29 个测试通过（`python3 -m unittest discover -s skills/orca-multi/scripts`）。
- 新增的 6 个测试放到修复前的脚本上运行全部失败（3 个断言失败、3 个报错），说明确实能拦住对应问题。
- **尚未验证**：以上修复还没在真实 Orca 调度中运行过；`heartbeat` 作为唤醒类型的实际效果，以及签收含提问的批次后 `reply` 是否仍正常，需在下一次真实派工中确认。

### 未在本次处理（复盘 P2/P3）

启动探活与自动重派（#4）、项目级 `verify_vars` / `doctor` / 合同静态检查（#7）、执行模型授权写入配置（#9）、主工作区任务 `release` 语义（#10）。

## 2026-10-22：worktree 改由 Orca 原生创建，并修复调度中暴露的 4 个缺陷

来源：在 AIOps 项目（`dev/pg1-continue`）用 orca-multi 连续调度约 20 个任务时实际遇到的问题。每条都附现象、根因、修复和对应测试。

涉及文件：`skills/orca-multi/scripts/orca_multi.py`（+54/−16）、`skills/orca-multi/scripts/test_orca_multi.py`（新增 4 个测试，更新 1 个）、`skills/orca-multi/SKILL.md`。

### 1. 执行 agent 的 worktree 在 Orca 侧边栏看不到；派工频繁报 `selector_not_found`（主问题）

- **现象**
  - 用户在 Orca 侧边栏只能看到主工作区，看不到执行 agent 所在的 worktree 和终端，误以为 agent 没有通过 Orca 启动。
  - 派新任务时 `run` 多次以 `orca worktree set --worktree path:... 失败: selector_not_found` 退出，需要反复重跑。
- **根因**：旧实现先用 `git worktree add` 把 worktree 建到仓库内的隐藏目录 `.worktrees/<任务号>`，再调用 `orca worktree set` 补登记，有两个后果：
  1. Orca 发现新目录有延迟，补登记发得太早就会失败（竞态）；
  2. 这种自建、补登记的 worktree（lineage `origin: manual`，位于仓库内的隐藏目录）不在侧边栏显示。
- **验证**：用 `orca worktree create` 原生建了一个对照 worktree（`om-probe-20261022`），用户确认它在侧边栏可见，而 skill 自建的 worktree 都不可见。另外实测：`--base-branch` 可直接接收提交 SHA；`orca worktree rm` 会同时删除 worktree 和分支。
- **修复**：`spawn` 改为调用 `orca worktree create --repo path:<项目> --name <任务号> --base-branch <基线> --parent-worktree path:<项目> --setup skip`，worktree 路径和分支名以 Orca 返回值为准（如 `~/orca/workspaces/<项目>/<任务号>`，分支名为任务号）。创建即登记，不再有竞态。状态里记录 `worktree_owner: "orca"`；数量上限统计（`managed_worktrees`）和删除前的安全检查（`cleanup`）都认这类 worktree，旧版 `.worktrees/` 下的 worktree 仍然兼容。
- **测试**：`test_dirty_overlap_uses_worktree_then_merge` 改为断言 worktree 由 `worktree create` 创建、位于 Orca 工作区目录、记录为 Orca 所有；测试里的假 orca 新增了 `worktree create` 实现。

### 2. 两个任务先后进入主工作区，互相把对方的文件算作越界

- **现象**：pp02 在主工作区交付（`awaiting_accept`）后，下一个任务 pp11 也被分到了主工作区。pp02 的 guard 把 pp11 新建的文件判为越界，pp02 因此无法正常验收，只能放弃后人工复核。
- **根因**：判断"主工作区是否空闲"时只看 `running` 状态的任务。主工作区任务交付后，改动仍未提交，guard 依据的是主工作区指纹，此时主工作区并没有真正空出来。
- **修复**：主工作区任务的状态只要不是 `done`（包括 `awaiting_accept`、`accepted`、`failed`），主工作区就视为占用，直到 `finish` 结束才释放。
- **测试**：`test_main_stays_busy_until_main_task_done`。

### 3. 复用链（`reuse`）上前序任务的过程文件被判越界，还会被一起提交

- **现象**：PP10 分三步复用同一个 worktree（a→b→c）。第三步的 guard 把前两步的 `task.md`、`report.md` 判为越界，执行 agent 甚至提议删除这些过程记录；收尾时这些文件还会被当作产品文件提交。
- **根因**：guard 只放行本任务自己的过程目录 `<runs_dir>/<本任务>/**`，`finish` 也只排除这一个目录。
- **修复**：guard 和 `finish` 改为排除整个 `<runs_dir>/**`。收尾删除 worktree 之前，把其中所有过程文件拷回主工作区存档，已存在的文件不覆盖。
- **测试**：`test_reuse_chain_process_files_not_out_of_scope_nor_committed`。

### 4. 已验收的任务改动后无法重新验收

- **现象**：调度端在 `accepted` 之后调整了 worktree 文件（移走过程记录）。`finish` 提示"验收之后改动又发生了变化：请重新验收"，而 `accept` 又提示"accepted 状态不能验收"，两者互相卡死，只能手动改 `state.json`。
- **修复**：`accept` 允许从 `accepted` 状态重新验收。前提不变：guard 和 verify 必须针对当前改动重新通过。
- **测试**：`test_reaccept_after_dispatcher_adjusts_accepted_worktree`（同时确认没有新的 guard/verify 记录时，仍然拒绝验收）。

### 5. 合并后验收失败时无法定位原因

- **现象**：合并后在干净目录里重跑 verify 失败，日志只保留了最后 6 行，即 Maven 的收尾提示，看不到是哪个用例失败。临时 worktree 又随即被删除，只能手动复现（后来查明是测试夹具启动本机临时 PostgreSQL 偶发失败，与代码无关）。
- **修复**：失败命令的完整输出写入 `<runs_dir>/<任务号>/evidence/post-merge-verify-<n>.out`，汇总日志里给出该文件路径。
- **测试**：`test_post_merge_failure_keeps_full_output`。

### 验证

- 全部 23 个测试通过：`python3 -m unittest discover -s skills/orca-multi/scripts`。
- 上面 5 个新增或更新的测试放到修改前的脚本上运行，全部失败（3 个断言失败、2 个报错），说明这些测试确实能拦住对应的问题。
- **尚未验证**：新的创建方式还没有经过一次真实的端到端派工（执行 agent 启动 → 交付 → 合并 → 回收）；下次真实派工时需确认侧边栏可见、回收正常。

### 已知但未在本次处理

- 被测项目（AIOps）的测试夹具用"先绑定随机端口、释放后再交给 PostgreSQL"的方式启动临时数据库，多个测试并发运行时偶发 `pg_ctl: could not start server`。这是被测项目的问题，不是本 skill 的问题；本次只通过修复 5 的完整日志提升了可定位性。

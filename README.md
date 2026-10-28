# orca-multi

[English](README.en.md) | [中文](README.md)

让 Claude Code（或 Codex）当**调度端**，通过 Orca 把开发任务派给 Codex 等执行 agent：拆任务、写合同、启动、等回报、校验改动范围、跑验收命令、不通过自动打回，最后经人确认再合并。

适用于任何 git 项目。执行 agent 的终端在 Orca 里可见，人可以随时查看或插话。

## 目录

```text
orca-multi/
├── README.md                 本说明（中文）
├── README.en.md              English README
├── install.sh                安装到 Claude Code / Codex，本机或远程
└── skills/orca-multi/        skill 源码（安装时整体复制）
    ├── SKILL.md              给调度端看的工作流程
    ├── scripts/
    │   ├── orca-multi        命令入口
    │   ├── orca_multi.py     实现
    │   └── test_orca_multi.py
    └── templates/task.md     任务合同模板
```

## 依赖

- Python 3.9 及以上（只用标准库）
- git
- Orca 命令行 `orca`，并且 Orca 应用在运行
- 执行 agent 的命令行（默认 `codex`）

## 安装

```bash
./install.sh                       # 本机：Claude Code 和 Codex 都装
./install.sh --target claude       # 只装 Claude Code
./install.sh --host min            # 通过 ssh 装到远程主机 min
```

| 客户端 | 安装位置 |
|---|---|
| Claude Code | `~/.claude/skills/orca-multi` |
| Codex | `~/.codex/skills/orca-multi`（SKILL.md 里的路径自动改成 `~/.codex`） |

已存在的同名目录会被整体替换。安装后验证：

```bash
python3 -m unittest discover -s ~/.claude/skills/orca-multi/scripts
```

## 使用

在项目仓库里打开 Claude Code，说：

```text
orca-multi <任务描述>
```

调度端会按 `SKILL.md` 自动执行：

1. 读代码和项目规则，不清楚的先问人。
2. 拆任务：小任务一个合同；互不依赖的并行；有依赖的用 `reuse` 串行。
3. 写合同 `<runs_dir>/<任务号>/task.md`（模板见 `templates/task.md`）。`runs_dir`：项目有 `.ai-dev/` 时为 `.ai-dev/runs`，否则为 `.orca-multi/runs`。
4. `orca-multi lint <任务号>` 检查合同：提示可能受影响但没写进 `allowed_files` 的测试，和 verify 里写到的文件；建议用于改动前后端代码的任务，`orca-multi baseline <任务号>` 先跑一遍 verify，记录改动前就失败的命令（`run` 时写进派工说明）；`orca-multi plan <任务号>` 看每个任务用哪个工作区。
5. `orca-multi run <任务号>` 后台自动跑：同步任务目录附件 → 启动 → 等回报 → guard 校验范围 → verify → 不通过打回（默认最多 2 轮）→ 验收。已结束或已放弃的任务不会被重新启动，确需重做加 `--redo`；检查进度用 `status`，不要对已完成的任务再跑 `run`。
6. 复核真实改动，向人汇报。
7. 人同意后 `orca-multi finish <任务号> --message "<英文提交信息>" --yes`：提交、`--no-ff` 合并、回收 worktree，再在干净目录里用合并后的代码重跑 verify。

`run` 的退出码：

| 退出码 | 含义 | 处理 |
|---|---|---|
| 0 | 全部验收 | 复核后收尾 |
| 1 | 需人工处理（越界、主工作区被改、打回用完、agent 报失败） | 告诉人，按决定 reject / abandon / 改合同重来 |
| 2 | 等待超时 | 再次 `run` 继续 |
| 3 | 执行 agent 提问 | `orca-multi reply <消息ID> "回答"` 后再次 `run` |

其他命令：`lint`、`baseline`、`status`、`view`、`send`、`reply`、`wait`、`guard`、`verify`、`accept`、`reject`、`finish`、`abandon`、`adopt`、`gc`。`orca-multi --help` 查看。

## 合同要点

```yaml
---
id: T-20261022-short-name
title: 一句话说明
allowed_files:        # 只读任务写 []
  - src/foo/**
forbidden:
  - src/foo/generated/**
verify:               # 能自动判断成败的命令
  - npm run test
tier: FAST            # 可选：FAST→low，不写→medium，HEAVY→high
workspace: worktree   # 可选：auto | main | worktree
reuse: T-...          # 可选：复用前一个任务的工作区和终端
---
## 背景 / 要做 / 不做什么 / 验收标准
```

- 合同里不要写密钥、数据库凭据。
- 参考材料（原型、截图、原方案等）放在任务目录里：`plan`/`run` 之前放的，worktree 创建时一起复制；之后新增或改动的，每次 `run` 开始和每次 `reply` 时自动同步。
- `allowed_files` 要把“因这次改动会失败的现有测试和夹具”也列进去，测试目录可用通配（如 `backend/tests/worklog_api/**`）；写完用 `lint` 检查。

## 工作区规则

- 主工作区同一时间只给一个任务用。
- 可写任务同时满足这三条才在主工作区做，否则新建 `.worktrees/<任务号>`：主工作区空闲；`allowed_files` 不碰已有未提交改动；不和其他进行中的任务重叠。
- 上限：worktree 5 个，同时运行的 agent 4 个，超出排队。
- 新 worktree 在 Orca 里挂为项目子节点，`node_modules`、`.venv` 等用符号链接共享。

## 项目配置（可选）

`<项目>/.orca-multi/config.json`，自动加入 `.git/info/exclude`，不进提交。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `max_worktrees` | 5 | worktree 上限 |
| `max_active_workers` | 4 | 同时运行的 agent 上限 |
| `workspace` | auto | 默认工作区 |
| `agent` / `model` / `effort` | codex / gpt-6.1-sol / medium | 执行 agent、模型、推理强度 |
| `link_paths` | node_modules、.venv 等 | 符号链接共享给 worktree 的目录 |
| `max_fix_rounds` | 2 | 自动打回轮数 |
| `runs_dir` | 见上 | 合同与证据目录 |
| `agent_notes` | 空 | 项目特有说明，附在派工说明末尾 |

## 风险与边界

- Orca 启动的 Codex 是 `danger-full-access`、不需要审批。guard 只能发现仓库内越界写入，发现不了仓库外的写入。
- 提交、合并都要人明确同意；工具从不推送。
- `main`、`master` 受保护：主工作区在这两个分支上时 `finish` 拒绝合并。
- 同一时间只能有一个 `run` 在等消息（Orca 限制）。要加新任务时，等当前 run 结束，再用一个 run 带上全部进行中的任务号。

## 已知情况

- `orca-multi run` 遇到提问退出码为 3。调用时不要接 `| tail` 之类的管道，管道会吞掉真实退出码；放后台运行时看通知里的 exit code。
- 对已过期的旧提问执行 `reply` 会被 Orca 拒绝，工具在回复成功后才记录“已回答”，这类提问会一直显示为“仍有 N 个提问未回答”（只是提示，不影响使用）；目前没有忽略命令。
- 执行 agent 在收到调度端答复前就回报失败时，任务会停在 failed，`accept` 不可用。此时调度端自己重跑 guard 和 verify 确认通过后，手动提交合并，再 `abandon` 释放终端。
- 在 worktree 里用 Vite 预览时，依赖目录是符号链接，需要在仓库外的临时配置里把主仓库 `node_modules` 加进 `server.fs.allow`，否则字体图标 403。

## 开发与发布

1. 改 `skills/orca-multi/` 下的源码。
2. 跑测试：`python3 -m unittest discover -s skills/orca-multi/scripts`
3. `./install.sh` 装到本机；需要时 `./install.sh --host <主机>` 装到其他机器。

## 许可证

MIT，见 [LICENSE](LICENSE)。

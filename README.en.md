# orca-multi

[English](README.en.md) | [中文](README.md)

Let Claude Code (or Codex) act as the **dispatcher**: it hands development tasks to executor agents such as Codex through Orca. It splits the work, writes task contracts, starts the agents, waits for their reports, checks the scope of the changes, runs the acceptance commands, automatically sends failed work back for fixes, and finally asks a human to confirm before merging.

It works with any git project. The executor agents run in terminals that stay visible in Orca, so a person can watch or step in at any time.

> Note: the skill itself (`skills/orca-multi/SKILL.md`, which the dispatcher reads) and the tool's console messages are written in Chinese. This README explains how to install and use the tool in English.

## Layout

```text
orca-multi/
├── README.md                 Chinese README
├── README.en.md              This file
├── install.sh                Install into Claude Code / Codex, locally or on a remote host
└── skills/orca-multi/        Skill source (copied as a whole on install)
    ├── SKILL.md              Workflow for the dispatcher
    ├── scripts/
    │   ├── orca-multi        Command entry point
    │   ├── orca_multi.py     Implementation
    │   └── test_orca_multi.py
    └── templates/task.md     Task contract template
```

## Requirements

- Python 3.9 or newer (standard library only)
- git
- The Orca command line `orca`, with the Orca app running
- The command line of the executor agent (`codex` by default)

## Install

```bash
./install.sh                       # Local machine: install for both Claude Code and Codex
./install.sh --target claude       # Claude Code only
./install.sh --host min            # Install on the remote host "min" over ssh
```

| Client | Install location |
|---|---|
| Claude Code | `~/.claude/skills/orca-multi` |
| Codex | `~/.codex/skills/orca-multi` (paths in SKILL.md are rewritten to `~/.codex`) |

An existing directory with the same name is replaced as a whole. To verify the install:

```bash
python3 -m unittest discover -s ~/.claude/skills/orca-multi/scripts
```

## Usage

Open Claude Code inside your project repository and say:

```text
orca-multi <task description>
```

The dispatcher then follows `SKILL.md` automatically:

1. Reads the code and the project rules; asks a human when something is unclear.
2. Splits the work: one contract for a small task; independent parts run in parallel; dependent parts run one after another with `reuse`.
3. Writes a contract `<runs_dir>/<task id>/task.md` (template: `templates/task.md`). `runs_dir` is `.ai-dev/runs` when the project has `.ai-dev/`, otherwise `.orca-multi/runs`.
4. Checks the contract and the plan:
   - `orca-multi lint <task id>` flags tests that probably reference the files you are changing but are missing from `allowed_files`, and files that `verify` mentions but the contract does not allow.
   - `orca-multi baseline <task id>` (recommended for code changes) runs the `verify` commands once before dispatch and records the ones that already fail; `run` then adds them to the executor's instructions.
   - `orca-multi plan <task id>` shows which workspace each task will use.
5. `orca-multi run <task id>` runs everything in the background: sync attachments from the task directory → start → wait for the report → `guard` checks the scope → `verify` → send back on failure (at most 2 rounds by default) → accept. Tasks that are finished or abandoned are not restarted; add `--redo` when you really want to redo one. To check progress use `status`, and never run `run` again on a finished task.
6. Reviews the real changes and reports to the human.
7. After the human agrees: `orca-multi finish <task id> --message "<English commit message>" --yes` commits, merges with `--no-ff`, reclaims the worktree, and re-runs `verify` against the merged code in a clean directory.

Exit codes of `run`:

| Exit code | Meaning | What to do |
|---|---|---|
| 0 | Everything accepted | Review, then finish |
| 1 | Needs a human (out of scope, main workspace was changed, fix rounds used up, the agent reported failure) | Tell the human; then reject / abandon / change the contract and retry |
| 2 | Wait timed out | Run `run` again to continue |
| 3 | The executor agent asked a question | `orca-multi reply <message id> "answer"`, then run `run` again |

Other commands: `lint`, `baseline`, `status`, `view`, `send`, `reply`, `wait`, `guard`, `verify`, `accept`, `reject`, `finish`, `abandon`, `adopt`, `gc`. See `orca-multi --help`.

## Contract essentials

```yaml
---
id: T-20261022-short-name
title: One-line summary
allowed_files:        # use [] for read-only tasks
  - src/foo/**
forbidden:
  - src/foo/generated/**
verify:               # commands whose exit code decides pass or fail
  - npm run test
tier: FAST            # optional: FAST -> low, omitted -> medium, HEAVY -> high
workspace: worktree   # optional: auto | main | worktree
reuse: T-...          # optional: reuse the workspace and terminal of an earlier task
---
## Background / To do / Out of scope / Acceptance criteria
```

- Never put secrets or database credentials in a contract.
- Put reference material (prototypes, screenshots, an original plan, ...) in the task directory. Files placed there before `plan`/`run` are copied when the worktree is created; files added or changed later are synced at the start of every `run` and after every `reply`.
- List the existing tests and fixtures that your change is likely to break in `allowed_files` as well. Test directories can use wildcards (for example `backend/tests/worklog_api/**`). Run `lint` after writing the contract.

## Workspace rules

- The main workspace is given to only one task at a time.
- A writing task runs in the main workspace only if all three hold; otherwise a new `.worktrees/<task id>` is created: the main workspace is idle; `allowed_files` does not touch existing uncommitted changes; the task does not overlap with other running tasks.
- Limits: 5 worktrees and 4 running agents at the same time; the rest queue.
- A new worktree is attached to the project as a child node in Orca; `node_modules`, `.venv` and similar directories are shared through symbolic links.

## Project configuration (optional)

`<project>/.orca-multi/config.json`. It is added to `.git/info/exclude` automatically and is never committed.

| Field | Default | Meaning |
|---|---|---|
| `max_worktrees` | 5 | Worktree limit |
| `max_active_workers` | 4 | Limit of agents running at the same time |
| `workspace` | auto | Default workspace |
| `agent` / `model` / `effort` | codex / gpt-6.1-sol / medium | Executor agent, model and reasoning effort |
| `link_paths` | node_modules, .venv, ... | Directories shared with worktrees through symbolic links |
| `max_fix_rounds` | 2 | Automatic send-back rounds |
| `runs_dir` | see above | Directory for contracts and evidence |
| `agent_notes` | empty | Project-specific notes appended to the executor's instructions |

## Risks and boundaries

- A Codex started by Orca runs with `danger-full-access` and needs no approvals. `guard` only detects out-of-scope writes inside the repository; it cannot detect writes outside it.
- Committing and merging always need explicit human consent; the tool never pushes.
- `main` and `master` are protected: `finish` refuses to merge while the main workspace is on either branch.
- Only one `run` can wait for messages at a time (an Orca limitation). To add a task, wait for the current run to end, then start one run that includes all in-progress task ids.

## Known issues

- `orca-multi run` exits with code 3 when the executor asks a question. Do not pipe the command through `| tail` or similar; the pipe swallows the real exit code. When running it in the background, read the exit code from the completion notice.
- Replying to an old question that has already expired is rejected by Orca. The tool only records a question as answered after the reply succeeds, so such questions keep showing as "N questions still unanswered". This is only a notice and does not affect use; there is no command to dismiss them yet.
- If the executor reports failure before it receives the dispatcher's answer, the task stays in `failed` and `accept` is unavailable. In that case the dispatcher re-runs `guard` and `verify` itself, commits and merges manually once they pass, and then runs `abandon` to release the terminal.
- When previewing with Vite inside a worktree, the dependency directories are symbolic links. Add the main repository's `node_modules` to `server.fs.allow` in a temporary config kept outside the repository; otherwise font icons return 403.

## Development and release

1. Edit the source under `skills/orca-multi/`.
2. Run the tests: `python3 -m unittest discover -s skills/orca-multi/scripts`
3. Install on this machine with `./install.sh`; install on other machines with `./install.sh --host <host>`.

## License

MIT, see [LICENSE](LICENSE).

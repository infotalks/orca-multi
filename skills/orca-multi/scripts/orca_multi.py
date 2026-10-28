#!/usr/bin/env python3
"""orca-multi：用 Orca 调度执行 agent 的通用工具（任意 git 项目可用）。

约定（由本脚本强制）：
1. 执行 agent 只通过 Orca 启动（orca orchestration worker-start），终端在 Orca 里可见，人工可随时查看。
2. 人工操作经调度端：send 追加指令、reject 打回、accept 验收、finish 收尾；执行 agent 完成后用
   worker_done 回报，由调度端验收。run 命令把“启动→等待→校验→打回或验收”整条链路自动跑完。
3. 工作区按任务决定：能在主工作区安全完成的就地做，需要并行或会碰到已有未提交改动的才新建 worktree；
   新 worktree 建在项目内 .worktrees/，在 Orca 里挂为当前项目的子节点。
4. worktree 数量和并发 worker 数量有硬上限；后续任务可以用 reuse 复用已有工作区和终端。
5. 合并后回收终端和 worktree；不要的任务 abandon（先存补丁）；失败的保留现场等人决定。
6. Orca 启动的 agent 拥有完全权限，worktree 隔离只是约定。guard 会对比主工作区和其他 worktree
   的前后变化；仓库之外的写入无法检测。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULTS = {
    "max_worktrees": 5,  # .worktrees/ 下同时存在的 worktree 上限
    "max_active_workers": 4,  # 同时在跑的 worker 上限（Orca 同一 Run 最多 5 个）
    "worktree_dir": ".worktrees",
    "runs_dir": None,  # 默认：有 .ai-dev/ 用 .ai-dev/runs，否则 .orca-multi/runs
    "branch_prefix": "orca-multi/",
    "workspace": "auto",  # auto | main | worktree
    "agent": "codex",
    "model": "gpt-6.1-sol",
    "effort": "medium",
    # 主仓库里的依赖目录，用符号链接共享给新 worktree；不存在的自动跳过
    "link_paths": ["node_modules", ".venv", "frontend/node_modules", "backend/.venv"],
    "max_fix_rounds": 2,  # run 自动打回的最多轮数
    "wait_window_ms": 900000,
    "agent_notes": "",  # 项目特有的补充说明，原样附在派工说明末尾
}

TRANSITIONS = {
    "running": {"awaiting_accept", "failed"},
    "awaiting_accept": {"accepted", "running", "failed"},
    "failed": {"running", "done"},
    "accepted": {"done", "running"},
    "done": set(),
}
ACTIVE_STATUSES = {"running"}
PROTECTED_BRANCHES = {"main", "master"}  # 只能由用户手动提交，工具不往这里合并
# 任务目录里调度端自己的文件，不复制给执行 agent
RUN_INTERNAL = {"state.json", "snapshot.json", "guard.json", "verify.json", "report.md", "baseline.json"}


class OrcaMultiError(Exception):
    pass


# ───────────────────────── 纯函数（有单元测试） ─────────────────────────

def _unquote(value: str) -> str:
    """只去掉整体包裹的一对引号；命令里的引号（如 -k "a or b"）原样保留。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_task(text: str) -> dict:
    """解析任务合同：--- 之间是 key: value 与 `- 列表项`，其后是正文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise OrcaMultiError("任务合同必须以 --- 开头的头部信息开始")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise OrcaMultiError("任务合同的头部信息没有以 --- 结束") from None
    meta: dict = {}
    key = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.lstrip().startswith("- "):
            if key is None or not isinstance(meta.get(key), list):
                raise OrcaMultiError(f"列表项没有所属字段: {raw!r}")
            meta[key].append(_unquote(raw.lstrip()[2:].strip()))
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", raw)
        if not m:
            raise OrcaMultiError(f"无法解析合同行: {raw!r}")
        key, value = m.group(1), _unquote(m.group(2).strip())
        meta[key] = [] if value in ("", "[]") else value
    meta["body"] = "\n".join(lines[end + 1:]).strip()
    if not meta.get("id"):
        raise OrcaMultiError("任务合同缺少必填字段 id")
    if "allowed_files" not in meta:
        raise OrcaMultiError("任务合同缺少 allowed_files（只读任务写成空列表 `allowed_files: []`）")
    for field in ("allowed_files", "forbidden", "verify"):
        meta.setdefault(field, [])
        if not isinstance(meta[field], list):
            raise OrcaMultiError(f"{field} 必须是列表")
    meta.setdefault("title", meta["id"])
    if meta.get("workspace", "auto") not in ("auto", "main", "worktree"):
        raise OrcaMultiError("workspace 只能是 auto、main 或 worktree")
    return meta


def glob_to_regex(pattern: str) -> re.Pattern:
    """路径通配：** 匹配任意层级，* 与 ? 不跨目录；以 / 结尾表示整个目录。"""
    if pattern.endswith("/"):
        pattern += "**"
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def match_any(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


def literal_prefix(pattern: str) -> str:
    m = re.search(r"[*?]", pattern)
    return pattern[: m.start()] if m else pattern


def patterns_overlap(a: list[str], b: list[str]) -> bool:
    """两组路径通配是否可能指向同一文件（按通配符前的固定前缀粗判，宁可多判）。"""
    for x in a:
        for y in b:
            px, py = literal_prefix(x), literal_prefix(y)
            if px.startswith(py) or py.startswith(px):
                return True
    return False


def evaluate_scope(changed: list[str], allowed: list[str], forbidden: list[str], always_ok: list[str] | None = None) -> dict:
    """返回越界文件与命中禁止项的文件。always_ok 是汇报和证据目录，不算越界。"""
    always_ok = always_ok or []
    out_of_scope, forbidden_hit = [], []
    for path in sorted(set(changed)):
        if match_any(path, always_ok):
            continue
        if match_any(path, forbidden):
            forbidden_hit.append(path)
        elif not match_any(path, allowed):
            out_of_scope.append(path)
    return {"out_of_scope": out_of_scope, "forbidden_hit": forbidden_hit}


def choose_workspace(task: dict, active: list[dict], main_dirty: list[str], default: str = "auto") -> tuple[str, str]:
    """决定任务在哪干活，返回 (main|worktree, 原因)。

    active 是其他进行中任务的 [{workspace, allowed_files}]；main_dirty 是主工作区当前未提交的文件。
    主工作区同一时间只给一个任务用；可写任务还要求不碰已有未提交改动、不和其他进行中任务抢同一批文件。
    """
    readonly = not task["allowed_files"]
    main_busy = any(a["workspace"] == "main" for a in active)
    wanted = task.get("workspace") or "auto"
    if wanted == "auto":
        wanted = default
    if wanted == "main":
        if main_busy:
            raise OrcaMultiError("主工作区已有进行中的任务，不能再指定 workspace: main")
        return "main", "合同指定在主工作区执行"
    if wanted == "worktree":
        return "worktree", "合同指定新建 worktree"
    if main_busy:
        return "worktree", "主工作区已有进行中的任务"
    if readonly:
        return "main", "只读任务，不需要隔离"
    dirty_hit = [p for p in main_dirty if match_any(p, task["allowed_files"])]
    if dirty_hit:
        return "worktree", f"主工作区里这些文件已有未提交改动: {dirty_hit[:5]}"
    clash = [a for a in active if patterns_overlap(task["allowed_files"], a["allowed_files"])]
    if clash:
        return "worktree", "与其他进行中任务的文件范围重叠"
    return "main", "主工作区空闲，且不碰已有未提交改动"


def can_spawn(worktrees: int, active_workers: int, cfg: dict, needs_worktree: bool) -> tuple[bool, str]:
    if active_workers >= cfg["max_active_workers"]:
        return False, f"并发 worker 已达上限 {active_workers}/{cfg['max_active_workers']}"
    if needs_worktree and worktrees >= cfg["max_worktrees"]:
        return False, f"worktree 已达上限 {worktrees}/{cfg['max_worktrees']}，先合并或放弃已完成的任务"
    return True, ""


def check_transition(current: str, new: str) -> None:
    if new != current and new not in TRANSITIONS.get(current, set()):
        raise OrcaMultiError(f"不允许的状态变化: {current} -> {new}")


def diff_fingerprints(before: dict, after: dict) -> list[str]:
    return [k for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]


def extract_json(text: str):
    """orca 的 --json 输出可能夹着单行心跳 {"_keepalive":...}，去掉后整体解析。"""
    kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith('{"_keepalive"')]
    cleaned = "\n".join(kept).strip()
    if not cleaned:
        raise OrcaMultiError("orca 没有返回内容")
    return json.loads(cleaned)


def build_spec(task_id: str, rel_dir: str, workspace: str, notes: str = "") -> str:
    where = (
        "你在项目主工作区里工作，里面可能有别人未提交的改动：不要修改、还原或整理任何与本任务无关的文件。"
        if workspace == "main" else
        "只在当前 worktree 内工作，不要读写主仓库目录和其他 worktree。"
    )
    spec = (
        f"你是 orca-multi 的执行 agent，任务 {task_id}。\n"
        f"1. 阅读并严格按 {rel_dir}/task.md 执行，同时遵守仓库里的 AGENTS.md 等协作规则。\n"
        f"2. {where}不要提交、暂存或推送；不要执行 git reset、git checkout、git stash 等会丢弃改动或改写历史的命令；"
        "不要安装依赖（npm/pip install 等），也不要修改依赖目录（node_modules、.venv 等，它们可能是指向主仓库的软链接）。\n"
        "3. 只修改合同 allowed_files 范围内的文件；allowed_files 为空表示只读任务，除汇报外不得修改任何文件。"
        "汇报文件（report.md）和任务目录是唯一例外。\n"
        "4. 验收只运行合同 verify 列出的命令；不要运行或依据仓库里其他流程、守卫、质量门脚本，也不要为了它们补文件。"
        "允许用只读的 git status / git diff 检查自己的改动范围。"
        "若 verify 里的命令与以上规则冲突（如安装依赖、改写仓库文件），不要执行，用 ask 向调度端提问。\n"
        f"5. 汇报写到 {rel_dir}/report.md：改动文件、验收命令及真实输出末尾、未验证项、发现但未处理的问题。\n"
        "6. 完成后，按注入的前置说明用 orca orchestration send 发送一次 worker_done"
        "（成功 --outcome succeeded，失败 --outcome failed），然后停下等待调度端验收或追加指令。\n"
        "7. 拿不准需求时用前置说明里的 ask 向调度端提问，不要自行假设。\n"
    )
    return spec + (f"\n项目补充说明：\n{notes}\n" if notes else "")


# ───────────────────────── 命令执行与仓库辅助 ─────────────────────────

ORCA = os.environ.get("ORCA_MULTI_ORCA", "orca")


def run(cmd: list[str], cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise OrcaMultiError(f"命令失败 ({proc.returncode}): {' '.join(cmd[:6])}\n{(proc.stderr or proc.stdout).strip()[:600]}")
    return proc


def first_failure(lines: list[str], width: int = 12) -> list[str]:
    """从验证输出里找第一个失败用例（node --test / pytest / 通用 error），返回它附近的几行。"""
    import re
    pat = re.compile(r"^(not ok\b|\s*✖ (?!failing tests)|FAILED |E\s{3}|.*\b(SyntaxError|TypeError|AssertionError|ReferenceError)\b)")
    for i, line in enumerate(lines):  # node --test 末尾的汇总带报错原因，优先用它
        if line.strip().startswith("✖ failing tests"):
            return [ln for ln in lines[i + 1:i + 1 + width] if ln.strip()]
    for i, line in enumerate(lines):
        if pat.search(line):
            return lines[i:i + width]
    return []


def _node_version() -> str:
    try:
        return subprocess.run(["node", "-v"], text=True, capture_output=True).stdout.strip() or "?"
    except OSError:
        return "?"


NODE_VERSION = _node_version()


def git(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    # 关闭路径转义：否则中文等非 ASCII 路径会输出为带引号的八进制串，范围匹配会误判越界
    return run(["git", "-c", "core.quotepath=false", *args], cwd=cwd, check=check).stdout


def orca(*args: str, check: bool = True, allow_unknown: bool = False):
    """调用 orca 并返回 result 字段；失败抛出，不重试（Orca 要求先查原状态再决定）。"""
    proc = run([ORCA, *args, "--json"], check=False)
    try:
        data = extract_json(proc.stdout)
    except (OrcaMultiError, json.JSONDecodeError):
        data = None
    result = (data or {}).get("result", data) if isinstance(data, dict) else None
    if allow_unknown and isinstance(result, dict) and result.get("dispatchId"):
        return result  # 已派出但启动未确认：交给调用方记录并提示，不能重派
    if proc.returncode != 0 or (isinstance(data, dict) and data.get("ok") is False):
        if check:
            raise OrcaMultiError(f"orca {' '.join(args[:3])} 失败: {(proc.stderr or proc.stdout).strip()[:600]}")
        return None
    return (data or {}).get("result", data)


def repo_root(cwd: str | Path | None = None) -> Path:
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).strip()
    return Path(common).resolve().parent


def load_config(root: Path, path: str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    cfg_path = Path(path) if path else root / ".orca-multi" / "config.json"
    if cfg_path.is_file():
        cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
    if not cfg["runs_dir"]:
        cfg["runs_dir"] = ".ai-dev/runs" if (root / ".ai-dev").is_dir() else ".orca-multi/runs"
    return cfg


TIER_EFFORT = {"FAST": "low", "MEDIUM": "medium", "HEAVY": "high"}

CFG: dict = dict(DEFAULTS, runs_dir=".orca-multi/runs")  # main() 里按项目覆盖


def runs_rel() -> str:
    return CFG["runs_dir"].strip("/")


def run_dir(root: Path, task_id: str) -> Path:
    return root / runs_rel() / task_id


def read_state(root: Path, task_id: str) -> dict | None:
    p = run_dir(root, task_id) / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def write_state(root: Path, state: dict) -> None:
    d = run_dir(root, state["id"])
    d.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def set_status(root: Path, state: dict, new: str) -> None:
    check_transition(state["status"], new)
    state["status"] = new
    write_state(root, state)


def all_states(root: Path) -> list[dict]:
    out = []
    base = root / runs_rel()
    for p in sorted(base.glob("*/state.json")) if base.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("tool") == "orca-multi":
            out.append(data)
    return out


def worktree_paths(root: Path) -> list[Path]:
    return [Path(ln[len("worktree "):]) for ln in git("worktree", "list", "--porcelain", cwd=root).splitlines()
            if ln.startswith("worktree ")]


def managed_worktrees(root: Path, cfg: dict) -> list[Path]:
    """本工具管理的 worktree：旧版建在 .worktrees/ 下的，加上状态里记录为 Orca 创建的。"""
    base = (root / cfg["worktree_dir"]).resolve()
    owned = {Path(s["worktree"]).resolve() for s in all_states(root) if s.get("worktree_owner") == "orca"}
    return [p for p in worktree_paths(root)
            if base in p.resolve().parents or (p.resolve() in owned and p.resolve() != root.resolve())]


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def path_fingerprint(path: Path) -> str:
    if path.is_symlink():
        return "link:" + os.readlink(path)
    if path.is_file():
        return sha1_file(path)
    return "missing"


def excluded_prefixes(cfg: dict) -> tuple[str, ...]:
    """调度端自己的目录（worktree、状态与证据）不计入工作区指纹。"""
    return (cfg["worktree_dir"].strip("/") + "/", cfg["runs_dir"].strip("/") + "/", ".orca-multi/")


def workspace_fingerprint(path: Path, exclude: tuple[str, ...] = ()) -> dict:
    """工作区指纹：HEAD + 每个未提交文件的内容摘要。用于发现越出自己工作区的写入。"""
    fp = {"HEAD": git("rev-parse", "HEAD", cwd=path).strip()}
    parts = git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=path).split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:]
        if status[0] in "RC":  # 重命名/复制：下一段是原路径
            i += 1
        if any(rel.startswith(p) for p in exclude):
            continue
        fp[rel] = f"{status}:{path_fingerprint(path / rel)}"
    return fp


def dirty_files(path: Path, cfg: dict) -> list[str]:
    return [k for k in workspace_fingerprint(path, excluded_prefixes(cfg)) if k != "HEAD"]


def changed_files(worktree: Path, base: str) -> list[str]:
    tracked = git("diff", "--name-only", "--no-renames", base, cwd=worktree).splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard", cwd=worktree).splitlines()
    return sorted({p for p in tracked + untracked if p})


def snapshot_path(root: Path, task_id: str) -> Path:
    return run_dir(root, task_id) / "snapshot.json"


def task_files(root: Path, cfg: dict, s: dict) -> list[str]:
    """本任务改动的文件。worktree 任务对比基线；主工作区任务对比启动前的指纹。"""
    if s["workspace"] == "main":
        snap = json.loads(snapshot_path(root, s["id"]).read_text(encoding="utf-8"))
        now = workspace_fingerprint(root, excluded_prefixes(cfg))
        return [f for f in diff_fingerprints(snap["main"], now) if f != "HEAD"]
    return [f for f in changed_files(Path(s["worktree"]), s["base"]) if f not in cfg["link_paths"]]


def candidate_hash(workdir: Path, files: list[str]) -> str:
    h = hashlib.sha1()
    for rel in sorted(files):
        p = workdir / rel
        h.update(f"{rel}|{'D' if not p.exists() else sha1_file(p) if p.is_file() else 'x'}\n".encode())
    return h.hexdigest()


def ensure_excluded(root: Path, cfg: dict) -> None:
    exclude = Path(git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude", cwd=root).strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    lines = existing.splitlines()
    add = [ln for ln in (f"/{cfg['worktree_dir'].strip('/')}/", "/.orca-multi/") if ln not in lines]
    if add:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + sep + "\n".join(add) + "\n", encoding="utf-8")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def need_state(root: Path, task_id: str) -> dict:
    s = read_state(root, task_id)
    if not s:
        raise OrcaMultiError(f"没有任务 {task_id}")
    return s


def load_task(root: Path, task_id: str) -> dict:
    p = run_dir(root, task_id) / "task.md"
    if not p.is_file():
        raise OrcaMultiError(f"找不到任务合同 {p}；先按模板写好合同")
    task = parse_task(p.read_text(encoding="utf-8"))
    if task["id"] != task_id:
        raise OrcaMultiError(f"合同里的 id（{task['id']}）与任务号（{task_id}）不一致")
    return task


def active_states(root: Path, exclude_id: str | None = None) -> list[dict]:
    return [s for s in all_states(root) if s["status"] in ACTIVE_STATUSES and s["id"] != exclude_id]


# ───────────────────────── 启动 ─────────────────────────

def ensure_run(objective: str) -> str:
    current = orca("orchestration", "run-current", check=False) or {}
    run_ = current.get("run") if isinstance(current, dict) else None
    if run_ and run_.get("id"):
        return run_["id"]
    return orca("orchestration", "run-create", "--objective", objective)["run"]["id"]


# Orca worker-start：只有这些 agent 接受 --model/--effort；omp 只接受 --model；
# 其余（如 zcode、opencode）使用 agent 自身配置的模型，传了会被拒绝或忽略。
MODEL_AND_EFFORT_AGENTS = {"claude", "codex", "cursor", "antigravity", "muse"}
MODEL_ONLY_AGENTS = {"omp"}


def agent_launch_args(agent: str, model: str | None, effort: str | None, explicit_model: bool) -> tuple[list[str], str, str | None]:
    """返回 (worker-start 参数, 记录用的模型, 记录用的推理强度)。"""
    if agent in MODEL_AND_EFFORT_AGENTS:
        return ["--agent", agent, "--model", model, "--effort", effort], model, effort
    if agent in MODEL_ONLY_AGENTS:
        return ["--agent", agent, "--model", model], model, None
    if explicit_model:
        say(f"注意：{agent} 使用自身配置的模型，合同里的 model/effort 被忽略；请在 {agent} 中切换默认模型")
    return ["--agent", agent], f"{agent} 自身配置", None


def workspace_label(plan: dict) -> str:
    if plan["workspace"] == "main":
        return "主工作区"
    return str(plan["worktree"]) if plan["worktree"] else "新建 Orca worktree（侧边栏可见，挂在当前项目下）"


def plan_task(root: Path, cfg: dict, task_id: str, planned: list[dict] | None = None) -> dict:
    """算出启动计划但不执行：工作区、分支、基线、上限是否允许。planned 是同批里排在前面、将先启动的任务。"""
    task = load_task(root, task_id)
    active = active_states(root, task_id) + (planned or [])
    reuse_state = None
    if task.get("reuse"):
        reuse_state = read_state(root, task["reuse"])
        if not reuse_state or reuse_state["status"] not in ("accepted", "awaiting_accept", "failed"):
            raise OrcaMultiError(f"reuse 只能复用已停下的任务，{task['reuse']} 当前为 {reuse_state and reuse_state['status']}")
        workspace, reason = reuse_state["workspace"], f"复用任务 {task['reuse']} 的工作区和终端"
    else:
        snapshot = [{"workspace": s["workspace"], "allowed_files": s.get("allowed_files", [])} for s in active]
        # 主工作区任务交付后（待验收/已验收/失败）改动仍未提交，guard 按主工作区指纹判断范围；
        # 此时再放一个任务进主工作区，两边会把对方的文件算成自己越界。直到 done 才释放主工作区。
        snapshot += [{"workspace": "main", "allowed_files": s.get("allowed_files", [])} for s in all_states(root)
                     if s["workspace"] == "main" and s["status"] not in ("done", *ACTIVE_STATUSES) and s["id"] != task_id]
        workspace, reason = choose_workspace(task, snapshot, dirty_files(root, cfg), cfg["workspace"])
    needs_new = workspace == "worktree" and not reuse_state
    new_planned = sum(1 for p in planned or [] if p.get("new_worktree"))
    ok, why = can_spawn(len(managed_worktrees(root, cfg)) + new_planned, len(active), cfg, needs_new)
    if reuse_state:
        wt, branch, base = Path(reuse_state["worktree"]), reuse_state.get("branch"), reuse_state["base"]
    elif workspace == "main":
        wt, branch, base = root, None, git("rev-parse", "HEAD", cwd=root).strip()
    else:
        wt = None  # 由 Orca 在启动时创建（orca worktree create），路径与分支名以其返回为准
        branch = task_id
        base = task.get("base") or git("rev-parse", "HEAD", cwd=root).strip()
    return {"task": task, "workspace": workspace, "reason": reason, "reuse": reuse_state, "worktree": wt,
            "branch": branch, "base": base, "can_start": ok, "blocked": why}


# ───────────────────────── 基线、合同检查、任务目录同步 ─────────────────────────
def baseline_note(root: Path, task_id: str) -> str:
    """基线结果（改动前就失败的 verify 命令）写进派工说明，让执行 agent 不必自己踩一遍。"""
    path = run_dir(root, task_id) / "baseline.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    failed = [r for r in data.get("results", []) if r.get("exit") != 0]
    if not failed:
        return f"基线（{str(data.get('head', ''))[:8]}）：合同 verify 的命令在改动前全部通过。"
    lines = [f"基线（{str(data.get('head', ''))[:8]}）：下列 verify 命令在改动前就失败，不一定是你引入的"
             "（引用本任务将新建的文件时失败属正常；其余失败若阻塞验收，用 ask 向调度端说明，不要为此改 allowed_files 之外的文件）："]
    for r in failed:
        lines.append(f"- {r['command']}（退出码 {r['exit']}）")
        lines += [f"    {ln}" for ln in (r.get("first_failure") or r.get("tail") or [])[:6]]
    return "\n".join(lines)


def run_baseline(root: Path, cfg: dict, task_id: str, timeout_s: int) -> dict:
    """在主工作区的当前提交上先跑一遍合同 verify，记录改动前的状态。主工作区有进行中的任务时拒绝，避免干扰 guard。"""
    task = load_task(root, task_id)
    busy = [s["id"] for s in active_states(root) if s["workspace"] == "main"]
    if busy:
        raise OrcaMultiError(f"主工作区有进行中的任务 {busy}，基线会干扰它们的范围校验，请等它们结束")
    results = []
    for i, command in enumerate(task["verify"], 1):
        try:
            proc = subprocess.run(["bash", "-lc", command], cwd=root, text=True, capture_output=True, timeout=timeout_s)
            code, out = proc.returncode, (proc.stdout + proc.stderr).strip().splitlines()
        except subprocess.TimeoutExpired:
            code, out = 124, [f"超时（{timeout_s} 秒）"]
        results.append({"command": command, "exit": code, "tail": out[-6:], "first_failure": first_failure(out) if code else []})
    data = {"head": git("rev-parse", "HEAD", cwd=root).strip(), "dirty": dirty_files(root, cfg)[:20],
            "results": results, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (run_dir(root, task_id) / "baseline.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|e2e)(/|$)|(^|/)test_[^/]*$|\.(test|spec)\.[A-Za-z]+$")
GENERIC_STEMS = {"index", "main", "utils", "util", "api", "app", "base", "common", "config", "types", "style", "styles", "router", "store"}
PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+/[A-Za-z0-9_.-]+\.(?:py|js|ts|mjs|sh|vue)\b")


LINT_MAX_TESTS = 12  # 提示条数上限，按相关度排序，避免淹没真正要看的几条


def lint_contract(root: Path, task_id: str) -> list[str]:
    """合同检查（启发式，只提示不拦截）：
    1. allowed_files 里的源文件，在测试文件里被引用、但测试文件不在 allowed_files 里（按相关度取前几条）；
    2. verify 命令里写到的文件路径不在 allowed_files 里（agent 可能需要新建或修改它）；
       命令里 `cd 目录 &&` 之后的相对路径按该目录解析。"""
    task = load_task(root, task_id)
    allowed, forbidden = task["allowed_files"], task["forbidden"]
    if not allowed:
        return []
    runs_glob = f"{runs_rel()}/**"
    tracked = [p for p in git("ls-files", cwd=root).splitlines() if p and not match_any(p, [runs_glob])]
    sources = [f for f in tracked if match_any(f, allowed) and not TEST_PATH_RE.search(f)]
    stems: dict[str, str] = {}
    for f in sources:
        stem = Path(f).stem
        if len(stem) >= 4 and stem.lower() not in GENERIC_STEMS:
            stems.setdefault(stem, Path(f).name)
    warnings = []
    tests = [f for f in tracked if TEST_PATH_RE.search(f) and not match_any(f, allowed) and not match_any(f, forbidden)]
    scored: list[tuple[int, str, list[str]]] = []
    for f in tests:
        path = root / f
        try:
            if path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hit = [stem for stem in stems if re.search(rf"\b{re.escape(stem)}\b", text)]
        if hit:
            # 提到完整文件名（含扩展名）说明多半是直接读取或导入该文件，相关度更高
            score = sum(2 + (stems[stem] in text) * 3 for stem in hit)
            scored.append((score, f, sorted(hit)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    for _, f, hit in scored[:LINT_MAX_TESTS]:
        warnings.append(f"测试文件 {f} 引用了 {'、'.join(hit[:3])}，但不在 allowed_files 里；改动若影响它，执行 agent 会被迫提问")
    if len(scored) > LINT_MAX_TESTS:
        warnings.append(f"另有 {len(scored) - LINT_MAX_TESTS} 个测试文件也引用了这些文件（只列相关度最高的 {LINT_MAX_TESTS} 个）")
    for command in task["verify"]:
        prefix = ""
        for segment in re.split(r"&&|;|\|\|", command):
            m = re.match(r"\s*cd\s+(\S+)", segment)
            if m:
                prefix = m.group(1).strip("'\"").rstrip("/") + "/"
                continue
            for token in PATH_TOKEN_RE.findall(segment):
                full = prefix + token if prefix and not token.startswith(prefix) else token
                if not match_any(full, allowed) and not (root / full).is_file():
                    warnings.append(f"verify 引用了 {full}（不存在也不在 allowed_files 里），执行 agent 无法新建它")
    return warnings


def sync_task_files(root: Path, s: dict) -> int:
    """把主工作区任务目录里的附件（参考材料等）同步到执行 agent 的 worktree：创建时只复制一次，之后新增或改动的不会自动到达。"""
    if s["workspace"] != "worktree" or not s.get("worktree"):
        return 0
    src, dst = run_dir(root, s["id"]), Path(s["worktree"]) / runs_rel() / s["id"]
    if not src.is_dir() or not dst.parent.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in src.iterdir():
        if f.is_file() and f.name not in RUN_INTERNAL:
            target = dst / f.name
            data = f.read_bytes()
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
                copied += 1
    return copied



def spawn(root: Path, cfg: dict, task_id: str, dry_run: bool = False, redo: bool = False) -> dict | None:
    existing = read_state(root, task_id)
    if existing and existing["status"] != "done":
        say(f"任务 {task_id} 已存在（{existing['status']}），不重复启动")
        return existing
    if existing and not redo:
        raise OrcaMultiError(f"任务 {task_id} 已结束（done），不会自动重新启动；确需重做请加 --redo")
    plan = plan_task(root, cfg, task_id)
    task, wt, rel_dir = plan["task"], plan["worktree"], f"{runs_rel()}/{task_id}"
    say(f"{task_id}: 工作区={workspace_label(plan)}（{plan['reason']}）")
    if not plan["can_start"]:
        raise OrcaMultiError("暂不能启动：" + plan["blocked"])
    notes = "\n".join(x for x in (cfg.get("agent_notes", ""), baseline_note(root, task_id)) if x)
    spec = build_spec(task_id, rel_dir, plan["workspace"], notes)
    agent = task.get("agent") or cfg["agent"]
    model = task.get("model") or cfg["model"]
    # 推理强度：合同 effort 优先；否则按 tier（FAST→low，HEAVY→high）；都没有用默认 medium
    effort = task.get("effort") or TIER_EFFORT.get(str(task.get("tier") or "").upper()) or cfg["effort"]
    launch, model, effort = agent_launch_args(agent, model, effort, bool(task.get("model") or task.get("effort")))
    if dry_run:
        say(f"【dry-run】agent={agent} model={model} effort={effort}\n{spec}")
        return None

    ensure_excluded(root, cfg)
    owner = (plan["reuse"] or {}).get("worktree_owner")
    if plan["workspace"] == "worktree" and not plan["reuse"]:
        # 由 Orca 创建 worktree：创建即登记（自建再补登记会撞上 selector_not_found），
        # 并作为当前项目子节点显示在侧边栏；仓库内 .worktrees/ 隐藏目录下的 worktree 在侧边栏看不到
        created = orca("worktree", "create", "--repo", f"path:{root}", "--name", task_id,
                       "--base-branch", plan["base"], "--parent-worktree", f"path:{root}",
                       "--setup", "skip", "--comment", f"orca-multi 任务 {task_id}") or {}
        info = created.get("worktree") or {}
        if not info.get("path"):
            raise OrcaMultiError(f"orca worktree create 没有返回 worktree 路径: {str(created)[:300]}")
        wt, owner = Path(info["path"]), "orca"
        plan["branch"] = (info.get("branch") or task_id).removeprefix("refs/heads/")
        say(f"{task_id}: Orca 已创建 worktree {wt}（分支 {plan['branch']}）")
        for rel in cfg["link_paths"]:
            src, dst = root / rel, wt / rel
            if src.exists() and not dst.exists() and not dst.is_symlink():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(src, dst)
    if wt != root:  # 合同和附件复制进 worktree（新 worktree 里没有主工作区的未提交文件）
        (wt / rel_dir).mkdir(parents=True, exist_ok=True)
        for f in run_dir(root, task_id).iterdir():
            if f.is_file() and f.name not in RUN_INTERNAL:
                (wt / rel_dir / f.name).write_bytes(f.read_bytes())
        orca("worktree", "set", "--worktree", f"path:{wt}",
             "--display-name", f"{task_id}｜{task['title']}", "--workspace-status", "in-progress",
             "--comment", f"orca-multi 任务 {task_id}", check=plan["reuse"] is None)

    run_id = ensure_run(f"orca-multi：{task['title']}")
    snap = {"main": workspace_fingerprint(root, excluded_prefixes(cfg)), "siblings": {}}
    for other in worktree_paths(root):
        if other.resolve() not in (root.resolve(), wt.resolve()):
            snap["siblings"][str(other)] = workspace_fingerprint(other, excluded_prefixes(cfg))
    snapshot_path(root, task_id).write_text(json.dumps(snap, ensure_ascii=False) + "\n", encoding="utf-8")

    start = ["orchestration", "worker-start", "--spec", spec, "--task-title", f"{task_id}｜{task['title']}",
             "--display-name", f"{task_id}｜{task['title']}", "--worktree", f"path:{wt}"]
    reuse = plan["reuse"]
    if reuse and reuse.get("terminal"):
        start += ["--terminal", reuse["terminal"]]
    else:
        start += launch
    result = orca(*start, allow_unknown=True)
    if result.get("state") == "outcome_unknown" or result.get("lastError"):
        say(f"警告：{task_id} 已派出，但 Orca 未能确认执行 agent 开始工作（{result.get('stage')}）。"
            f"不会重派；用 view 查看终端确认，必要时 abandon。")
    terminal = next((e["id"] for e in result.get("effects", []) if e.get("kind") == "terminal"), None) or (reuse or {}).get("terminal")
    state = {
        "tool": "orca-multi", "id": task_id, "title": task["title"], "status": "running",
        "workspace": plan["workspace"], "worktree": str(wt), "branch": plan["branch"], "base": plan["base"],
        "worktree_owner": owner,
        "allowed_files": task["allowed_files"], "run_id": result.get("runId", run_id),
        "task_id": result["taskId"], "dispatch_id": result["dispatchId"], "terminal": terminal,
        "agent": agent, "model": model, "effort": effort, "reused_from": task.get("reuse"),
        "fix_rounds": 0, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if reuse:  # 被复用的旧任务交出工作区
        reuse["status"] = "done"
        reuse["handed_to"] = task_id
        write_state(root, reuse)
    write_state(root, state)
    say(f"已启动 {task_id}：终端 {terminal}（人工查看: orca terminal switch --terminal {terminal}）")
    return state


# ───────────────────────── 校验 ─────────────────────────

def guard(root: Path, cfg: dict, s: dict, ignore_main: str | None = None) -> dict:
    task = load_task(root, s["id"])
    workdir = Path(s["worktree"])
    files = task_files(root, cfg, s)
    # 过程文件（合同、汇报、证据）不算产品改动；复用 worktree 时前序任务的过程目录也在这里
    rel_dir = f"{runs_rel()}/**"
    scope = evaluate_scope(files, task["allowed_files"], task["forbidden"], [rel_dir])
    snap = json.loads(snapshot_path(root, s["id"]).read_text(encoding="utf-8"))
    problems, outside = [], []
    if scope["out_of_scope"]:
        problems.append(f"越出 allowed_files: {scope['out_of_scope']}")
    if scope["forbidden_hit"]:
        problems.append(f"命中 forbidden: {scope['forbidden_hit']}")
    main_changes: list[str] = []
    if s["workspace"] == "main":
        if snap["main"]["HEAD"] != git("rev-parse", "HEAD", cwd=root).strip():
            outside.append("主工作区 HEAD 在任务期间变化（有人提交或切换了分支）")
    else:
        main_changes = diff_fingerprints(snap["main"], workspace_fingerprint(root, excluded_prefixes(cfg)))
        # 主工作区 HEAD 沿同一分支前进（调度端 finish 合并其他任务、提交调度文档）是正常推进；
        # worktree 里的 agent 改不到主工作区的 HEAD。只有历史被改写或切换分支才算异常。
        if "HEAD" in main_changes:
            main_changes.remove("HEAD")
            if run(["git", "merge-base", "--is-ancestor", snap["main"]["HEAD"], "HEAD"], cwd=root, check=False).returncode != 0:
                outside.append("主工作区 HEAD 不再包含任务启动时的提交（历史被改写或切换了分支）")
        # 同时在主工作区执行的任务，其范围内的变化不算本任务越界
        others = [p for a in active_states(root, s["id"]) if a["workspace"] == "main" for p in load_task(root, a["id"])["allowed_files"]]
        main_changes = [f for f in main_changes if not (others and match_any(f, others))]
        # 人工确认过的主工作区变化记在状态里，之后只拦新的变化
        acked = s.setdefault("ignored_main", {})
        if ignore_main:
            for f in main_changes:
                acked[f] = ignore_main
            write_state(root, s)
        main_changes = [f for f in main_changes if f not in acked]
        if main_changes:
            outside.append(f"主工作区在任务启动后发生变化: {main_changes[:20]}")
    # 其他进行中任务的 worktree：只有超出那个任务自己范围的改动才可疑
    owners = {str(Path(a["worktree"]).resolve()): load_task(root, a["id"])["allowed_files"] for a in all_states(root)
              if a["id"] != s["id"] and a["status"] != "done" and a["workspace"] == "worktree"}
    # 只比对仍登记在 git 里的 worktree：已回收的目录可能只剩残留文件，在里面跑 git 会落到主仓库
    live = {str(p.resolve()) for p in worktree_paths(root)}
    for path, before in snap["siblings"].items():
        if Path(path).exists() and str(Path(path).resolve()) in live:
            delta = diff_fingerprints(before, workspace_fingerprint(Path(path), excluded_prefixes(cfg)))
            owner = owners.get(str(Path(path).resolve()))
            if owner is not None:
                delta = [f for f in delta if f != "HEAD" and not match_any(f, owner + [f"{runs_rel()}/**"])]
            # 人工确认过的其他 worktree 变化同样记在状态里（例如对方任务自己清理的临时文件）
            acked_sib = s.setdefault("ignored_siblings", {})
            if ignore_main and delta:
                for f in delta:
                    acked_sib[f"{path}::{f}"] = ignore_main
                write_state(root, s)
            delta = [f for f in delta if f"{path}::{f}" not in acked_sib]
            if delta:
                outside.append(f"其他 worktree {path} 发生变化: {delta[:10]}")
    result = {"ok": not problems and not outside, "files": files, "problems": problems, "outside": outside,
              "ignore_main": ignore_main, "candidate": candidate_hash(workdir, files), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (run_dir(root, s["id"]) / "guard.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def verify(root: Path, cfg: dict, s: dict) -> dict:
    task = load_task(root, s["id"])
    workdir = Path(s["worktree"])
    evidence = run_dir(root, s["id"]) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    results, ok = [], True
    for i, command in enumerate(task["verify"], 1):
        proc = subprocess.run(["bash", "-lc", command], cwd=workdir, text=True, capture_output=True)
        (evidence / f"verify-{i}.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        out = (proc.stdout + proc.stderr).strip().splitlines()
        results.append({"command": command, "exit": proc.returncode, "tail": out[-6:],
                        "first_failure": first_failure(out), "log": f"{runs_rel()}/{s['id']}/evidence/verify-{i}.log"})
        ok = ok and proc.returncode == 0
    files = task_files(root, cfg, s)
    out = {"ok": ok, "results": results, "candidate": candidate_hash(workdir, files), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (run_dir(root, s["id"]) / "verify.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def report_path(root: Path, s: dict) -> Path:
    return Path(s["worktree"]) / runs_rel() / s["id"] / "report.md"


def accept(root: Path, cfg: dict, s: dict) -> None:
    # 已验收后调度端又调整了文件（如移走过程文件），允许针对新改动重新验收，guard/verify 仍须对当前改动通过
    if s["status"] not in ("awaiting_accept", "accepted"):
        raise OrcaMultiError(f"{s['status']} 状态不能验收（要等执行 agent 回报 worker_done）")
    if not report_path(root, s).is_file():
        raise OrcaMultiError("执行 agent 没有写汇报 report.md，不能验收")
    current = candidate_hash(Path(s["worktree"]), task_files(root, cfg, s))
    g = read_json(run_dir(root, s["id"]) / "guard.json")
    v = read_json(run_dir(root, s["id"]) / "verify.json")
    if not (g and g["ok"] and g["candidate"] == current):
        raise OrcaMultiError("还没有针对当前改动的 guard 通过记录")
    if not (v and v["ok"] and v["candidate"] == current):
        raise OrcaMultiError("还没有针对当前改动的 verify 通过记录")
    s["accepted_candidate"] = current
    set_status(root, s, "accepted")
    if s["workspace"] == "worktree":
        orca("worktree", "set", "--worktree", f"path:{s['worktree']}", "--workspace-status", "in-review", check=False)


def reject(root: Path, s: dict, reason: str) -> None:
    """打回：在同一终端、同一工作区新开一次派工，把修改意见作为任务内容。

    执行 agent 回报 worker_done 后原 Dispatch 已结束，往它的信箱发消息不会被读取，所以不能用 send。
    """
    if s["status"] not in ("awaiting_accept", "accepted", "failed"):
        raise OrcaMultiError(f"{s['status']} 状态不能打回")
    rel_dir = f"{runs_rel()}/{s['id']}"
    spec = (
        f"调度端验收任务 {s['id']} 未通过，请在当前工作区继续修改。\n"
        f"修改意见：\n{reason}\n\n"
        f"仍须遵守 {rel_dir}/task.md 的全部约束（只改 allowed_files，不提交、不暂存、不推送，"
        "验收只运行合同 verify 中的命令）。改完后更新 report.md，并按前置说明再次发送 worker_done。"
    )
    start = ["orchestration", "worker-start", "--spec", spec,
             "--task-title", f"{s['id']}｜返工 {s.get('fix_rounds', 0) + 1}",
             "--display-name", f"{s['id']}｜{s['title']}", "--worktree", f"path:{s['worktree']}"]
    agent = s.get("agent") or CFG["agent"]
    model = s.get("model") if s.get("model") and not str(s.get("model")).endswith("自身配置") else CFG["model"]
    fresh, _, _ = agent_launch_args(agent, model, s.get("effort") or CFG["effort"], False)
    # 不要先 worker-release：释放会关闭终端，原执行 agent 随之退出
    result = None
    if s.get("terminal"):
        try:
            result = orca(*start, "--terminal", s["terminal"], allow_unknown=True)
        except OrcaMultiError as exc:
            if "agent_unconfigured" not in str(exc) and "not running" not in str(exc):
                raise
            say(f"原终端 {s['terminal']} 已不在运行，改为在同一工作区启动新的执行 agent（改动与汇报仍在工作区里）")
    if result is None:
        result = orca(*start, *fresh, allow_unknown=True)
    s.setdefault("history", []).append({"dispatch_id": s["dispatch_id"], "task_id": s["task_id"]})
    s["dispatch_id"], s["task_id"] = result["dispatchId"], result["taskId"]
    s["terminal"] = next((e["id"] for e in result.get("effects", []) if e.get("kind") == "terminal"), None) or s.get("terminal")
    s["fix_rounds"] = s.get("fix_rounds", 0) + 1
    s.pop("accepted_candidate", None)
    set_status(root, s, "running")
    if s["workspace"] == "worktree":
        orca("worktree", "set", "--worktree", f"path:{s['worktree']}", "--workspace-status", "in-progress", check=False)


def read_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def handle_worker_done(root: Path, msg: dict) -> dict | None:
    payload = msg.get("payload")
    payload = json.loads(payload) if isinstance(payload, str) else (payload or {})
    for s in all_states(root):
        if s["dispatch_id"] == payload.get("dispatchId") and s["status"] != "done":
            if s["status"] == "running":
                set_status(root, s, "awaiting_accept" if payload.get("outcome") == "succeeded" else "failed")
                say(f"{s['id']}: 执行 agent 回报 {payload.get('outcome')}")
            return s
    return None


def evaluate(root: Path, cfg: dict, s: dict) -> tuple[str, str]:
    """自动验收一次。返回 (accepted|fix|human, 说明)：fix 表示可以打回给执行 agent 自己修。"""
    if not report_path(root, s).is_file():
        return "fix", "没有找到汇报文件 report.md，请按派工说明补写。"
    g = guard(root, cfg, s)
    if g["outside"]:
        return "human", "；".join(g["outside"])
    if g["problems"]:
        if s["workspace"] == "main":  # 主工作区里分不清是谁改的，交给人判断
            return "human", "；".join(g["problems"])
        return "fix", "改动越出合同范围，请撤回这些文件的改动：" + "；".join(g["problems"])
    v = verify(root, cfg, s)
    if not v["ok"]:
        failed = [r for r in v["results"] if r["exit"] != 0]
        detail = "\n".join(
            f"$ {r['command']}（退出码 {r['exit']}，调度端 node {NODE_VERSION}，完整日志 {r.get('log', '')}）\n"
            + ("首个失败：\n" + "\n".join(r["first_failure"]) + "\n结尾：\n" if r.get("first_failure") else "")
            + "\n".join(r["tail"]) for r in failed)
        return "fix", "验收命令未通过，请修复：\n" + detail
    accept(root, cfg, s)
    return "accepted", f"改动 {len(g['files'])} 个文件，guard 与 verify 通过"


# ───────────────────────── 收尾与回收 ─────────────────────────

def cleanup(root: Path, cfg: dict, s: dict, outcome: str = "completed", note: str | None = None) -> None:
    orca("orchestration", "worker-release", "--dispatch", s["dispatch_id"], check=False)
    orca("orchestration", "task-update", "--id", s["task_id"], "--status", outcome,
         "--result", note or f"orca-multi {s['id']} 已完成", check=False)
    if s["workspace"] != "worktree":
        return
    wt = Path(s["worktree"]).resolve()
    legacy = (root / cfg["worktree_dir"]).resolve() in wt.parents
    orca_owned = s.get("worktree_owner") == "orca" and wt in {p.resolve() for p in worktree_paths(root)}
    if wt == root.resolve() or not (legacy or orca_owned):
        raise OrcaMultiError(f"拒绝删除既不在 {cfg['worktree_dir']}/ 下、也不是本工具让 Orca 创建的目录: {wt}")
    shared = [o["id"] for o in all_states(root) if o["id"] != s["id"] and Path(o["worktree"]).resolve() == wt and o["status"] != "done"]
    if shared:
        raise OrcaMultiError(f"worktree 仍被其他任务使用: {shared}")
    if wt.exists():
        orca("worktree", "rm", "--worktree", f"path:{wt}", "--force", check=False)
        for _ in range(30):  # Orca 的删除是异步的
            if not wt.exists():
                break
            time.sleep(0.5)
    if wt.exists():
        raise OrcaMultiError(f"worktree {wt} 删除未完成，稍后用 gc --apply 重试")


@contextlib.contextmanager
def merge_lock(root: Path):
    """主工作区同一时间只允许一个 finish 合并（含合并后验收），并发 finish 会排队等待。"""
    path = root / runs_rel() / ".merge.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("另一个 finish 正在合并主工作区，等待其完成…")
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def finish(root: Path, cfg: dict, s: dict, message: str | None, yes: bool) -> None:
    if s["workspace"] == "worktree" and yes:
        with merge_lock(root):
            _finish(root, cfg, read_state(root, s["id"]) or s, message, yes)
    else:
        _finish(root, cfg, s, message, yes)


def _finish(root: Path, cfg: dict, s: dict, message: str | None, yes: bool) -> None:
    if s["status"] != "accepted":
        raise OrcaMultiError("只有已验收（accepted）的任务才能收尾")
    files = task_files(root, cfg, s)
    if candidate_hash(Path(s["worktree"]), files) != s.get("accepted_candidate"):
        raise OrcaMultiError("验收之后改动又发生了变化：请重新验收")
    if s["workspace"] == "main":
        if not yes:
            say(f"{s['id']} 在主工作区完成，改动保持未提交：{files}。加 --yes 释放终端并结束任务。")
            return
        cleanup(root, cfg, s)
        set_status(root, s, "done")
        say(f"{s['id']} 已结束，改动留在主工作区，未提交")
        return
    # 合同和汇报是过程文件，不进提交；汇报复制回主工作区留档
    process_dir = f"{runs_rel()}/"  # 含复用链上前序任务的过程目录
    product = [f for f in files if not f.startswith(process_dir)]
    if not product:
        raise OrcaMultiError("除过程文件外没有可合并的改动")
    if not message:
        raise OrcaMultiError("worktree 任务收尾需要 --message 提交信息")
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).strip()
    if current in PROTECTED_BRANCHES:
        raise OrcaMultiError(f"主工作区当前在 {current} 分支：受保护分支只能由用户手动提交，请先切到工作分支")
    say(f"将提交 {len(product)} 个文件到分支 {s['branch']}，用 --no-ff 合并进主工作区当前分支，然后回收 worktree：")
    for f in product:
        say("  " + f)
    if not yes:
        say("这是预演；确认后加 --yes 执行。")
        return
    wt = Path(s["worktree"])
    if report_path(root, s).is_file():
        (run_dir(root, s["id"]) / "report.md").write_text(report_path(root, s).read_text(encoding="utf-8"), encoding="utf-8")
    # worktree 随后删除：复用链上前序任务的汇报、日志也拷回主工作区留档（不覆盖已有文件）
    for f in (wt / runs_rel()).rglob("*") if (wt / runs_rel()).is_dir() else []:
        dst = root / f.relative_to(wt)
        if f.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(f.read_bytes())
    git("add", "-A", "--", *product, cwd=wt)
    if run(["git", "diff", "--cached", "--quiet"], cwd=wt, check=False).returncode != 0:  # 上次合并失败后重跑时已提交过
        git("commit", "-q", "-m", message, cwd=wt)
    merge = run(["git", "merge", "--no-ff", "-q", "-m", f"merge: {s['id']}", s["branch"]], cwd=root, check=False)
    if merge.returncode != 0:
        run(["git", "merge", "--abort"], cwd=root, check=False)
        raise OrcaMultiError("合并失败，已中止合并并保留 worktree：\n" + (merge.stdout + merge.stderr).strip()[:600])
    cleanup(root, cfg, s)
    run(["git", "branch", "-d", s["branch"]], cwd=root, check=False)
    set_status(root, s, "done")
    say(f"{s['id']} 已合并并回收")
    post_merge_verify(root, cfg, s)


def post_merge_verify(root: Path, cfg: dict, s: dict) -> bool:
    """合并后在干净的临时 worktree 里用合并结果重跑验收命令。

    主工作区可能有别人未提交的改动，不在主工作区跑；失败不自动回退合并，只报告，由人决定。
    """
    task = load_task(root, s["id"])
    if not task["verify"]:
        return True
    tmp = root / cfg["worktree_dir"] / f"_verify-{s['id']}"
    git("worktree", "add", "-q", "--detach", str(tmp), "HEAD", cwd=root)
    try:
        for rel in cfg["link_paths"]:
            if (root / rel).exists() and not (tmp / rel).exists():
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                os.symlink(root / rel, tmp / rel)
        ok, lines = True, []
        evidence = run_dir(root, s["id"]) / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        for i, command in enumerate(task["verify"], 1):
            proc = subprocess.run(["bash", "-lc", command], cwd=tmp, text=True, capture_output=True)
            ok = ok and proc.returncode == 0
            lines.append(f"[{'通过' if proc.returncode == 0 else '失败'}] {command}")
            if proc.returncode != 0:
                # 完整输出留档；临时 worktree 随后删除，只看末尾几行无法定位失败的用例
                full = evidence / f"post-merge-verify-{i}.out"
                full.write_text(proc.stdout + proc.stderr, encoding="utf-8")
                lines.extend("    " + ln for ln in (proc.stdout + proc.stderr).strip().splitlines()[-6:])
                lines.append(f"    完整输出: {full}")
        (evidence / "post-merge-verify.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        say("合并后验收（干净目录、合并后的代码）：" + ("通过" if ok else "失败，合并未回退，请处理"))
        for ln in lines:
            say("  " + ln)
        return ok
    finally:
        for rel in cfg["link_paths"]:
            if (tmp / rel).is_symlink():
                (tmp / rel).unlink()
        run(["git", "worktree", "remove", "--force", str(tmp)], cwd=root, check=False)


def abandon(root: Path, cfg: dict, s: dict, yes: bool) -> None:
    if s["status"] == "done":
        raise OrcaMultiError("任务已结束")
    workdir = Path(s["worktree"])
    files = task_files(root, cfg, s) if workdir.exists() else []
    if s["workspace"] == "main":
        say(f"将放弃 {s['id']}：释放终端；主工作区里的改动 {files[:10]} 不会自动还原（可能混有别人的改动），由人决定。")
    else:
        say(f"将放弃 {s['id']}：丢弃 worktree 里 {len(files)} 个文件的改动 {files[:10]}，删除分支 {s['branch']}，释放终端。")
    if not yes:
        say("这是预演；确认后加 --yes 执行。")
        return
    evidence = run_dir(root, s["id"]) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    if s["workspace"] == "worktree" and workdir.exists() and files:
        git("add", "-A", "--", *files, cwd=workdir)  # 只为让补丁带上新文件，worktree 随后删除
        (evidence / "abandoned.patch").write_text(git("diff", "--cached", "--binary", s["base"], cwd=workdir), encoding="utf-8")
    cleanup(root, cfg, s, outcome="failed", note=f"orca-multi {s['id']} 已放弃")
    if s["workspace"] == "worktree":
        run(["git", "branch", "-D", s["branch"]], cwd=root, check=False)
    s["status"] = "done"
    s["abandoned"] = True
    write_state(root, s)
    say(f"{s['id']} 已放弃" + ("（补丁存于 evidence/abandoned.patch，可用 git apply 找回）" if s["workspace"] == "worktree" else ""))


# ───────────────────────── 自动执行 ─────────────────────────

class WaiterBusy(OrcaMultiError):
    """同一个 Orca 编排 run 只允许一个等待者：另一个 orca-multi run/wait 正在收消息。"""


def check_messages(window_ms: int) -> dict:
    try:
        return orca("orchestration", "check", "--wait", "--types", "worker_done,escalation,question,heartbeat",
                    "--timeout-ms", str(window_ms)) or {}
    except OrcaMultiError as exc:
        if "waiter_exists" in str(exc):
            raise WaiterBusy("另一个 orca-multi run/wait 正在等待消息") from exc
        if '"_keepalive"' in str(exc) and '"ok": false' not in str(exc) and '"ok":false' not in str(exc):
            # 等待连接中途断开，只收到心跳：当作本轮没有消息，由调用方继续下一轮
            say("等待消息的连接中途断开（只收到心跳），继续等待")
            return {}
        raise


def auto_run(root: Path, cfg: dict, ids: list[str], timeout_s: int, max_rounds: int, redo: bool = False) -> int:
    """启动→等待→校验→打回或验收，直到每个任务都停在“已验收 / 需人工 / 失败”。

    返回 0 全部验收；3 有问题要调度端回答；1 有任务需人工处理；2 超时（只是检查点，再跑一次继续）。
    """
    for tid in ids:
        load_task(root, tid)  # 先校验全部合同，避免启动一半才发现合同错误
    deadline = time.time() + timeout_s
    # 没有状态的任务，以及已放弃（abandoned）的任务都需要（重新）启动；spawn 本身支持重派 done 任务
    pending = [t for t in ids if not read_state(root, t) or (redo and read_state(root, t).get("abandoned"))]
    for t in ids:
        st = read_state(root, t)
        if st and st.get("abandoned") and not redo:
            say(f"{t}: 已放弃/已结束，不会重新启动；确需重做请加 --redo")
    for t in ids:  # 把主工作区任务目录里后来新增或改动的参考材料同步给仍在工作的 agent
        st = read_state(root, t)
        if st and st["status"] in ACTIVE_STATUSES | {"awaiting_accept", "failed"}:
            n = sync_task_files(root, st)
            if n:
                say(f"{t}: 已把任务目录里 {n} 个附件同步到执行 agent 的 worktree")
    needs_human: dict[str, str] = {}
    new_questions: list[dict] = []
    waiting = unanswered_questions(root)
    if waiting:
        say(f"仍有 {len(waiting)} 个提问未回答（不会因此退出）：" + "、".join(sorted(waiting)))
    while True:
        for tid in list(pending):  # 有名额就启动，没名额就排队
            try:
                spawn(root, cfg, tid, redo=redo)
                pending.remove(tid)
            except OrcaMultiError as exc:
                if "暂不能启动" not in str(exc):
                    raise
                say(f"{tid} 排队：{exc}")
        # 已回报但还没处理的（例如上次 run 中断）先补做验收
        for s in all_states(root):
            if s["id"] in ids and s["status"] == "awaiting_accept" and s["id"] not in needs_human:
                handle_evaluation(root, cfg, s, max_rounds, needs_human)
        if new_questions:
            # 同一批消息里的交付、验收和排队启动都已处理完，才退出通知调度端回答
            for m in new_questions:
                say(f"执行 agent 提问 [{m.get('type')}] {m.get('id')}: {m.get('subject')}\n  {m.get('body')}\n"
                    f"  回答: orca-multi reply {m.get('id')} \"<内容>\"，然后再运行 run")
            return 3
        mine = [s for s in all_states(root) if s["id"] in ids]
        if not any(s["status"] == "running" for s in mine):
            if pending:
                say(f"无法启动 {pending}：上限已满且没有进行中的任务，先收尾或放弃已完成的任务")
            break
        if time.time() >= deadline:
            say("等待超时（只是检查点，再运行一次 run 会继续）")
            return 2
        window = int(min(cfg["wait_window_ms"], max(1000, (deadline - time.time()) * 1000)))
        try:
            res = check_messages(window)
        except WaiterBusy:
            say("另一个 orca-multi run 正在等待消息（同一时间只能有一个）。已启动的任务不受影响；"
                "等那个 run 结束后，用一个 run 带上所有进行中的任务号继续。")
            return 2
        msgs = res.get("messages", [])
        # 已经 reply 过或已经上报过的提问只需签收，不再上报
        seen = answered_ids(root) | set(asked_questions(root))
        new_questions = [m for m in msgs if m.get("type") in ("question", "escalation") and m.get("id") not in seen]
        for m in msgs:
            if m.get("type") == "worker_done":
                handle_worker_done(root, m)
        record_questions(root, new_questions)
        # 提问已记入 .asked.json，可以签收整批（含心跳），避免重复投递和心跳积压打扰协调会话
        if res.get("deliveryId"):
            orca("orchestration", "check", "--ack", res["deliveryId"])
    print_summary(root, ids, needs_human)
    states = [read_state(root, t) for t in ids]
    skipped = [t for t, st in zip(ids, states) if st and st.get("abandoned") and not redo]
    return 0 if not skipped and all(s and s["status"] in ("accepted", "done") for s in states) else 1


def handle_evaluation(root: Path, cfg: dict, s: dict, max_rounds: int, needs_human: dict) -> None:
    verdict, detail = evaluate(root, cfg, s)
    if verdict == "accepted":
        say(f"{s['id']}: 已验收（{detail}）")
    elif verdict == "fix" and s.get("fix_rounds", 0) < max_rounds:
        reject(root, s, detail)
        say(f"{s['id']}: 自动打回第 {s['fix_rounds']} 轮：{detail.splitlines()[0]}")
    else:
        needs_human[s["id"]] = detail
        say(f"{s['id']}: 需人工处理：{detail.splitlines()[0]}")


def print_summary(root: Path, ids: list[str], needs_human: dict) -> None:
    say("── 汇总 ──")
    for tid in ids:
        s = read_state(root, tid)
        if not s:
            say(f"- {tid}: 未启动")
            continue
        where = "主工作区" if s["workspace"] == "main" else s["worktree"]
        say(f"- {tid}: {s['status']}  工作区={where}  打回 {s.get('fix_rounds', 0)} 轮" +
            (f"  需人工：{needs_human[tid]}" if tid in needs_human else ""))


# ───────────────────────── 子命令 ─────────────────────────

def cmd_run(args, root, cfg):
    return auto_run(root, cfg, args.ids, args.timeout_s,
                    args.max_rounds if args.max_rounds is not None else cfg["max_fix_rounds"], args.redo)


def cmd_baseline(args, root, cfg):
    bad = 0
    for tid in args.ids:
        data = run_baseline(root, cfg, tid, args.timeout_s)
        failed = [r for r in data["results"] if r["exit"] != 0]
        bad += bool(failed)
        say(f"{tid}: 基线 {data['head'][:8]}，{len(data['results'])} 条 verify，失败 {len(failed)} 条（已记录，run 时会写进派工说明）")
        for r in failed:
            say(f"  ✗ {r['command']}（退出码 {r['exit']}）")
            for ln in (r["first_failure"] or r["tail"])[:4]:
                say(f"      {ln}")
    return 0


def cmd_lint(args, root, cfg):
    total = 0
    for tid in args.ids:
        warnings = lint_contract(root, tid)
        total += len(warnings)
        say(f"{tid}: " + ("合同检查未发现可疑项" if not warnings else f"{len(warnings)} 条提示"))
        for w in warnings:
            say(f"  ! {w}")
    return 1 if (total and args.strict) else 0


def cmd_plan(args, root, cfg):
    planned: list[dict] = []
    for tid in args.ids:
        p = plan_task(root, cfg, tid, planned)
        if p["can_start"]:
            planned.append({"id": tid, "workspace": p["workspace"], "allowed_files": p["task"]["allowed_files"],
                            "new_worktree": p["workspace"] == "worktree" and not p["reuse"]})
        where = workspace_label(p)
        say(f"- {tid}: {where}（{p['reason']}）" + ("" if p["can_start"] else f"  暂不能启动：{p['blocked']}"))
    return 0


def cmd_spawn(args, root, cfg):
    spawn(root, cfg, args.id, args.dry_run, args.redo)
    return 0


def cmd_status(args, root, cfg):
    rows = []
    for s in all_states(root):
        if args.id and s["id"] != args.id:
            continue
        if s["status"] == "done" and not args.all and not args.id:
            continue
        files = task_files(root, cfg, s) if s["status"] != "done" and Path(s["worktree"]).exists() else []
        rows.append({k: s.get(k) for k in ("id", "status", "workspace", "terminal", "worktree")} | {"files": len(files)})
    summary = {"worktrees": len(managed_worktrees(root, cfg)), "max_worktrees": cfg["max_worktrees"],
               "active_workers": len(active_states(root)), "max_active_workers": cfg["max_active_workers"]}
    if args.json:
        say(json.dumps({"summary": summary, "tasks": rows}, ensure_ascii=False, indent=2))
        return 0
    say(f"worktree {summary['worktrees']}/{summary['max_worktrees']}  并发 worker {summary['active_workers']}/{summary['max_active_workers']}")
    for r in rows or []:
        where = "主工作区" if r["workspace"] == "main" else r["worktree"]
        say(f"- {r['id']}  [{r['status']}]  {where}  改动 {r['files']} 个文件  终端={r['terminal'] or '-'}")
    if not rows:
        say("暂无进行中的 orca-multi 任务（--all 查看已结束的）")
    return 0


def cmd_view(args, root, cfg):
    s = need_state(root, args.id)
    if args.open:
        orca("terminal", "switch", "--terminal", s["terminal"])
        say("已在 Orca 里切换到该终端")
    out = orca("terminal", "read", "--terminal", s["terminal"], "--limit", str(args.limit))
    term = out.get("terminal", out) if isinstance(out, dict) else {}
    tail = term.get("tail") if isinstance(term, dict) else None
    say("\n".join(tail) if tail else json.dumps(out, ensure_ascii=False)[:2000])
    return 0


def cmd_send(args, root, cfg):
    s = need_state(root, args.id)
    orca("orchestration", "send", "--to", f"dispatch:{s['dispatch_id']}", "--subject", args.subject or "调度端指令",
         "--body", args.text, "--task-id", s["task_id"], "--dispatch-id", s["dispatch_id"])
    say(f"已向 {args.id} 发送指令")
    return 0


def add_list_items(text: str, key: str, items: list[str]) -> str:
    """在合同头部的列表字段末尾追加条目（已存在的跳过）；字段不存在则新增，`key: []` 会展开成列表。"""
    items = [i for i in items if i]
    if not items:
        return text
    lines = text.splitlines()
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    existing = set(parse_task(text).get(key, []))
    new = [f"  - {i}" for i in items if i not in existing]
    if not new:
        return text
    idx = next((i for i in range(1, end) if re.match(rf"^{key}:\s*(\[\])?\s*$", lines[i])), None)
    if idx is None:
        lines[end:end] = [f"{key}:"] + new
    else:
        lines[idx] = f"{key}:"
        j = idx + 1
        while j < end and (lines[j].lstrip().startswith("- ") or not lines[j].strip()):
            j += 1
        lines[j:j] = new
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def amend(root: Path, s: dict, allow: list[str], verify_add: list[str], note: str | None) -> None:
    """修改进行中任务的合同：同步主工作区与 worktree 两份，并通知执行 agent 重读。"""
    path = run_dir(root, s["id"]) / "task.md"
    before = path.read_text(encoding="utf-8")
    text = add_list_items(add_list_items(before, "allowed_files", allow), "verify", verify_add)
    if text == before and not note:
        say("合同没有变化")
        return
    parse_task(text)  # 写入前校验
    path.write_text(text, encoding="utf-8")
    wt = Path(s["worktree"])
    rel = f"{runs_rel()}/{s['id']}/task.md"
    if wt.resolve() != root.resolve() and wt.exists():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text, encoding="utf-8")
    s["contract_version"] = s.get("contract_version", 1) + 1
    write_state(root, s)
    lines = [f"调度端已更新合同（版本 {s['contract_version']}），请重读 {rel} 后按新合同继续："]
    lines += [f"- allowed_files 新增: {a}" for a in allow] + [f"- verify 新增: {v}" for v in verify_add]
    if note:
        lines.append(f"- 说明: {note}")
    if s["status"] == "running":
        orca("orchestration", "send", "--to", f"dispatch:{s['dispatch_id']}", "--subject", "合同已更新",
             "--body", "\n".join(lines), "--task-id", s["task_id"], "--dispatch-id", s["dispatch_id"])
        say(f"已更新 {s['id']} 的合同并通知执行 agent（版本 {s['contract_version']}）")
    else:
        say(f"已更新 {s['id']} 的合同（版本 {s['contract_version']}）；任务不在运行中，未发送通知")


def cmd_amend(args, root, cfg):
    amend(root, need_state(root, args.id), args.allow or [], args.verify_add or [], args.note)
    return 0


def answered_path(root: Path) -> Path:
    return root / runs_rel() / ".answered.json"


def answered_ids(root: Path) -> set:
    p = answered_path(root)
    return set(json.loads(p.read_text(encoding="utf-8"))) if p.is_file() else set()


def asked_path(root: Path) -> Path:
    return root / runs_rel() / ".asked.json"


def asked_questions(root: Path) -> dict:
    """run 已上报过的提问 {id: {type, subject, body, at}}；签收后靠它避免重复上报。"""
    p = asked_path(root)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def record_questions(root: Path, msgs: list[dict]) -> None:
    if not msgs:
        return
    data = asked_questions(root)
    for m in msgs:
        data[m["id"]] = {"type": m.get("type"), "subject": m.get("subject"), "body": m.get("body"),
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    p = asked_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def unanswered_questions(root: Path) -> set:
    return set(asked_questions(root)) - answered_ids(root)


def cmd_reply(args, root, cfg):
    orca("orchestration", "reply", "--id", args.message_id, "--body", args.text)
    p = answered_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(answered_ids(root) | {args.message_id})) + "\n", encoding="utf-8")
    say("已回复")
    for st in all_states(root):  # 回复后执行 agent 马上会继续，先把新增的参考材料同步过去
        if st["status"] in ACTIVE_STATUSES:
            sync_task_files(root, st)
    return 0


def cmd_wait(args, root, cfg):
    res = check_messages(args.timeout_s * 1000)
    msgs = res.get("messages", [])
    blocked = False
    for m in msgs:
        if m.get("type") == "worker_done":
            handle_worker_done(root, m)
        elif m.get("type") in ("question", "escalation"):
            blocked = True
            say(f"[{m.get('type')}] {m.get('id')}: {m.get('subject')}\n  {m.get('body')}")
    if msgs and not blocked:
        orca("orchestration", "check", "--ack", res["deliveryId"])
    if not msgs:
        say("没有新消息")
    return 3 if blocked else 0


def cmd_guard(args, root, cfg):
    g = guard(root, cfg, need_state(root, args.id), args.ignore_main)
    say(f"guard {args.id}: {'通过' if g['ok'] else '失败'}；改动文件 {len(g['files'])} 个")
    for p in g["problems"] + g["outside"]:
        say("  ✗ " + p)
    return 0 if g["ok"] else 1


def cmd_verify(args, root, cfg):
    v = verify(root, cfg, need_state(root, args.id))
    for r in v["results"]:
        say(f"[{'通过' if r['exit'] == 0 else '失败'}] {r['command']}")
    return 0 if v["ok"] else 1


def cmd_accept(args, root, cfg):
    accept(root, cfg, need_state(root, args.id))
    say(f"已验收 {args.id}")
    return 0


def cmd_reject(args, root, cfg):
    s = need_state(root, args.id)
    before = s.get("terminal")
    reject(root, s, args.reason)
    say(f"已打回 {args.id}（" + ("同一终端继续修改" if s.get("terminal") == before else f"新终端 {s.get('terminal')}") + "）")
    return 0


def cmd_finish(args, root, cfg):
    finish(root, cfg, need_state(root, args.id), args.message, args.yes)
    return 0


def cmd_abandon(args, root, cfg):
    abandon(root, cfg, need_state(root, args.id), args.yes)
    return 0


def cmd_adopt(args, root, cfg):
    """把已经派出、但因异常没有记录状态的 Dispatch 接回管理（不会重新派工）。"""
    if read_state(root, args.id):
        raise OrcaMultiError(f"任务 {args.id} 已有状态记录，无需 adopt")
    task = load_task(root, args.id)
    if not snapshot_path(root, args.id).is_file():
        raise OrcaMultiError("缺少启动前快照，guard 无法判断越界；只能 abandon 后重新启动")
    shown = orca("orchestration", "worker-show", "--dispatch", args.dispatch)
    dispatch, worker = shown.get("dispatch", {}), shown.get("worker", {})
    wt = root / cfg["worktree_dir"] / args.id
    workspace = "worktree" if wt.exists() else "main"
    state = {
        "tool": "orca-multi", "id": args.id, "title": task["title"], "status": "running",
        "workspace": workspace, "worktree": str(wt if workspace == "worktree" else root),
        "branch": cfg["branch_prefix"] + args.id if workspace == "worktree" else None,
        "base": git("merge-base", "HEAD", cfg["branch_prefix"] + args.id, cwd=root).strip() if workspace == "worktree" else git("rev-parse", "HEAD", cwd=root).strip(),
        "allowed_files": task["allowed_files"], "run_id": dispatch.get("runId"),
        "task_id": dispatch.get("taskId"), "dispatch_id": args.dispatch,
        "terminal": worker.get("assigneeHandle") or dispatch.get("assigneeHandle"),
        "fix_rounds": 0, "adopted": True, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if not state["task_id"]:
        raise OrcaMultiError("Orca 没有返回该 Dispatch 的 taskId，无法接管")
    write_state(root, state)
    say(f"已接管 {args.id}：Dispatch {args.dispatch}，终端 {state['terminal']}，工作区 {state['worktree']}")
    return 0


def cmd_gc(args, root, cfg):
    states = all_states(root)
    live = {Path(s["worktree"]).resolve() for s in states if s["status"] != "done"}
    leftovers = [s for s in states if s["status"] == "done" and s["workspace"] == "worktree"
                 and Path(s["worktree"]).exists() and Path(s["worktree"]).resolve() not in live]
    orphans = [p for p in managed_worktrees(root, cfg) if p.resolve() not in live
               and all(Path(s["worktree"]).resolve() != p.resolve() for s in leftovers)]
    say(f"已结束但 worktree 还在: {[s['id'] for s in leftovers] or '无'}")
    say(f"没有任务记录的 worktree（只列出，不自动删除）: {[str(p) for p in orphans] or '无'}")
    say(f"等待处理（验收 / 收尾 / 排查）: {[s['id'] for s in states if s['status'] in ('accepted', 'awaiting_accept', 'failed')] or '无'}")
    if args.apply:
        for s in leftovers:
            cleanup(root, cfg, s)
            say(f"已回收 {s['id']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="orca-multi", description="用 Orca 调度执行 agent")
    ap.add_argument("--config", help="配置文件（默认 <项目>/.orca-multi/config.json）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("run", cmd_run, "自动执行：启动、等待、校验、打回或验收，直到全部停下")
    p.add_argument("ids", nargs="+")
    p.add_argument("--timeout-s", type=int, default=3600)
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--redo", action="store_true", help="重新启动已放弃/已结束的任务（默认不会，避免误重启）")
    p = add("baseline", cmd_baseline, "派工前在主工作区先跑一遍合同 verify，记录改动前就失败的命令（run 时写进派工说明）")
    p.add_argument("ids", nargs="+")
    p.add_argument("--timeout-s", type=int, default=1800, help="每条命令的超时（默认 1800 秒）")
    p = add("lint", cmd_lint, "合同检查（启发式）：提示可能受影响但没写进 allowed_files 的测试，以及 verify 里写到的文件")
    p.add_argument("ids", nargs="+")
    p.add_argument("--strict", action="store_true", help="有提示就以退出码 1 结束")
    p = add("plan", cmd_plan, "只看每个任务会用哪个工作区、能否启动")
    p.add_argument("ids", nargs="+")
    p = add("spawn", cmd_spawn, "只启动一个任务")
    p.add_argument("id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--redo", action="store_true", help="重新启动已结束的任务")
    p = add("status", cmd_status, "汇总任务与上限使用情况")
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p = add("view", cmd_view, "查看执行终端输出，--open 在 Orca 里切过去")
    p.add_argument("id")
    p.add_argument("--open", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    p = add("send", cmd_send, "向执行 agent 追加指令")
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--subject")
    p = add("amend", cmd_amend, "修改进行中任务的合同（同步两份副本并通知执行 agent）")
    p.add_argument("id")
    p.add_argument("--allow", action="append", metavar="GLOB", help="追加 allowed_files，可重复")
    p.add_argument("--verify-add", action="append", metavar="CMD", help="追加 verify 命令，可重复")
    p.add_argument("--note", help="附加说明，一并发给执行 agent")
    p = add("reply", cmd_reply, "回答执行 agent 的提问")
    p.add_argument("message_id")
    p.add_argument("text")
    p = add("wait", cmd_wait, "等一批 Orca 消息（不轮询）")
    p.add_argument("--timeout-s", type=int, default=900)
    p = add("guard", cmd_guard, "范围校验")
    p.add_argument("id")
    p.add_argument("--ignore-main", metavar="原因", help="主工作区的变化是人为造成的时，写明原因后忽略")
    p = add("verify", cmd_verify, "运行合同里的验收命令")
    p.add_argument("id")
    p = add("accept", cmd_accept, "验收（要求 guard 与 verify 针对当前改动通过）")
    p.add_argument("id")
    p = add("reject", cmd_reject, "打回给同一个执行 agent")
    p.add_argument("id")
    p.add_argument("reason")
    p = add("finish", cmd_finish, "收尾：worktree 任务提交合并并回收；主工作区任务只结束不提交（需要 --yes）")
    p.add_argument("id")
    p.add_argument("--message", help="提交信息（worktree 任务必填）")
    p.add_argument("--yes", action="store_true")
    p = add("abandon", cmd_abandon, "放弃任务并回收（需要 --yes）")
    p.add_argument("id")
    p.add_argument("--yes", action="store_true")
    p = add("adopt", cmd_adopt, "接管已派出但未记录状态的 Dispatch（不重派）")
    p.add_argument("id")
    p.add_argument("--dispatch", required=True)
    p = add("gc", cmd_gc, "列出残留，--apply 回收已结束任务的 worktree")
    p.add_argument("--apply", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = repo_root()
        CFG.update(load_config(root, args.config))
        return args.fn(args, root, CFG)
    except OrcaMultiError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

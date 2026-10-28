"""orca-multi 测试：纯逻辑 + 用假 orca 在临时 git 仓库里跑完整流程。

运行: python3 -m unittest discover -s ~/.claude/skills/orca-multi/scripts
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("orca_multi", HERE / "orca_multi.py")
om = importlib.util.module_from_spec(spec)
sys.modules["orca_multi"] = om
spec.loader.exec_module(om)


def contract(tid, allowed=("src/demo/**",), verify=("test -f src/demo/A.txt",), extra=""):
    allow = "allowed_files: []\n" if not allowed else "allowed_files:\n" + "".join(f"  - {a}\n" for a in allowed)
    ver = "verify:\n" + "".join(f"  - {v}\n" for v in verify) if verify else ""
    return f"---\nid: {tid}\ntitle: 演示 {tid}\n{allow}forbidden:\n  - src/demo/secret/**\n{ver}{extra}---\n正文\n"


class PureTest(unittest.TestCase):
    def test_parse(self):
        t = om.parse_task(contract("T-a"))
        self.assertEqual(t["allowed_files"], ["src/demo/**"])
        self.assertEqual(t["verify"], ["test -f src/demo/A.txt"])
        self.assertEqual(om.parse_task(contract("T-r", allowed=()))["allowed_files"], [])
        with self.assertRaises(om.OrcaMultiError):
            om.parse_task("---\nid: x\n---\n")
        with self.assertRaises(om.OrcaMultiError):
            om.parse_task(contract("T-b", extra="workspace: elsewhere\n"))

    def test_glob_and_scope(self):
        self.assertTrue(om.match_any("a/b/c.vue", ["a/**"]))
        self.assertFalse(om.match_any("a/b/c.vue", ["a/*.vue"]))
        self.assertTrue(om.match_any("z.js", ["**/z.js"]))
        self.assertTrue(om.match_any("a/b", ["a/"]))
        r = om.evaluate_scope(["src/demo/A", "src/demo/secret/k", "x.py", "runs/T/report.md"],
                              ["src/demo/**"], ["src/demo/secret/**"], ["runs/T/**"])
        self.assertEqual(r, {"out_of_scope": ["x.py"], "forbidden_hit": ["src/demo/secret/k"]})

    def test_overlap(self):
        self.assertTrue(om.patterns_overlap(["src/a/**"], ["src/a/b.js"]))
        self.assertTrue(om.patterns_overlap(["src/**"], ["src/a/*.js"]))
        self.assertFalse(om.patterns_overlap(["src/a/**"], ["src/b/**"]))

    def test_choose_workspace(self):
        w = om.parse_task(contract("T-w"))
        r = om.parse_task(contract("T-r", allowed=()))
        self.assertEqual(om.choose_workspace(w, [], [])[0], "main")
        self.assertEqual(om.choose_workspace(r, [], ["src/demo/A.txt"])[0], "main")
        self.assertEqual(om.choose_workspace(w, [], ["src/demo/A.txt"])[0], "worktree")
        self.assertEqual(om.choose_workspace(w, [], ["other.txt"])[0], "main")
        self.assertEqual(om.choose_workspace(w, [{"workspace": "main", "allowed_files": []}], [])[0], "worktree")
        self.assertEqual(om.choose_workspace(w, [{"workspace": "worktree", "allowed_files": ["src/**"]}], [])[0], "worktree")
        self.assertEqual(om.choose_workspace(w, [{"workspace": "worktree", "allowed_files": ["lib/**"]}], [])[0], "main")
        self.assertEqual(om.choose_workspace(w, [], [], default="worktree")[0], "worktree")
        forced = om.parse_task(contract("T-f", extra="workspace: main\n"))
        with self.assertRaises(om.OrcaMultiError):
            om.choose_workspace(forced, [{"workspace": "main", "allowed_files": []}], [])

    def test_agent_launch_args_by_agent(self):
        args, model, effort = om.agent_launch_args("codex", "gpt-6.1-sol", "high", False)
        self.assertEqual(args, ["--agent", "codex", "--model", "gpt-6.1-sol", "--effort", "high"])
        args, model, effort = om.agent_launch_args("zcode", "gpt-6.1-sol", "high", False)
        self.assertEqual(args, ["--agent", "zcode"])  # zcode 用自身配置的模型
        self.assertEqual((model, effort), ("zcode 自身配置", None))
        args, _, effort = om.agent_launch_args("omp", "m1", "high", False)
        self.assertEqual(args, ["--agent", "omp", "--model", "m1"])
        self.assertIsNone(effort)

    def test_limits(self):
        cfg = {"max_worktrees": 1, "max_active_workers": 2}
        self.assertTrue(om.can_spawn(1, 0, cfg, needs_worktree=False)[0])
        self.assertFalse(om.can_spawn(1, 0, cfg, needs_worktree=True)[0])
        self.assertFalse(om.can_spawn(0, 2, cfg, needs_worktree=False)[0])

    def test_transitions(self):
        om.check_transition("running", "awaiting_accept")
        with self.assertRaises(om.OrcaMultiError):
            om.check_transition("running", "accepted")

    def test_spec_mentions_only_contract_verify(self):
        s = om.build_spec("T", "runs/T", "main")
        self.assertIn("只运行合同 verify", s)
        self.assertIn("主工作区", s)

    def test_spec_states_report_exception_dependency_ban_and_verify_conflict_rule(self):
        s = om.build_spec("T", "runs/T", "worktree")
        self.assertIn("唯一例外", s)                  # 汇报文件是 allowed_files 的例外
        self.assertIn("不要安装依赖", s)
        self.assertIn("node_modules", s)
        self.assertIn("git status", s)                # 允许只读检查
        self.assertIn("与以上规则冲突", s)             # verify 与规则冲突时先问
        self.assertIn("ask", s)


FAKE_ORCA = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_ORCA_LOG"], "a").write(json.dumps(args, ensure_ascii=False) + "\n")
a = " ".join(args)
n = sum(1 for _ in open(os.environ["FAKE_ORCA_LOG"]))
def out(r): print(json.dumps({"ok": True, "result": r}))
def opt(name):
    return args[args.index(name) + 1] if name in args else None
if "run-current" in a: out({"run": {"id": "run_1"}})
elif args[:2] == ["worktree", "create"]:
    # 模拟 Orca 原生创建：建在仓库外的 Orca 工作区目录，分支名即 --name
    import subprocess
    repo = opt("--repo").split(":", 1)[1]
    path = os.path.join(os.environ["FAKE_ORCA_WS"], opt("--name"))
    subprocess.run(["git", "worktree", "add", "-q", "-b", opt("--name"), path, opt("--base-branch")], cwd=repo, check=True)
    out({"worktree": {"path": path, "branch": "refs/heads/" + opt("--name")}})
elif "worker-start" in a:
    out({"runId": "run_1", "taskId": f"task_{n}", "dispatchId": f"disp_{n}", "effects": [{"kind": "terminal", "id": f"term_{n}"}]})
else: out({})
'''


class FlowBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "src/demo").mkdir(parents=True)
        self.sh("git", "init", "-q", "-b", "dev")
        self.sh("git", "config", "user.email", "t@example.com")
        self.sh("git", "config", "user.name", "t")
        (self.root / "src/demo/old.txt").write_text("o\n")
        (self.root / "README.md").write_text("r\n")
        self.sh("git", "add", "-A")
        self.sh("git", "commit", "-qm", "init")
        fake = Path(self.tmp.name) / "orca"
        fake.write_text(FAKE_ORCA)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = Path(self.tmp.name) / "orca.log"
        self.log.write_text("")
        os.environ["FAKE_ORCA_LOG"] = str(self.log)
        os.environ["FAKE_ORCA_WS"] = str(Path(self.tmp.name) / "orca-workspaces")
        om.ORCA = str(fake)
        self.cfg = om.load_config(self.root)
        self.cfg.update(link_paths=[], max_worktrees=1)
        om.CFG.clear()
        om.CFG.update(self.cfg)
        self.orig_check, self.orig_orca = om.check_messages, om.orca
        # 假 Orca 的 worktree rm 不删目录，这里用 git 模拟 Orca 的异步删除
        def orca(*a, **k):
            if a[:2] == ("worktree", "rm"):
                subprocess.run(["git", "worktree", "remove", "--force", a[3].split(":", 1)[1]], cwd=self.root, check=True)
            return self.orig_orca(*a, **k)
        om.orca = orca

    def tearDown(self):
        om.check_messages, om.orca = self.orig_check, self.orig_orca
        self.tmp.cleanup()

    def sh(self, *cmd):
        return subprocess.run(cmd, cwd=self.root, check=True, capture_output=True, text=True).stdout

    def write_contract(self, tid, **kw):
        d = self.root / om.runs_rel() / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.md").write_text(contract(tid, **kw))

    def agent(self, actions):
        """actions: 每次 check 时依次执行的 (任务号, 动作函数)；动作在任务工作区里改文件，然后回报 worker_done。"""
        queue = list(actions)

        def check(_ms):
            if not queue:
                return {"messages": []}
            tid, act = queue.pop(0)
            s = om.read_state(self.root, tid)
            wd = Path(s["worktree"])
            act(wd)
            rep = wd / om.runs_rel() / tid / "report.md"
            rep.parent.mkdir(parents=True, exist_ok=True)
            rep.write_text("done\n")
            return {"deliveryId": "d", "messages": [{"type": "worker_done", "payload": {"dispatchId": s["dispatch_id"], "outcome": "succeeded"}}]}
        om.check_messages = check


class Flow(FlowBase):
    def test_main_workspace_flow_without_commit(self):
        self.write_contract("T-m", verify=("test -f src/demo/A.txt",))
        self.agent([("T-m", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-m"], 60, 2), 0)
        s = om.read_state(self.root, "T-m")
        self.assertEqual((s["workspace"], s["status"]), ("main", "accepted"))
        self.assertEqual(om.managed_worktrees(self.root, self.cfg), [])
        head = self.sh("git", "rev-parse", "HEAD")
        om.finish(self.root, self.cfg, s, None, yes=True)
        self.assertEqual(self.sh("git", "rev-parse", "HEAD"), head)  # 不提交
        self.assertTrue((self.root / "src/demo/A.txt").is_file())
        self.assertEqual(om.read_state(self.root, "T-m")["status"], "done")

    def test_auto_reject_then_accept(self):
        self.write_contract("T-f", verify=("grep -q good src/demo/A.txt",))
        self.agent([("T-f", lambda wd: (wd / "src/demo/A.txt").write_text("bad\n")),
                    ("T-f", lambda wd: (wd / "src/demo/A.txt").write_text("good\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-f"], 60, 2), 0)
        s = om.read_state(self.root, "T-f")
        self.assertEqual((s["status"], s["fix_rounds"]), ("accepted", 1))
        log = self.log.read_text()
        self.assertIn("验收命令未通过", log)
        self.assertNotIn('"send", "--to"', log)  # 原 Dispatch 已结束，打回必须新开派工
        self.assertIn('"--terminal"', log)
        self.assertEqual(len(s["history"]), 1)

    def test_rounds_exhausted_needs_human(self):
        self.write_contract("T-x", verify=("false",))
        self.agent([("T-x", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))] * 3)
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-x"], 60, 1), 1)
        self.assertEqual(om.read_state(self.root, "T-x")["fix_rounds"], 1)

    def test_main_out_of_scope_goes_to_human(self):
        self.write_contract("T-o")
        def act(wd):
            (wd / "src/demo/A.txt").write_text("a\n")
            (wd / "README.md").write_text("改了范围外\n")
        self.agent([("T-o", act)])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-o"], 60, 2), 1)
        self.assertEqual(om.read_state(self.root, "T-o")["status"], "awaiting_accept")
        self.assertNotIn("调度端验收未通过", self.log.read_text())

    def test_dirty_overlap_uses_worktree_then_merge(self):
        (self.root / "src/demo/old.txt").write_text("别人未提交的改动\n")
        self.write_contract("T-w")
        self.agent([("T-w", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-w"], 60, 2), 0)
        s = om.read_state(self.root, "T-w")
        self.assertEqual(s["workspace"], "worktree")
        self.assertEqual(Path(s["worktree"]).parent.name, "orca-workspaces")  # Orca 原生创建，不在仓库内隐藏目录
        self.assertEqual(s["worktree_owner"], "orca")
        log = self.log.read_text()
        self.assertIn('"worktree", "create"', log)
        self.assertIn("--parent-worktree", log)
        self.assertNotIn('"--parent-worktree", "current"', log)
        om.finish(self.root, self.cfg, s, "feat: demo", yes=True)
        self.assertTrue((self.root / "src/demo/A.txt").is_file())
        self.assertIn("merge: T-w", self.sh("git", "log", "-1", "--format=%s"))
        self.assertFalse(Path(s["worktree"]).exists())
        self.assertEqual((self.root / "src/demo/old.txt").read_text(), "别人未提交的改动\n")
        self.assertNotIn("T-w/report.md", self.sh("git", "show", "--stat", "HEAD^2"))
        log = (self.root / om.runs_rel() / "T-w/evidence/post-merge-verify.log").read_text()
        self.assertIn("[通过]", log)
        self.assertNotIn("_verify-", self.sh("git", "worktree", "list"))

    def test_parallel_second_task_queues_on_worktree_limit(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-1")
        self.write_contract("T-2")
        self.agent([("T-1", lambda wd: (wd / "src/demo/A.txt").write_text("1\n"))])
        rc = om.auto_run(self.root, self.cfg, ["T-1", "T-2"], 60, 2)
        self.assertEqual(rc, 1)  # T-2 因 worktree 上限 1 未能启动
        self.assertIsNone(om.read_state(self.root, "T-2"))
        self.assertEqual(om.read_state(self.root, "T-1")["status"], "accepted")

    def test_answered_question_is_acked_not_reported_again(self):
        self.write_contract("T-q")
        s = om.spawn(self.root, self.cfg, "T-q")
        om.answered_path(self.root).write_text('["msg_q"]')
        calls = []
        def check(_ms):
            calls.append(1)
            if len(calls) == 1:  # 已回答的提问被重新投递
                return {"deliveryId": "d1", "messages": [{"type": "question", "id": "msg_q"}, {"type": "heartbeat", "id": "msg_h"}]}
            wd = Path(s["worktree"])
            (wd / "src/demo/A.txt").write_text("a\n")
            (wd / om.runs_rel() / "T-q" / "report.md").write_text("done\n")
            return {"deliveryId": "d2", "messages": [{"type": "worker_done", "payload": {"dispatchId": s["dispatch_id"], "outcome": "succeeded"}}]}
        om.check_messages = check
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-q"], 60, 2), 0)
        self.assertIn('"--ack", "d1"', self.log.read_text())

    def test_parallel_worktrees_do_not_flag_each_other(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.cfg["max_worktrees"] = 2
        self.write_contract("T-p1")
        self.write_contract("T-p2", allowed=("lib/**",), verify=("test -f lib/B.txt",))
        def act1(wd):
            (wd / "src/demo/A.txt").write_text("a\n")
            (Path(om.read_state(self.root, "T-p2")["worktree"]) / "lib").mkdir(exist_ok=True)
            (Path(om.read_state(self.root, "T-p2")["worktree"]) / "lib/B.txt").write_text("b\n")
        self.agent([("T-p1", act1), ("T-p2", lambda wd: None)])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-p1", "T-p2"], 60, 2), 0)

    def test_finish_refuses_protected_branch(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-m2")
        self.agent([("T-m2", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-m2"], 60, 2), 0)
        self.sh("git", "branch", "-m", "dev", "main")
        head = self.sh("git", "rev-parse", "HEAD")
        with self.assertRaises(om.OrcaMultiError):
            om.finish(self.root, self.cfg, om.read_state(self.root, "T-m2"), "feat: x", yes=True)
        self.assertEqual(self.sh("git", "rev-parse", "HEAD"), head)

    def test_abandon_worktree_keeps_patch(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-a")
        s = om.spawn(self.root, self.cfg, "T-a")
        (Path(s["worktree"]) / "src/demo/New.txt").write_text("n\n")
        om.abandon(self.root, self.cfg, s, yes=True)
        self.assertFalse(Path(s["worktree"]).exists())
        self.assertIn("New.txt", (self.root / om.runs_rel() / "T-a/evidence/abandoned.patch").read_text())
        self.assertNotIn("T-a", self.sh("git", "branch"))

    def test_cleanup_never_removes_project_root(self):
        bogus = {"id": "T-z", "workspace": "worktree", "worktree": str(self.root), "dispatch_id": "d", "task_id": "t"}
        with self.assertRaises(om.OrcaMultiError):
            om.cleanup(self.root, self.cfg, bogus)
        self.assertTrue(self.root.exists())

    def test_worktree_guard_catches_main_change(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-g")
        def act(wd):
            (wd / "src/demo/A.txt").write_text("a\n")
            (self.root / "README.md").write_text("越界写主工作区\n")
        self.agent([("T-g", act)])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-g"], 60, 2), 1)
        g = json.loads((self.root / om.runs_rel() / "T-g/guard.json").read_text())
        self.assertTrue(any("主工作区" in o for o in g["outside"]))

    def test_main_stays_busy_until_main_task_done(self):
        """主工作区任务交付后未结束前，新可写任务不能再进主工作区（否则两边互相算越界）。"""
        self.cfg["max_worktrees"] = 2
        self.write_contract("T-m1")
        self.agent([("T-m1", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-m1"], 60, 2), 0)
        self.assertEqual(om.read_state(self.root, "T-m1")["status"], "accepted")
        self.write_contract("T-m2", allowed=("lib/**",), verify=())
        p = om.plan_task(self.root, self.cfg, "T-m2")
        self.assertEqual(p["workspace"], "worktree")
        om.finish(self.root, self.cfg, om.read_state(self.root, "T-m1"), None, yes=True)
        self.assertEqual(om.plan_task(self.root, self.cfg, "T-m2")["workspace"], "main")

    def test_reuse_chain_process_files_not_out_of_scope_nor_committed(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.cfg["max_worktrees"] = 2
        self.write_contract("T-c1")
        self.write_contract("T-c2", extra="reuse: T-c1\n", verify=("test -f src/demo/B.txt",))
        self.agent([("T-c1", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-c1"], 60, 2), 0)
        self.agent([("T-c2", lambda wd: (wd / "src/demo/B.txt").write_text("b\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-c2"], 60, 2), 0)
        s = om.read_state(self.root, "T-c2")
        self.assertEqual(s["status"], "accepted")
        om.finish(self.root, self.cfg, s, "feat: chain", yes=True)
        stat = self.sh("git", "show", "--stat", "--format=", "HEAD^2")
        self.assertIn("A.txt", stat)
        self.assertIn("B.txt", stat)
        self.assertNotIn(om.runs_rel(), stat)
        self.assertTrue((self.root / om.runs_rel() / "T-c1/report.md").is_file())  # 前序汇报拷回留档

    def test_reaccept_after_dispatcher_adjusts_accepted_worktree(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-ra")
        self.agent([("T-ra", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-ra"], 60, 2), 0)
        s = om.read_state(self.root, "T-ra")
        (Path(s["worktree"]) / "src/demo/A2.txt").write_text("x\n")
        with self.assertRaises(om.OrcaMultiError):
            om.finish(self.root, self.cfg, s, "feat: x", yes=True)
        with self.assertRaises(om.OrcaMultiError):  # 没有针对新改动的 guard/verify 记录
            om.accept(self.root, self.cfg, s)
        om.guard(self.root, self.cfg, s)
        self.assertTrue(om.verify(self.root, self.cfg, om.read_state(self.root, "T-ra"))["ok"])
        om.accept(self.root, self.cfg, om.read_state(self.root, "T-ra"))
        om.finish(self.root, self.cfg, om.read_state(self.root, "T-ra"), "feat: x", yes=True)
        self.assertIn("A2.txt", self.sh("git", "show", "--stat", "--format=", "HEAD^2"))

    def test_post_merge_failure_keeps_full_output(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-pm", verify=("test -f src/demo/A.txt",))
        self.agent([("T-pm", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-pm"], 60, 2), 0)
        s = om.read_state(self.root, "T-pm")
        task = self.root / om.runs_rel() / "T-pm/task.md"
        task.write_text(task.read_text().replace("test -f src/demo/A.txt", "echo 详细失败原因 && false"))
        om.finish(self.root, self.cfg, s, "feat: pm", yes=True)
        out = (self.root / om.runs_rel() / "T-pm/evidence/post-merge-verify-1.out").read_text()
        self.assertIn("详细失败原因", out)

    def test_worktree_guard_accepts_main_fast_forward_but_flags_rewrite(self):
        """调度端合并其他任务使主工作区 HEAD 前进是正常的；历史被改写才算异常。"""
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.cfg["max_worktrees"] = 2
        self.write_contract("T-ff")
        def act(wd):
            (wd / "src/demo/A.txt").write_text("a\n")
            self.sh("git", "commit", "-q", "--allow-empty", "-m", "dispatcher merged another task")
        self.agent([("T-ff", act)])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-ff"], 60, 2), 0)
        self.write_contract("T-rw", allowed=("lib/**",), verify=("test -f lib/B.txt",), extra="workspace: worktree\n")
        def rewrite(wd):
            (wd / "lib").mkdir(exist_ok=True)
            (wd / "lib/B.txt").write_text("b\n")
            self.sh("git", "reset", "-q", "--hard", "HEAD~1")
        self.agent([("T-rw", rewrite)])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-rw"], 60, 2), 1)
        g = json.loads((self.root / om.runs_rel() / "T-rw/guard.json").read_text())
        self.assertTrue(any("历史被改写" in o for o in g["outside"]), g)

    def test_question_still_processes_other_deliveries_and_is_reported_once(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.cfg["max_worktrees"] = 2
        self.write_contract("T-q1")
        self.write_contract("T-q2", allowed=("lib/**",), verify=("test -f lib/B.txt",))
        s1 = om.spawn(self.root, self.cfg, "T-q1")
        s2 = om.spawn(self.root, self.cfg, "T-q2")
        def done(s, rel, content):
            wd = Path(s["worktree"])
            (wd / rel).parent.mkdir(parents=True, exist_ok=True)
            (wd / rel).write_text(content)
            (wd / om.runs_rel() / s["id"] / "report.md").write_text("done\n")
            return {"type": "worker_done", "payload": {"dispatchId": s["dispatch_id"], "outcome": "succeeded"}}
        q = {"type": "question", "id": "msg_q2", "subject": "Q", "body": "需要确认"}
        calls = []
        def check(_ms):
            calls.append(1)
            if len(calls) == 1:  # 同一批：T-q1 交付 + T-q2 提问
                return {"deliveryId": "d1", "messages": [done(s1, "src/demo/A.txt", "a\n"), q]}
            if len(calls) == 2:  # 未回答的提问被重新投递，不应再次导致退出
                return {"deliveryId": "d2", "messages": [q, {"type": "heartbeat", "id": "hb"}]}
            return {"deliveryId": "d3", "messages": [done(s2, "lib/B.txt", "b\n")]}
        om.check_messages = check
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-q1", "T-q2"], 60, 2), 3)
        self.assertEqual(om.read_state(self.root, "T-q1")["status"], "accepted")  # 提问不耽误另一任务验收
        log = self.log.read_text()
        self.assertIn('"--ack", "d1"', log)
        self.assertIn("msg_q2", om.asked_questions(self.root))
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-q1", "T-q2"], 60, 2), 0)
        self.assertIn('"--ack", "d2"', self.log.read_text())

    def test_run_respawns_abandoned_task(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-ab")
        s = om.spawn(self.root, self.cfg, "T-ab")
        om.abandon(self.root, self.cfg, s, yes=True)
        self.assertTrue(om.read_state(self.root, "T-ab")["abandoned"])
        # 默认不会自动重启已放弃的任务（避免对已完成并回收的任务误执行 run）
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-ab"], 60, 2), 1)
        self.assertEqual(om.read_state(self.root, "T-ab")["status"], "done")
        with self.assertRaises(om.OrcaMultiError):
            om.spawn(self.root, self.cfg, "T-ab")
        # 显式 --redo 才重新启动
        self.agent([("T-ab", lambda wd: (wd / "src/demo/A.txt").write_text("a\n"))])
        self.assertEqual(om.auto_run(self.root, self.cfg, ["T-ab"], 60, 2, redo=True), 0)
        s2 = om.read_state(self.root, "T-ab")
        self.assertEqual(s2["status"], "accepted")
        self.assertFalse(s2.get("abandoned"))

    def test_amend_updates_both_contract_copies_and_notifies_agent(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-am")
        s = om.spawn(self.root, self.cfg, "T-am")
        om.amend(self.root, s, ["lib/**", "src/demo/**"], ["test -d src"], "补充范围")
        main_copy = om.load_task(self.root, "T-am")
        self.assertEqual(main_copy["allowed_files"], ["src/demo/**", "lib/**"])  # 已存在的不重复
        self.assertEqual(main_copy["verify"], ["test -f src/demo/A.txt", "test -d src"])
        wt_copy = om.parse_task((Path(s["worktree"]) / om.runs_rel() / "T-am/task.md").read_text())
        self.assertEqual(wt_copy["allowed_files"], main_copy["allowed_files"])
        self.assertEqual(om.read_state(self.root, "T-am")["contract_version"], 2)
        log = self.log.read_text()
        self.assertIn("合同已更新", log)
        self.assertIn("补充范围", log)

    def test_add_list_items_expands_empty_list(self):
        text = contract("T-e", allowed=())
        out = om.add_list_items(text, "allowed_files", ["docs/x.md"])
        self.assertEqual(om.parse_task(out)["allowed_files"], ["docs/x.md"])

    def test_merge_lock_is_exclusive(self):
        import fcntl
        with om.merge_lock(self.root):
            with (self.root / om.runs_rel() / ".merge.lock").open("w") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (self.root / om.runs_rel() / ".merge.lock").open("w") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 释放后可再获取

    def test_zcode_worker_start_omits_model_and_effort(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-z", extra="agent: zcode\nmodel: GLM-5.3-Flash\neffort: high\n")
        s = om.spawn(self.root, self.cfg, "T-z")
        start = [json.loads(l) for l in self.log.read_text().splitlines() if "worker-start" in l][-1]
        self.assertIn("zcode", start)
        self.assertNotIn("--model", start)
        self.assertNotIn("--effort", start)
        self.assertEqual(s["agent"], "zcode")
        self.assertEqual(s["model"], "zcode 自身配置")

    def test_reject_fresh_agent_uses_agent_specific_args(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")
        self.write_contract("T-zr", extra="agent: zcode\n")
        s = om.spawn(self.root, self.cfg, "T-zr")
        s["status"] = "failed"
        s["terminal"] = None  # 原终端已不在：reject 另起新 agent
        om.write_state(self.root, s)
        om.reject(self.root, om.read_state(self.root, "T-zr"), "继续")
        start = [json.loads(l) for l in self.log.read_text().splitlines() if "worker-start" in l][-1]
        self.assertIn("zcode", start)
        self.assertNotIn("--model", start)
        self.assertNotIn("--effort", start)


class NewFeatures(FlowBase):
    """基线、合同检查、任务目录同步、重启保护。"""

    def spec_of(self, tid):
        for line in self.log.read_text().splitlines():
            a = json.loads(line)
            if "worker-start" in a and f"{tid}｜" in " ".join(a):
                return a[a.index("--spec") + 1]
        return ""

    def test_baseline_records_failures_and_goes_into_spec(self):
        self.write_contract("T-bl", verify=("test -f src/demo/A.txt", "test -f README.md"))
        data = om.run_baseline(self.root, self.cfg, "T-bl", 60)
        self.assertEqual([r["exit"] != 0 for r in data["results"]], [True, False])
        self.assertTrue((self.root / om.runs_rel() / "T-bl" / "baseline.json").is_file())
        om.spawn(self.root, self.cfg, "T-bl")
        spec = self.spec_of("T-bl")
        self.assertIn("基线", spec)
        self.assertIn("test -f src/demo/A.txt", spec)
        self.assertNotIn("test -f README.md（退出码", spec)      # 通过的命令不列出

    def test_baseline_all_pass_is_noted_and_not_copied_to_worktree(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")      # 迫使用 worktree
        self.write_contract("T-bp", verify=("test -f README.md",))
        om.run_baseline(self.root, self.cfg, "T-bp", 60)
        s = om.spawn(self.root, self.cfg, "T-bp")
        self.assertIn("全部通过", self.spec_of("T-bp"))
        self.assertFalse((Path(s["worktree"]) / om.runs_rel() / "T-bp" / "baseline.json").exists())

    def test_baseline_refuses_when_main_workspace_busy(self):
        self.write_contract("T-busy", verify=("true",))
        om.spawn(self.root, self.cfg, "T-busy")                    # 主工作区任务进行中
        self.write_contract("T-bl2", verify=("true",))
        with self.assertRaises(om.OrcaMultiError):
            om.run_baseline(self.root, self.cfg, "T-bl2", 60)

    def test_baseline_timeout_is_recorded_as_failure(self):
        self.write_contract("T-to", verify=("sleep 5",))
        data = om.run_baseline(self.root, self.cfg, "T-to", 1)
        self.assertEqual(data["results"][0]["exit"], 124)

    def test_lint_flags_unlisted_tests_and_verify_paths(self):
        (self.root / "src/demo/Widget.vue").write_text("w\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/widget_user.test.js").write_text("import Widget from '../src/demo/Widget.vue'\n")
        (self.root / "tests/unrelated.test.js").write_text("nothing\n")
        self.sh("git", "add", "-A")
        self.sh("git", "commit", "-qm", "add")
        self.write_contract("T-li", allowed=("src/demo/**",), verify=("node --test tests/new_feature.test.js",))
        warnings = om.lint_contract(self.root, "T-li")
        joined = "\n".join(warnings)
        self.assertIn("tests/widget_user.test.js", joined)
        self.assertNotIn("unrelated", joined)
        self.assertIn("tests/new_feature.test.js", joined)         # verify 引用的新文件不在 allowed_files 里
        # 把它们加进 allowed_files 后不再提示
        self.write_contract("T-li2", allowed=("src/demo/**", "tests/**"), verify=("node --test tests/new_feature.test.js",))
        self.assertEqual(om.lint_contract(self.root, "T-li2"), [])

    def test_lint_resolves_cd_prefix_ignores_runs_dir_and_caps_results(self):
        (self.root / "src/demo/Gadget.vue").write_text("g\n")
        (self.root / "frontend/tests").mkdir(parents=True)
        (self.root / "frontend/tests/exists.test.js").write_text("x\n")
        for i in range(15):
            (self.root / f"frontend/tests/uses{i}.test.js").write_text("import Gadget from 'Gadget.vue'\n")
        runs = self.root / om.runs_rel() / "task_old"
        runs.mkdir(parents=True)
        (runs / "test_leftover.py").write_text("Gadget Gadget\n")
        self.sh("git", "add", "-A", "-f")
        self.sh("git", "commit", "-qm", "many")
        self.write_contract("T-cap", allowed=("src/demo/**", "frontend/tests/exists.test.js"),
                            verify=("cd frontend && node --test tests/exists.test.js tests/missing.test.js",))
        warnings = om.lint_contract(self.root, "T-cap")
        joined = "\n".join(warnings)
        self.assertNotIn("test_leftover", joined)                       # 运行目录里的历史文件不提示
        self.assertEqual(sum(w.startswith("测试文件 ") for w in warnings), om.LINT_MAX_TESTS)
        self.assertIn("另有 3 个测试文件", joined)                        # 超出上限的只报数量
        self.assertIn("frontend/tests/missing.test.js", joined)         # cd 目录被解析
        self.assertNotIn("frontend/tests/exists.test.js（", joined)     # 已在 allowed_files 里

    def test_lint_ignores_generic_and_short_stems(self):
        (self.root / "src/demo/index.js").write_text("i\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/a.test.js").write_text("index index\n")
        self.sh("git", "add", "-A")
        self.sh("git", "commit", "-qm", "add")
        self.write_contract("T-lg", allowed=("src/demo/**",), verify=())
        self.assertEqual(om.lint_contract(self.root, "T-lg"), [])

    def test_task_dir_attachments_sync_to_worktree_on_run_and_reply(self):
        (self.root / "src/demo/old.txt").write_text("dirty\n")      # 迫使用 worktree
        self.write_contract("T-sy")
        s = om.spawn(self.root, self.cfg, "T-sy")
        ref = self.root / om.runs_rel() / "T-sy" / "ref.md"
        ref.write_text("参考材料\n")                                   # spawn 之后才放进主工作区任务目录
        wt_ref = Path(s["worktree"]) / om.runs_rel() / "T-sy" / "ref.md"
        self.assertFalse(wt_ref.exists())
        self.assertEqual(om.sync_task_files(self.root, om.read_state(self.root, "T-sy")), 1)
        self.assertEqual(wt_ref.read_text(), "参考材料\n")
        self.assertEqual(om.sync_task_files(self.root, om.read_state(self.root, "T-sy")), 0)   # 幂等
        ref.write_text("更新后\n")
        om.cmd_reply(type("A", (), {"message_id": "m1", "text": "ok"})(), self.root, self.cfg)
        self.assertEqual(wt_ref.read_text(), "更新后\n")
        # 调度端自己的文件不同步
        (self.root / om.runs_rel() / "T-sy" / "verify.json").write_text("{}")
        om.sync_task_files(self.root, om.read_state(self.root, "T-sy"))
        self.assertFalse((Path(s["worktree"]) / om.runs_rel() / "T-sy" / "verify.json").exists())

    def test_sync_noop_for_main_workspace_task(self):
        self.write_contract("T-sm")
        s = om.spawn(self.root, self.cfg, "T-sm")
        self.assertEqual(om.sync_task_files(self.root, s), 0)

    def test_spawn_done_task_needs_redo(self):
        self.write_contract("T-rd")
        s = om.spawn(self.root, self.cfg, "T-rd")
        om.abandon(self.root, self.cfg, s, yes=True)
        with self.assertRaises(om.OrcaMultiError) as ctx:
            om.spawn(self.root, self.cfg, "T-rd")
        self.assertIn("--redo", str(ctx.exception))
        self.assertIsNotNone(om.spawn(self.root, self.cfg, "T-rd", redo=True))


if __name__ == "__main__":
    unittest.main()

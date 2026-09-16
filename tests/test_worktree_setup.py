"""Offline tests for the Paseo worktree setup command, using temporary git repositories."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime

GIT = shutil.which("git")


@unittest.skipUnless(GIT, "git is required")
class WorktreeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-codex-worktree-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve() / "space ' quote"
        self.base.mkdir()
        # Deterministic git: no user config, no prompts, fixed identity.
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "HOME": str(self.base),
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        self.origin = self.base / "origin.git"
        self.git(["init", "--bare", "-b", "main", str(self.origin)], self.base)
        seed = self.base / "seed"
        self.git(["clone", "-q", str(self.origin), str(seed)], self.base)
        self.commit(seed, "one")
        self.git(["push", "-q", "origin", "main"], seed)
        # The clone Paseo owns: its local main stays where it was cloned.
        self.repo = self.base / "repo"
        self.git(["clone", "-q", str(self.origin), str(self.repo)], self.base)
        self.stale_main = self.rev(self.repo, "main")
        self.publisher = seed

    def git(self, args, cwd, check=True):
        result = subprocess.run([GIT, *args], cwd=str(cwd), env=self.env, capture_output=True, text=True)
        if check and result.returncode:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    def commit(self, cwd, name):
        (Path(cwd) / name).write_text(name + "\n")
        self.git(["add", name], cwd)
        self.git(["commit", "-q", "-m", name], cwd)
        return self.rev(cwd, "HEAD")

    def rev(self, cwd, ref):
        return self.git(["rev-parse", ref], cwd)

    def advance_origin(self, name, branch="main"):
        """Publish a commit on origin without touching the Paseo clone."""
        self.git(["checkout", "-q", branch], self.publisher)
        sha = self.commit(self.publisher, name)
        self.git(["push", "-q", "origin", branch], self.publisher)
        return sha

    def worktree(self, branch, base="main", metadata=None, existing=False):
        """Create a worktree the way Paseo does and record its metadata."""
        path = self.base / "worktrees" / branch
        path.parent.mkdir(exist_ok=True)
        if existing:
            self.git(["worktree", "add", "-q", str(path), branch], self.repo)
        else:
            self.git(["worktree", "add", "-q", "-b", branch, "--no-track", str(path), base], self.repo)
        if metadata is not None:
            git_dir = Path(self.git(["rev-parse", "--absolute-git-dir"], path))
            (git_dir / "paseo").mkdir()
            (git_dir / "paseo" / "worktree.json").write_text(json.dumps(metadata))
        return path

    @staticmethod
    def branch_off(base="main", branch="feature", base_ref=None):
        return {"version": 1, "baseRefName": base, "baseRef": base_ref or f"refs/heads/{base}",
                "changeRequestLookupTarget": {"headRef": branch, "localBranchName": branch}}

    def prepare(self, path):
        with patch.object(runtime, "say") as say:
            self.assertEqual(runtime.prepare_worktree(path, self.env), 0)
        return " ".join(call.args[0] for call in say.call_args_list)

    def test_branch_off_worktree_moves_to_the_latest_origin_default_branch(self):
        latest = self.advance_origin("two")
        path = self.worktree("feature", metadata=self.branch_off())
        self.assertEqual(self.rev(path, "HEAD"), self.stale_main)
        message = self.prepare(path)
        self.assertIn("moved feature to the latest origin/main", message)
        self.assertEqual(self.rev(path, "HEAD"), latest)
        self.assertEqual(self.git(["branch", "--show-current"], path), "feature")
        self.assertEqual(self.git(["status", "--porcelain"], path), "")
        # The clone's own main and the other worktrees are not touched.
        self.assertEqual(self.rev(self.repo, "main"), self.stale_main)
        self.assertIn("already matches origin/main", self.prepare(path))
        self.assertEqual(self.rev(path, "HEAD"), latest)

    def test_branch_off_worktree_with_its_own_commits_or_edits_is_left_alone(self):
        self.advance_origin("two")
        path = self.worktree("feature", metadata=self.branch_off())
        (path / "one").write_text("edited\n")
        self.assertIn("uncommitted changes", self.prepare(path))
        self.assertEqual(self.rev(path, "HEAD"), self.stale_main)
        self.assertEqual((path / "one").read_text(), "edited\n")
        self.git(["checkout", "--", "one"], path)
        (path / "untracked").write_text("kept\n")
        own = self.commit(path, "mine")
        self.assertIn("has its own commits", self.prepare(path))
        self.assertEqual(self.rev(path, "HEAD"), own)
        self.assertTrue((path / "untracked").exists())

    def test_branch_off_from_another_base_follows_that_base_on_origin(self):
        self.git(["checkout", "-q", "-b", "release"], self.publisher)
        self.commit(self.publisher, "release-one")
        self.git(["push", "-q", "origin", "release"], self.publisher)
        self.git(["fetch", "-q", "origin"], self.repo)
        self.git(["branch", "release", "origin/release"], self.repo)
        latest = self.advance_origin("release-two", branch="release")
        path = self.worktree("hotfix", base="release",
                             metadata=self.branch_off("release", "hotfix", "refs/remotes/origin/release"))
        self.assertIn("moved hotfix to the latest origin/release", self.prepare(path))
        self.assertEqual(self.rev(path, "HEAD"), latest)
        # A base that exists only locally cannot be refreshed from origin.
        self.git(["branch", "local-only", "main"], self.repo)
        local = self.worktree("from-local", base="local-only", metadata=self.branch_off("local-only", "from-local"))
        self.assertIn("has no copy on origin", self.prepare(local))
        self.assertEqual(self.rev(local, "HEAD"), self.stale_main)

    def test_checked_out_branch_is_fast_forwarded_only(self):
        self.git(["checkout", "-q", "-b", "topic"], self.publisher)
        self.commit(self.publisher, "topic-one")
        self.git(["push", "-q", "origin", "topic"], self.publisher)
        self.git(["fetch", "-q", "origin"], self.repo)
        self.git(["branch", "topic", "origin/topic"], self.repo)
        latest = self.advance_origin("topic-two", branch="topic")
        checkout = {"version": 1, "baseRefName": "topic",
                    "changeRequestLookupTarget": {"headRef": "topic", "localBranchName": "topic"}}
        path = self.worktree("topic", metadata=checkout, existing=True)
        self.assertIn("fast-forwarded topic to origin/topic", self.prepare(path))
        self.assertEqual(self.rev(path, "HEAD"), latest)
        self.advance_origin("topic-three", branch="topic")
        own = self.commit(path, "local-topic")
        self.assertIn("have diverged", self.prepare(path))
        self.assertEqual(self.rev(path, "HEAD"), own)

    def test_pull_request_checkout_and_unknown_worktrees_keep_their_commits(self):
        self.advance_origin("two")
        pr = {"version": 1, "baseRefName": "main", "baseRef": "refs/heads/main",
              "changeRequestLookupTarget": {"headRef": "pr-head", "localBranchName": "pr-7", "changeRequestNumber": 7}}
        path = self.worktree("pr-7", metadata=pr)
        self.assertIn("pr-7 has no copy on origin", self.prepare(path))
        self.assertEqual(self.rev(path, "HEAD"), self.stale_main)
        # Without Paseo metadata the branch is treated like any checked-out branch.
        plain = self.worktree("plain")
        self.assertIn("plain has no copy on origin", self.prepare(plain))
        self.assertEqual(self.rev(plain, "HEAD"), self.stale_main)

    def test_origin_head_is_recovered_when_the_clone_lost_it(self):
        latest = self.advance_origin("two")
        self.git(["remote", "set-head", "origin", "-d"], self.repo)
        path = self.worktree("feature", metadata=self.branch_off())
        self.prepare(path)
        self.assertEqual(self.rev(path, "HEAD"), latest)
        self.assertEqual(self.git(["symbolic-ref", "refs/remotes/origin/HEAD"], path), "refs/remotes/origin/main")

    def test_failures_are_reported_without_changing_the_worktree(self):
        path = self.worktree("feature", metadata=self.branch_off())
        with self.assertRaises(runtime.SetupError):
            runtime.prepare_worktree(self.base / "missing", self.env)
        with self.assertRaises(runtime.SetupError):
            runtime.prepare_worktree(path / "..", self.env)
        self.git(["remote", "set-url", "origin", str(self.base / "gone.git")], self.repo)
        with self.assertRaisesRegex(runtime.SetupError, "fetch origin"):
            runtime.prepare_worktree(path, self.env)
        self.git(["remote", "set-url", "origin", str(self.origin)], self.repo)
        self.git(["checkout", "-q", "--detach"], path)
        with self.assertRaisesRegex(runtime.SetupError, "detached HEAD"):
            runtime.prepare_worktree(path, self.env)
        self.assertEqual(self.rev(path, "HEAD"), self.stale_main)

    def test_launcher_mode_prepares_the_paseo_worktree_path(self):
        latest = self.advance_origin("two")
        path = self.worktree("feature", metadata=self.branch_off())
        settings = self.base / "settings.json"
        runtime.write_json(settings, {})
        argv = ["claude_codex.py", "worktree-setup", str(settings)]
        with patch.dict(os.environ, {**self.env, "PASEO_WORKTREE_PATH": str(path)}), \
             patch.object(sys, "argv", argv), patch.object(runtime, "say"):
            with self.assertRaises(SystemExit) as stop:
                runtime.main()
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual(self.rev(path, "HEAD"), latest)
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "argv", [*argv, "run"]), \
             patch.object(os, "getcwd", return_value=str(path)), patch.object(runtime, "say") as say:
            with self.assertRaises(SystemExit):
                runtime.main()
        self.assertIn("already matches", say.call_args[0][0])

    def test_init_registers_the_command_first_in_paseo_json(self):
        config = self.repo / "paseo.json"
        with patch.object(runtime, "say"), patch.object(runtime.shutil, "which", return_value=None):
            self.assertEqual(runtime.register_worktree_setup(self.repo), 0)
        self.assertEqual(runtime.read_json(config), {"worktree": {"setup": runtime.WORKTREE_SETUP_COMMAND}})
        self.assertEqual(config.stat().st_mode & 0o777, 0o644)
        # A second run changes nothing; other settings and commands are kept, ours goes first.
        before = config.read_bytes()
        with patch.object(runtime, "say") as say:
            runtime.register_worktree_setup(str(self.repo))
        self.assertIn("already runs", say.call_args[0][0])
        self.assertEqual(config.read_bytes(), before)
        config.chmod(0o600)
        runtime.write_json(config, {"scripts": {"dev": {"command": "npm run dev"}},
                                    "worktree": {"setup": "npm ci", "teardown": ["rm -rf .cache"]}})
        with patch.object(runtime, "say"):
            runtime.register_worktree_setup(self.repo)
        self.assertEqual(runtime.read_json(config), {
            "scripts": {"dev": {"command": "npm run dev"}},
            "worktree": {"setup": [runtime.WORKTREE_SETUP_COMMAND, "npm ci"], "teardown": ["rm -rf .cache"]},
        })
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        runtime.write_json(config, {"worktree": {"setup": ["npm ci", "cp ../.env ."]}})
        with patch.object(runtime, "say"):
            runtime.register_worktree_setup(self.repo)
        self.assertEqual(runtime.read_json(config)["worktree"]["setup"],
                         [runtime.WORKTREE_SETUP_COMMAND, "npm ci", "cp ../.env ."])
        for broken in ({"worktree": []}, {"worktree": {"setup": {"command": "x"}}}, {"worktree": {"setup": [1]}}):
            runtime.write_json(config, broken)
            with self.assertRaises(runtime.SetupError):
                runtime.register_worktree_setup(self.repo)
        with self.assertRaises(runtime.SetupError):
            runtime.register_worktree_setup(self.base)
        with patch.object(runtime, "say"), patch.object(sys, "argv", ["claude_codex.py", "worktree-setup",
                                                                     str(self.base / "s.json"), "init", str(self.repo)]):
            runtime.write_json(self.base / "s.json", {})
            runtime.write_json(config, {})
            with self.assertRaises(SystemExit) as stop:
                runtime.main()
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual(runtime.read_json(config), {"worktree": {"setup": runtime.WORKTREE_SETUP_COMMAND}})


if __name__ == "__main__":
    unittest.main()

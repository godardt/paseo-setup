"""Offline regression tests. Run: python3 -m unittest discover -s tests -v"""

import hashlib
import io
import json
import os
import shlex
import shutil
import socket
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime
import install


class LauncherTests(unittest.TestCase):
    def test_sdk_arguments_preserved_and_model_effort_respected(self):
        args = ["--output-format", "stream-json", "--input-format", "stream-json",
                "--resume", "session-id", "--model", "gpt-6-astra(max)",
                "--settings", '{"hooks":{}}', "--permission-prompt-tool", "stdio"]
        forwarded, effort = runtime.parse_launch_args(args, "high")
        self.assertEqual(effort, "max")
        self.assertEqual(forwarded[:2], ["--model", "gpt-6-astra(max)"])
        self.assertEqual(forwarded[2:], args[:6] + args[8:])

    def test_all_efforts_and_literal_prompt_separator(self):
        for effort in runtime.EFFORTS:
            args, actual = runtime.parse_launch_args(
                [f"--reasoning={effort}", "-p", "--", "--reasoning", "is prompt text"], "high"
            )
            self.assertEqual(actual, effort)
            self.assertEqual(args, ["--model", f"gpt-6-astra({effort})", "-p", "--", "--reasoning", "is prompt text"])

    def test_ultracode_selection_enables_native_mode_and_proxy_alias(self):
        for selection in (["--reasoning", "ultracode"], ["--model", runtime.ULTRACODE_MODEL],
                          ["--model", runtime.ULTRACODE_MODEL, "--effort", "xhigh"]):
            args, mode = runtime.parse_launch_args([*selection, "--output-format", "stream-json"], "high")
            self.assertEqual(mode, "ultracode")
            self.assertEqual(args, ["--model", runtime.ULTRACODE_MODEL, "--output-format", "stream-json",
                                    "--effort", "ultracode"])
        with self.assertRaises(runtime.SetupError):
            runtime.parse_launch_args(["--reasoning", "ultracode", "--effort", "max"], "high")

    def test_invalid_and_conflicting_options_fail(self):
        for args in (["--reasoning", "ultra"], ["--reasoning"], ["--model", "gpt-5"],
                     ["--model", "gpt-6-astra(none)"],
                     ["--model", "gpt-6-astra(max)", "--reasoning", "low"]):
            with self.subTest(args=args), self.assertRaises(runtime.SetupError):
                runtime.parse_launch_args(args, "high")

    def test_environment_isolated_and_all_helpers_mapped(self):
        source = {"ANTHROPIC_API_KEY": "old", "CLAUDE_CODE_OAUTH_TOKEN": "old",
                  "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: old",
                  "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
                  "CLAUDE_CODE_EFFORT_LEVEL": "low", "PATH": "/bin", "NO_PROXY": "example.test"}
        env = runtime.claude_env({"port": 8317, "api_key": "local-test-key", "config_dir": "/tmp/test config"}, "max", source)
        self.assertEqual(source["ANTHROPIC_API_KEY"], "old")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS", env)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8317")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1050000")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "gpt-6-astra(max)")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "gpt-6-astra(max)")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/tmp/test config/claude")
        self.assertEqual(env["NO_PROXY"], "example.test,127.0.0.1,localhost")

    def test_launcher_settings_disable_auto_mode_without_replacing_native_settings(self):
        launcher = {"permissions": {"disableAutoMode": "disable"}}
        appended = runtime.apply_launcher_settings(["--model", "gpt-6-astra(high)", "-p", "--", "--settings", "literal"])
        self.assertEqual(appended, ["--model", "gpt-6-astra(high)", "-p", "--settings", json.dumps(launcher, separators=(",", ":")),
                                    "--", "--settings", "literal"])
        # Paseo passes its own --settings for Ultra Code and fast mode; Claude
        # reads one operand, so the launcher merges rather than replaces it.
        merged = runtime.apply_launcher_settings(
            ["--model", "x", "--settings", '{"ultracode":true,"permissions":{"allow":["Bash"]}}', "--effort", "ultracode"])
        self.assertEqual(merged[:3] + merged[4:], ["--model", "x", "--settings", "--effort", "ultracode"])
        self.assertEqual(json.loads(merged[3]),
                         {"ultracode": True, "permissions": {"allow": ["Bash"], "disableAutoMode": "disable"}})
        inline = runtime.apply_launcher_settings(['--settings={"fastMode":false}'])
        self.assertEqual(inline[0][:11], "--settings=")
        self.assertEqual(json.loads(inline[0][11:]), {"fastMode": False, **launcher})
        # An operand that merely looks like the option or the separator is left alone.
        self.assertEqual(runtime.apply_launcher_settings(["--append-system-prompt", "--settings"]),
                         ["--append-system-prompt", "--settings", "--settings", json.dumps(launcher, separators=(",", ":"))])
        self.assertEqual(runtime.apply_launcher_settings(["--append-system-prompt", "--", "-p", "Hi", "--", "--settings", "x"]),
                         ["--append-system-prompt", "--", "-p", "Hi", "--settings", json.dumps(launcher, separators=(",", ":")),
                          "--", "--settings", "x"])
        with tempfile.TemporaryDirectory() as temp:
            settings_file = Path(temp) / "claude-settings.json"
            settings_file.write_text('{"env": {"FOO": "1"}, "permissions": {"deny": ["WebSearch"]}}')
            from_file = runtime.apply_launcher_settings(["--settings", str(settings_file), "-p"])
            self.assertEqual(json.loads(from_file[1]),
                             {"env": {"FOO": "1"}, "permissions": {"deny": ["WebSearch"], "disableAutoMode": "disable"}})
            settings_file.write_text("not json")
            with self.assertRaises(runtime.SetupError):
                runtime.apply_launcher_settings(["--settings", str(settings_file)])
        for operand in ('["not", "an", "object"]', str(Path(tempfile.gettempdir()) / "missing-claude-settings.json")):
            with self.subTest(operand=operand), self.assertRaises(runtime.SetupError):
                runtime.apply_launcher_settings(["--settings", operand])
        self.assertTrue(runtime.auto_mode_allowed({"CLAUDE_CODEX_AUTO_MODE": "1"}))
        for value in ({}, {"CLAUDE_CODEX_AUTO_MODE": "true"}, {"CLAUDE_CODEX_AUTO_MODE": "0"}):
            self.assertFalse(runtime.auto_mode_allowed(value))

    def test_paseo_sessions_use_the_daemon_profile(self):
        # Paseo reloads transcripts from the daemon's CLAUDE_CONFIG_DIR or ~/.claude,
        # not from the provider environment, so Paseo launches keep that profile.
        settings = {"port": 8317, "api_key": "local-test-key", "config_dir": "/tmp/test config"}
        paseo = {"CLAUDE_CODEX_PASEO_USAGE": "1", "PATH": "/bin"}
        self.assertNotIn("CLAUDE_CONFIG_DIR", runtime.claude_env(settings, "high", paseo))
        daemon = runtime.claude_env(settings, "high", {**paseo, "CLAUDE_CONFIG_DIR": "/daemon/claude"})
        self.assertEqual(daemon["CLAUDE_CONFIG_DIR"], "/daemon/claude")
        # Provider entries written by older installers pinned the isolated profile.
        stale = runtime.claude_env(settings, "high", {**paseo, "CLAUDE_CONFIG_DIR": "/tmp/test config/claude"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", stale)
        self.assertEqual(stale["ANTHROPIC_MODEL"], "gpt-6-astra(high)")
        terminal = runtime.claude_env(settings, "high", {"CLAUDE_CONFIG_DIR": "/shell/claude", "PATH": "/bin"})
        self.assertEqual(terminal["CLAUDE_CONFIG_DIR"], "/tmp/test config/claude")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-codex-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve() / "space ' quote $dollar"
        self.base.mkdir()
        self.settings = {"config_dir": str(self.base / "config"), "data_dir": str(self.base / "data"),
                         "state_dir": str(self.base / "state"), "bin_dir": str(self.base / "bin"),
                         "port": 8317, "api_key": "test-local-key", "reasoning": "high"}

    def fake_cli(self, name, body):
        target = self.base / name
        target.write_text(f"#!{sys.executable}\n" + body)
        target.chmod(0o755)
        return target

    UPSTREAM_MODULE = "// Paseo's own Claude provider module; never modified by this installer.\nexport {};\n"

    def fake_paseo(self, name, body):
        package = self.base / (name + "-package")
        entry = package / "bin" / "paseo"
        entry.parent.mkdir(parents=True)
        entry.write_text(f"#!{sys.executable}\n" + body)
        entry.chmod(0o755)
        runtime.write_json(package / "package.json", {"name": "@getpaseo/cli", "type": "module"})
        server = package / "node_modules" / "@getpaseo" / "server"
        runtime.write_json(server / "package.json", {"name": "@getpaseo/server", "type": "module"})
        module = server / install.LEGACY_AGENT_PATH
        module.parent.mkdir(parents=True)
        module.write_text(self.UPSTREAM_MODULE)
        target = self.base / name
        target.symlink_to(entry)
        return target

    def staged_args(self, *extra):
        claude = self.fake_cli("claude-original", "print('Claude Code')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        return ["--config-dir", self.settings["config_dir"], "--data-dir", self.settings["data_dir"],
                "--state-dir", self.settings["state_dir"], "--bin-dir", self.settings["bin_dir"],
                "--paseo-home", str(self.base / "paseo-home"), "--claude-bin", str(claude),
                "--peppy-config-dir", str(self.base / "claude-peppy"),
                "--proxy-binary", str(proxy), "--skip-login", "--skip-paseo-start", "--no-path", *extra]

    def test_paseo_wrapper_uses_runtime_path_and_quoted_node_prefix(self):
        node = self.fake_cli("node", "print('v22.0.0')\n")
        paseo = self.fake_paseo("paseo", "import json,os,sys\nprint(json.dumps({'path':os.environ['PATH'], 'home':os.environ['PASEO_HOME'], 'args':sys.argv[1:]}))\n")
        args = self.staged_args("--paseo-bin", str(paseo))
        installation_path = str(node.parent) + os.pathsep + "/installation-only"
        with patch.dict(os.environ, {"PATH": installation_path}):
            install.install(install.parser().parse_args(args))
        wrapper = Path(self.settings["bin_dir"]) / "paseo-codex"
        self.assertNotIn("/installation-only", wrapper.read_text())
        forwarded = ["--version", "space ' quote $literal", ""]
        for new_path in ("/new node ' $prefix/bin:/usr/bin", ""):
            with self.subTest(path=new_path):
                result = subprocess.run([str(wrapper), *forwarded], check=True, capture_output=True,
                                        text=True, env={**os.environ, "PATH": new_path})
                self.assertEqual(json.loads(result.stdout), {
                    "path": str(node.parent) + os.pathsep + new_path,
                    "home": str(self.base / "paseo-home"), "args": forwarded,
                })

    def test_launcher_without_prefix_preserves_empty_runtime_path(self):
        cli = self.fake_cli("print-path", "import os\nprint(repr(os.environ['PATH']))\n")
        wrapper = self.base / "wrapper"
        install.write_launcher(wrapper, [cli])
        result = subprocess.run([str(wrapper)], check=True, capture_output=True, text=True,
                                env={**os.environ, "PATH": ""})
        self.assertEqual(result.stdout.strip(), "''")

    def test_bash_login_precedence_preserves_profiles_and_is_idempotent(self):
        for names, chosen in (((".bash_profile", ".bash_login", ".profile"), ".bash_profile"),
                              ((".bash_login", ".profile"), ".bash_login"),
                              ((".profile",), ".profile"), ((), ".profile")):
            with self.subTest(profiles=names):
                home = self.base / (chosen + str(len(names)))
                home.mkdir()
                originals = {name: f"# existing {name}\n" for name in (".bashrc", *names)}
                for name, content in originals.items():
                    (home / name).write_text(content)
                with patch.dict(os.environ, {"HOME": str(home), "SHELL": "/bin/bash"}):
                    install.add_path(Path(self.settings["bin_dir"]))
                    before = {path.name: path.read_bytes() for path in home.iterdir()}
                    install.add_path(Path(self.settings["bin_dir"]))
                self.assertEqual({path.name: path.read_bytes() for path in home.iterdir()}, before)
                for name in (".bashrc", ".bash_profile", ".bash_login", ".profile"):
                    path = home / name
                    if name in (".bashrc", chosen):
                        self.assertTrue(path.read_text().startswith(originals.get(name, "")))
                        self.assertEqual(path.read_text().count(runtime.MARKER), 1)
                    elif name in names:
                        self.assertEqual(path.read_text(), originals[name])
                    else:
                        self.assertFalse(path.exists())

    def test_bash_path_update_preserves_dotfile_symlinks_and_modes(self):
        home = self.base / "home"
        home.mkdir()
        targets = []
        for name in (".bashrc", ".bash_login"):
            target = self.base / (name + "-target")
            target.write_text(f"# original {name}\n")
            target.chmod(0o640)
            (home / name).symlink_to(target)
            targets.append(target)
        profile = home / ".profile"
        profile.write_text("# lower priority\n")
        with patch.dict(os.environ, {"HOME": str(home), "SHELL": "/bin/bash"}):
            install.add_path(Path(self.settings["bin_dir"]))
            before = {path: path.read_bytes() for path in targets}
            backups = list(home.glob("*.claude-codex-backup-*"))
            install.add_path(Path(self.settings["bin_dir"]))
        self.assertEqual(profile.read_text(), "# lower priority\n")
        self.assertEqual(list(home.glob("*.claude-codex-backup-*")), backups)
        for name, target in zip((".bashrc", ".bash_login"), targets):
            self.assertTrue((home / name).is_symlink())
            self.assertEqual((home / name).resolve(), target)
            self.assertEqual(target.read_bytes(), before[target])
            self.assertTrue(target.read_text().startswith(f"# original {name}\n"))
            self.assertEqual(target.read_text().count(runtime.MARKER), 1)
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_recursive_paseo_selections_preserve_installed_wrapper(self):
        paseo = self.fake_paseo("paseo-original", "print('original Paseo')\n")
        args = self.staged_args()
        install.install(install.parser().parse_args([*args, "--paseo-bin", str(paseo)]))
        wrapper = Path(self.settings["bin_dir"]) / "paseo-codex"
        alias = self.base / "paseo-alias"
        alias.symlink_to(wrapper)
        discovered = self.base / "paseo"
        discovered.symlink_to(wrapper)
        named_wrapper = self.fake_cli("paseo-codex", "print('another wrapper')\n")
        protected = [wrapper, Path(self.settings["config_dir"]) / "settings.json",
                     Path(self.settings["config_dir"]) / "proxy.yaml",
                     self.base / "paseo-home" / "config.json"]
        before = {path: path.read_bytes() for path in protected}
        for candidate in (wrapper, alias, named_wrapper, None):
            with self.subTest(candidate=candidate), \
                 patch.dict(os.environ, {"PATH": str(self.base)}), \
                 patch.object(install.Runtime, "stop") as stop, \
                 patch.object(install, "atomic_write") as write, \
                 patch.object(install, "write_json") as write_json:
                extra = ["--paseo-bin", str(candidate)] if candidate else []
                with self.assertRaisesRegex(runtime.SetupError, "original Paseo executable"):
                    install.install(install.parser().parse_args([*args, *extra]))
                stop.assert_not_called()
                write.assert_not_called()
                write_json.assert_not_called()
                self.assertEqual({path: path.read_bytes() for path in protected}, before)
        result = subprocess.run([str(wrapper)], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "original Paseo")

    def concurrent_installers(self, fail_first=False):
        # Each child instruments the real installer/lock and reports over a socket.
        # Gates pause the first before settings are written and at transaction end;
        # the second reports an actual failed nonblocking flock before it waits.
        script = textwrap.dedent('''
            import json, os, socket, sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            import install
            import claude_codex as runtime
            channel = socket.socket(fileno=int(sys.argv[2]))
            role, fail_first = sys.argv[3], sys.argv[4] == "True"
            opts = install.parser().parse_args(json.loads(sys.argv[5]))
            settings_path = Path(opts.config_dir) / "settings.json"
            lock_path = Path(opts.config_dir) / "install.lock"
            def emit(*event):
                channel.sendall((json.dumps(event) + "\\n").encode())
            def gate():
                if channel.recv(1) != b"x":
                    raise RuntimeError("parent closed coordination channel")
            original_flock = runtime.fcntl.flock
            def flock(handle, operation):
                if Path(handle.name) == lock_path and operation == runtime.fcntl.LOCK_EX:
                    try:
                        original_flock(handle, operation | runtime.fcntl.LOCK_NB)
                    except BlockingIOError:
                        emit("contended")
                        original_flock(handle, operation)
                    emit("acquired")
                else:
                    original_flock(handle, operation)
            runtime.fcntl.flock = flock
            original_exists = Path.exists
            def exists(path):
                value = original_exists(path)
                if path == settings_path:
                    emit("previous-check", value)
                return value
            Path.exists = exists
            original_read = install.read_json
            def read_json(path):
                value = original_read(path)
                if path == settings_path:
                    emit("previous-read", value)
                return value
            install.read_json = read_json
            original_write = install.write_json
            def write_json(path, value):
                if path == settings_path:
                    emit("settings", value)
                    if role == "first":
                        gate()
                original_write(path, value)
            install.write_json = write_json
            original_say = install.say
            def say(message, tone=None):
                original_say(message, tone)
                if role == "first" and message.startswith("Installation staged;"):
                    emit("transaction-end")
                    gate()
                    if fail_first:
                        raise RuntimeError("injected installer failure")
            install.say = say
            try:
                install.install(opts)
            except RuntimeError as exc:
                emit("failed", str(exc))
                sys.exit(19)
            emit("done")
        ''')
        args = self.staged_args("--skip-paseo")

        def start(role, extra):
            parent, child = socket.socketpair()
            parent.settimeout(10)
            try:
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(ROOT / "scripts"), str(child.fileno()),
                     role, str(fail_first), json.dumps([*args, *extra])],
                    pass_fds=(child.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env={**os.environ, "HOME": str(self.base)},
                )
            finally:
                child.close()
            def cleanup():
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
                parent.close()
            self.addCleanup(cleanup)
            return process, parent

        def receive(channel):
            line = bytearray()
            while not line.endswith(b"\n"):
                piece = channel.recv(1)
                self.assertTrue(piece, "Installer exited before its expected coordination event")
                line.extend(piece)
            return json.loads(line)

        first, first_channel = start("first", ["--port", "18431", "--reasoning", "low"])
        self.assertEqual(receive(first_channel), ["acquired"])
        self.assertEqual(receive(first_channel), ["previous-check", False])
        event, first_settings = receive(first_channel)
        self.assertEqual(event, "settings")
        self.assertFalse((Path(self.settings["config_dir"]) / "settings.json").exists())
        lock_path = Path(self.settings["config_dir"]) / "install.lock"
        lock_inode = lock_path.stat().st_ino
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(lock_path.parent.stat().st_mode & 0o777, 0o700)
        second, second_channel = start("second", ["--reasoning", "max"])
        # This event proves actual kernel lock contention, not just scheduling:
        # no previous-settings existence check or read is allowed before it.
        self.assertEqual(receive(second_channel), ["contended"])
        first_channel.sendall(b"x")
        self.assertEqual(receive(first_channel), ["transaction-end"])
        self.assertEqual(runtime.read_json(lock_path.parent / "settings.json"), first_settings)
        self.assertEqual(runtime.read_json(lock_path.parent / "proxy.yaml"), runtime.proxy_config(first_settings))
        with self.assertRaises(runtime.SetupError):
            with runtime.file_lock(lock_path, timeout=0):
                self.fail("Install lock was released before the transaction ended")
        first_channel.sendall(b"x")
        expected = ["failed", "injected installer failure"] if fail_first else ["done"]
        self.assertEqual(receive(first_channel), expected)
        self.assertEqual(receive(second_channel), ["acquired"])
        self.assertEqual(receive(second_channel), ["previous-check", True])
        self.assertEqual(receive(second_channel), ["previous-read", first_settings])
        event, second_settings = receive(second_channel)
        self.assertEqual(event, "settings")
        self.assertEqual(receive(second_channel), ["done"])
        for process, expected_code in ((first, 19 if fail_first else 0), (second, 0)):
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, expected_code, stdout + stderr)
        self.assertEqual(second_settings["api_key"], first_settings["api_key"])
        self.assertEqual(second_settings["port"], 18431)
        self.assertEqual(second_settings["reasoning"], "max")
        self.assertEqual(runtime.read_json(lock_path.parent / "settings.json"), second_settings)
        self.assertEqual(runtime.read_json(lock_path.parent / "proxy.yaml"), runtime.proxy_config(second_settings))
        self.assertEqual(lock_path.stat().st_ino, lock_inode)
        with runtime.file_lock(lock_path, timeout=0):
            pass

    def test_concurrent_installers_wait_before_reading_previous_settings(self):
        self.concurrent_installers()

    def test_installer_failure_releases_lock_for_waiting_installer(self):
        self.concurrent_installers(fail_first=True)

    def test_paseo_pull_request_policy_is_kept_current_and_optional(self):
        config_file = self.base / "paseo-policy.json"
        stale = f"{install.PR_POLICY_START}\nold policy text\n{install.PR_POLICY_END}"
        runtime.write_json(config_file, {"version": 1, "daemon": {
            "appendSystemPrompt": f"House rules first.\n\n{stale}\n\nHouse rules last."}})
        self.settings["peppy_config_dir"] = str(self.base / "claude-peppy")
        with patch.object(install, "say"):
            install.merge_paseo(config_file, self.settings, {}, include_peppy=True)
        prompt = runtime.read_json(config_file)["daemon"]["appendSystemPrompt"]
        # Text around the block is preserved; the stale block is replaced by the current policy once.
        self.assertEqual(prompt, f"House rules first.\n\n{install.PR_POLICY}\n\nHouse rules last.")
        self.assertEqual(prompt.count(install.PR_POLICY_START), 1)
        self.assertIn("gh pr create", prompt)
        self.assertIn("refs/remotes/origin/HEAD", prompt)
        # A rerun is a no-op (no backup written); a user prompt without the block gets it appended.
        with patch.object(install, "backup") as backup, patch.object(install, "say"):
            install.merge_paseo(config_file, self.settings, {"paseo_config": str(config_file)}, include_peppy=True)
        backup.assert_not_called()
        runtime.write_json(config_file, {"version": 1, "daemon": {"appendSystemPrompt": "Only mine."}})
        with patch.object(install, "say"):
            install.merge_paseo(config_file, self.settings, {}, include_peppy=True)
        self.assertEqual(runtime.read_json(config_file)["daemon"]["appendSystemPrompt"],
                         f"Only mine.\n\n{install.PR_POLICY}")
        # Opting out leaves the prompt exactly as found, and never creates a daemon section.
        runtime.write_json(config_file, {"version": 1, "daemon": {"appendSystemPrompt": "Only mine."}})
        with patch.object(install, "say"):
            install.merge_paseo(config_file, self.settings, {}, include_peppy=True, pull_requests=False)
        self.assertEqual(runtime.read_json(config_file)["daemon"], {"appendSystemPrompt": "Only mine."})
        runtime.write_json(config_file, {"version": 1})
        with patch.object(install, "say"):
            install.merge_paseo(config_file, self.settings, {}, include_peppy=True, pull_requests=False)
        self.assertNotIn("daemon", runtime.read_json(config_file))
        runtime.write_json(config_file, {"version": 1, "daemon": "bad"})
        with self.assertRaises(runtime.SetupError):
            install.merge_paseo(config_file, self.settings, {}, include_peppy=True)

    def test_paseo_merge_preserves_config_and_is_idempotent(self):
        config_file = self.base / "paseo.json"
        original = {"version": 1, "daemon": {"port": 6768}, "pluginsEnabled": False, "plugins": {
            "mine": {"source": "directory", "path": "/plugins/mine", "enabled": False},
        }, "agents": {
            "providers": {"claude": {"enabled": True}, "other": {"extends": "codex", "label": "Other"}}
        }}
        runtime.write_json(config_file, original)
        self.settings["peppy_config_dir"] = str(self.base / "claude-peppy")
        install.merge_paseo(config_file, self.settings, {}, include_peppy=True)
        actual = runtime.read_json(config_file)
        # Daemon settings are kept; only the pull request policy is appended.
        self.assertEqual(actual["daemon"], {**original["daemon"], "appendSystemPrompt": install.PR_POLICY})
        # The plugin is registered from the data directory; other plugins and
        # settings are preserved, and the global switch is turned on.
        self.assertIs(actual["pluginsEnabled"], True)
        self.assertEqual(actual["plugins"], {
            "mine": original["plugins"]["mine"],
            "claude-codex": {"source": "directory", "path": str(self.base / "data" / "paseo-plugin"), "enabled": True},
        })
        self.assertEqual(actual["agents"]["providers"]["claude"], {"enabled": True})
        self.assertEqual(actual["agents"]["providers"]["other"], original["agents"]["providers"]["other"])
        provider = actual["agents"]["providers"]["claude-codex"]
        self.assertEqual(provider["extends"], "claude")
        self.assertEqual(provider["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1050000")
        self.assertEqual(provider["env"]["CLAUDE_CODEX_PASEO_USAGE"], "1")
        self.assertNotIn("CLAUDE_CONFIG_DIR", provider["env"])
        self.assertEqual(len(provider["models"]), 6)
        xhigh = next(model for model in provider["models"] if model["id"] == "gpt-6-astra(xhigh)")
        self.assertEqual([option["id"] for option in xhigh["thinkingOptions"]], ["default", "ultracode"])
        self.assertTrue(xhigh["thinkingOptions"][0]["isDefault"])
        self.assertEqual(provider["models"][-1]["id"], runtime.ULTRACODE_MODEL)
        self.assertEqual(sum(m["isDefault"] for m in provider["models"]), 1)
        self.assertEqual(provider["models"][4]["id"], "gpt-6-astra(max)")
        peppy = actual["agents"]["providers"]["claude-peppy"]
        self.assertEqual(peppy["extends"], "claude")
        self.assertEqual(peppy["label"], "Claude Peppy")
        self.assertEqual(peppy["command"], [str(self.base / "bin" / "claude-peppy")])
        # No profile pin and no token: the launcher runs Paseo sessions in the
        # daemon's profile and reads the token from the private settings.
        self.assertEqual(peppy["env"], {"CLAUDE_PEPPY_PASEO": "1"})
        self.assertNotIn("models", peppy)
        self.assertNotIn("test-local-key", config_file.read_text())
        self.settings["peppy_oauth_token"] = "sk-ant-oat01-secret"
        self.assertNotIn("secret", config_file.read_text())
        first = config_file.read_bytes()
        install.merge_paseo(config_file, self.settings, {"paseo_config": str(config_file)}, include_peppy=True)
        self.assertEqual(config_file.read_bytes(), first)
        self.assertEqual(len(list(self.base.glob("paseo.json.claude-codex-backup-*"))), 1)
        # A plugin the user disabled stays disabled across reruns.
        disabled = runtime.read_json(config_file)
        disabled["plugins"]["claude-codex"]["enabled"] = False
        runtime.write_json(config_file, disabled)
        install.merge_paseo(config_file, self.settings, {"paseo_config": str(config_file)}, include_peppy=True)
        self.assertIs(runtime.read_json(config_file)["plugins"]["claude-codex"]["enabled"], False)
        # A plugin entry left by an installation in another data directory is ours to move.
        moved = {**self.settings, "data_dir": str(self.base / "moved-data")}
        install.merge_paseo(config_file, moved, {"paseo_config": str(config_file),
                                                 "data_dir": self.settings["data_dir"]}, include_peppy=True)
        self.assertEqual(runtime.read_json(config_file)["plugins"]["claude-codex"]["path"],
                         str(self.base / "moved-data" / "paseo-plugin"))

    def test_paseo_merge_without_peppy_leaves_existing_entry_untouched(self):
        config_file = self.base / "paseo.json"
        self.settings["peppy_config_dir"] = str(self.base / "claude-peppy")
        runtime.write_json(config_file, {"version": 1, "agents": {"providers": {
            "claude-peppy": {"extends": "claude", "label": "User-renamed Peppy", "env": {}}
        }}})
        install.merge_paseo(config_file, self.settings, {"paseo_config": str(config_file)}, include_peppy=False)
        providers = runtime.read_json(config_file)["agents"]["providers"]
        self.assertEqual(providers["claude-peppy"]["label"], "User-renamed Peppy")
        self.assertIn("claude-codex", providers)

    def test_unrelated_same_name_provider_is_rejected_per_key(self):
        for name, expected in (("claude-codex", "claude-codex"), ("claude-peppy", "claude-peppy")):
            with self.subTest(provider=name):
                config_file = self.base / f"paseo-{name}.json"
                runtime.write_json(config_file, {"version": 1, "agents": {"providers": {
                    name: {"extends": "claude", "label": "Unrelated"}
                }}})
                self.settings["peppy_config_dir"] = str(self.base / "claude-peppy")
                with self.assertRaises(runtime.SetupError) as caught:
                    install.merge_paseo(config_file, self.settings, {}, include_peppy=True)
                self.assertIn(expected, str(caught.exception))
        config_file = self.base / "paseo-plugin-conflict.json"
        foreign = {"version": 1, "plugins": {"claude-codex": {"source": "directory", "path": "/elsewhere"}}}
        runtime.write_json(config_file, foreign)
        for previous in ({}, {"paseo_config": str(config_file), "data_dir": self.settings["data_dir"]}):
            with self.subTest(previous=previous):
                with self.assertRaisesRegex(runtime.SetupError, "unrelated plugin named claude-codex"):
                    install.merge_paseo(config_file, self.settings, previous, include_peppy=False)
                self.assertEqual(runtime.read_json(config_file), foreign)
        runtime.write_json(config_file, {"version": 1, "plugins": []})
        with self.assertRaisesRegex(runtime.SetupError, "plugins must be an object"):
            install.merge_paseo(config_file, self.settings, {}, include_peppy=False)

    def test_invalid_paseo_config_not_overwritten(self):
        for content in ('{"agents":[]}', '{"agents":{"providers":[]}}', '{bad', '[]'):
            path = self.base / "invalid.json"
            path.write_text(content)
            with self.assertRaises(runtime.SetupError):
                install.merge_paseo(path, self.settings, {}, include_peppy=False)
            self.assertEqual(path.read_text(), content)

    def test_download_checksum_failure_never_installs(self):
        def fake_download(url, path):
            Path(path).write_bytes(b"tampered artifact")
        with patch.object(install, "download", fake_download), \
             patch.object(install.platform, "system", return_value="Linux"), \
             patch.object(install.platform, "machine", return_value="x86_64"), \
             self.assertRaisesRegex(runtime.SetupError, "SHA-256"):
            install.install_proxy(self.base, install.PROXY_VERSION)
        self.assertFalse((self.base / "releases" / install.PROXY_VERSION / "cli-proxy-api").exists())

    def test_safe_archive_extraction(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for name, content in (("cli-proxy-api", b"test binary"), ("../../escape", b"bad")):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        data = archive.getvalue()
        with patch.object(install, "download", lambda url, path: Path(path).write_bytes(data)), \
             patch.object(install.platform, "system", return_value="Linux"), \
             patch.object(install.platform, "machine", return_value="x86_64"), \
             patch.dict(install.CHECKSUMS, {"linux_amd64_no-plugin": hashlib.sha256(data).hexdigest()}):
            binary = install.install_proxy(self.base, install.PROXY_VERSION)
        self.assertEqual(binary.read_bytes(), b"test binary")
        self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.base.parent / "escape").exists())

    def test_full_staged_install_and_launch_with_sdk_arguments(self):
        claude = self.fake_cli("claude-original", "import json,os,sys\nprint(json.dumps({'args':sys.argv[1:],'model':os.environ.get('ANTHROPIC_MODEL'),'base':os.environ.get('ANTHROPIC_BASE_URL')}))\n")
        paseo = self.fake_paseo("paseo-original", "print('paseo original')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        claude_before = claude.read_bytes()
        args = ["bash", str(ROOT / "install.sh"), "--config-dir", self.settings["config_dir"],
                "--data-dir", self.settings["data_dir"], "--state-dir", self.settings["state_dir"],
                "--bin-dir", self.settings["bin_dir"], "--paseo-home", str(self.base / "paseo"),
                "--claude-bin", str(claude), "--paseo-bin", str(paseo), "--proxy-binary", str(proxy),
                "--peppy-config-dir", str(self.base / "claude-peppy"),
                "--skip-login", "--skip-paseo-start", "--no-path"]
        module = install.legacy_agent_module(paseo)
        subprocess.run(args, check=True, capture_output=True, text=True)
        config_file = Path(self.settings["config_dir"]) / "settings.json"
        first = runtime.read_json(config_file)
        self.assertEqual(first["peppy_config_dir"], str(self.base / "claude-peppy"))
        self.assertEqual((self.base / "claude-peppy").stat().st_mode & 0o777, 0o700)
        peppy_launcher = Path(self.settings["bin_dir"]) / "claude-peppy"
        self.assertEqual(peppy_launcher.read_text().splitlines()[1], install.MARKER)
        peppy_provider_entry = runtime.read_json(self.base / "paseo" / "config.json")["agents"]["providers"]["claude-peppy"]
        self.assertEqual(peppy_provider_entry["command"], [str(peppy_launcher)])
        self.assertEqual(peppy_provider_entry["env"], {"CLAUDE_PEPPY_PASEO": "1"})
        # Paseo's own module is left exactly as shipped; the plugin is installed instead.
        self.assertEqual(module.read_text(), self.UPSTREAM_MODULE)
        self.assertFalse(list(module.parent.glob("agent.js.claude-codex-backup-*")))
        paseo_settings = runtime.read_json(self.base / "paseo" / "config.json")
        plugin_dir = Path(self.settings["data_dir"]) / "paseo-plugin"
        self.assertIs(paseo_settings["pluginsEnabled"], True)
        self.assertEqual(paseo_settings["plugins"],
                         {"claude-codex": {"source": "directory", "path": str(plugin_dir), "enabled": True}})
        for name in install.PLUGIN_FILES:
            self.assertEqual((plugin_dir / name).read_text(), (ROOT / "scripts" / "paseo_plugin" / name).read_text())
        (plugin_dir / "stale.server.ts").write_text("// from an earlier version\n")
        installed_runtime = Path(self.settings["data_dir"]) / "claude_codex.py"
        installed_runtime.write_text("# outdated installed launcher\n")
        auth = Path(self.settings["config_dir"]) / "auth" / "preserved.json"
        runtime.write_json(auth, {"type": "codex", "refresh_token": "dummy-preserve-only"})
        paseo_config = self.base / "paseo" / "config.json"
        providers = runtime.read_json(paseo_config)
        providers["agents"]["providers"]["unrelated"] = {"extends": "codex", "label": "Keep me"}
        runtime.write_json(paseo_config, providers)
        subprocess.run(args, check=True, capture_output=True, text=True)
        self.assertEqual(first, runtime.read_json(config_file))
        self.assertEqual(module.read_text(), self.UPSTREAM_MODULE)
        self.assertFalse(list(module.parent.glob("agent.js.claude-codex-backup-*")))
        self.assertFalse((plugin_dir / "stale.server.ts").exists())
        self.assertEqual(installed_runtime.read_bytes(), (ROOT / "scripts" / "claude_codex.py").read_bytes())
        self.assertEqual(runtime.read_json(auth)["refresh_token"], "dummy-preserve-only")
        self.assertEqual(runtime.read_json(paseo_config), providers)
        # A module still carrying an earlier installer's patch is reported, never touched.
        legacy = self.UPSTREAM_MODULE + install.LEGACY_PATCH_MARKER + " (begin)\n"
        module.write_text(legacy)
        result = subprocess.run(args, check=True, capture_output=True, text=True)
        self.assertIn("still carries the source patch", result.stderr)
        self.assertIn("npm install -g @getpaseo/cli", result.stderr)
        self.assertEqual(module.read_text(), legacy)
        self.assertFalse(list(module.parent.glob("agent.js.claude-codex-backup-*")))
        self.assertEqual(runtime.read_json(paseo_config), providers)
        self.assertEqual(claude.read_bytes(), claude_before)
        self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.settings["config_dir"]).stat().st_mode & 0o777, 0o700)
        launcher = Path(self.settings["bin_dir"]) / "claude-codex"
        result = subprocess.run([str(launcher), "--version"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["args"], ["--version"])
        self.assertEqual(result.stderr, "")
        with patch.object(runtime.Runtime, "has_login", return_value=True), \
             patch.object(runtime.Runtime, "start"), patch.object(runtime.os, "execve") as execute:
            runtime.launch(first, ["--model", "gpt-6-astra(xhigh)", "--input-format", "stream-json", "-p"])
        binary, forwarded, env = execute.call_args.args
        self.assertEqual(binary, str(claude))
        # Claude's own setting keeps its auto-mode classifier off this gateway;
        # the session falls back to ordinary permission prompts.
        self.assertEqual(forwarded, [str(claude), "--model", "gpt-6-astra(xhigh)", "--input-format", "stream-json", "-p",
                                     "--settings", '{"permissions":{"disableAutoMode":"disable"}}'])
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-6-astra(xhigh)")
        with patch.object(runtime.Runtime, "has_login", return_value=True), \
             patch.object(runtime.Runtime, "start"), patch.object(runtime.os, "execve") as execute, \
             patch.dict(os.environ, {"CLAUDE_CODEX_AUTO_MODE": "1"}):
            runtime.launch(first, ["--model", "gpt-6-astra(xhigh)", "-p"])
        self.assertEqual(execute.call_args.args[1], [str(claude), "--model", "gpt-6-astra(xhigh)", "-p"])

    def test_paseo_module_content_is_never_a_concern(self):
        paseo = self.fake_paseo("paseo-original", "print('original Paseo')\n")
        module = install.legacy_agent_module(paseo)
        module.write_text("// unfamiliar upstream implementation\nexport {};\n")
        original = module.read_bytes()
        with patch.object(install, "say") as say:
            install.install(install.parser().parse_args(self.staged_args("--paseo-bin", str(paseo))))
        self.assertEqual(module.read_bytes(), original)
        self.assertFalse(list(module.parent.glob("agent.js.claude-codex-backup-*")))
        self.assertFalse(any("source patch" in call.args[0] for call in say.call_args_list))
        self.assertIn("claude-codex", runtime.read_json(self.base / "paseo-home" / "config.json")["plugins"])
        # Packages whose module cannot be located, such as the desktop bundle, are skipped.
        self.assertIsNone(install.legacy_agent_module(self.base / "missing-paseo"))
        bundle = self.fake_desktop_bundle() / "paseo"
        self.assertIsNone(install.legacy_agent_module(bundle))
        self.assertFalse(install.warn_legacy_patch(bundle))
        module.unlink()
        self.assertIsNone(install.legacy_agent_module(paseo))
        self.assertFalse(install.warn_legacy_patch(paseo))

    def test_absent_paseo_is_skipped_without_npm_or_config_changes(self):
        claude = self.fake_cli("claude-original", "print('Claude Code')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        args = ["--config-dir", self.settings["config_dir"], "--data-dir", self.settings["data_dir"],
                "--state-dir", self.settings["state_dir"], "--bin-dir", self.settings["bin_dir"],
                "--paseo-home", str(self.base / "absent-paseo"), "--claude-bin", str(claude),
                "--peppy-config-dir", str(self.base / "claude-peppy"),
                "--proxy-binary", str(proxy), "--skip-login", "--no-path"]
        with patch.dict(os.environ, {"PATH": ""}), patch.object(install.subprocess, "run") as run:
            install.install(install.parser().parse_args(args))
        run.assert_not_called()
        self.assertFalse((self.base / "absent-paseo").exists())
        self.assertFalse((Path(self.settings["bin_dir"]) / "paseo-codex").exists())
        self.assertFalse((Path(self.settings["bin_dir"]) / install.WORKTREE_SETUP_COMMAND).exists())
        self.assertFalse((Path(self.settings["data_dir"]) / "npm" / "paseo").exists())

    def test_installer_reuses_system_paseo_over_saved_private_copy(self):
        claude = self.fake_cli("claude-original", "print('Claude Code')\n")
        system_paseo = self.fake_paseo("paseo", "print('paseo on PATH')\n")
        private_paseo = self.fake_paseo("paseo-private", "print('private paseo')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        original = system_paseo.read_bytes()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        args = ["--config-dir", self.settings["config_dir"], "--data-dir", self.settings["data_dir"],
                "--state-dir", self.settings["state_dir"], "--bin-dir", self.settings["bin_dir"],
                "--paseo-home", str(self.base / "paseo-home"), "--port", str(port),
                "--peppy-config-dir", str(self.base / "claude-peppy"),
                "--claude-bin", str(claude), "--proxy-binary", str(proxy), "--skip-login", "--no-path"]
        install.install(install.parser().parse_args(args + ["--paseo-bin", str(private_paseo), "--skip-paseo-start"]))
        test_path = str(self.base) + os.pathsep + os.environ.get("PATH", "")
        def check_activation(command, **kwargs):
            self.assertEqual(install.legacy_agent_module(system_paseo).read_text(), self.UPSTREAM_MODULE)
            self.assertTrue((Path(self.settings["data_dir"]) / "paseo-plugin" / "index.server.ts").is_file())
            # The daemon inherits this environment and must find the worktree
            # setup launcher named in paseo.json.
            self.assertTrue(kwargs["env"]["PATH"].startswith(self.settings["bin_dir"] + os.pathsep))
            self.assertEqual(kwargs["env"]["PATH"].split(os.pathsep).count(self.settings["bin_dir"]), 1)
            self.assertIn(str(self.base), kwargs["env"]["PATH"].split(os.pathsep))
            return subprocess.CompletedProcess(command, 0)
        with patch.dict(os.environ, {"PATH": test_path}), \
             patch.object(install.subprocess, "run", side_effect=check_activation) as run:
            install.install(install.parser().parse_args(args))
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [[str(system_paseo), "daemon", "restart"], [str(system_paseo), "reload"]])
        saved = runtime.read_json(Path(self.settings["config_dir"]) / "settings.json")
        self.assertEqual(saved["paseo_bin"], str(system_paseo))
        self.assertEqual(system_paseo.read_bytes(), original)
        self.assertFalse((Path(self.settings["data_dir"]) / "npm" / "paseo").exists())
        wrapper = Path(self.settings["bin_dir"]) / "paseo-codex"
        result = subprocess.run([str(wrapper), "--version"], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "paseo on PATH")
        setup = Path(self.settings["bin_dir"]) / install.WORKTREE_SETUP_COMMAND
        self.assertIn(" worktree-setup ", setup.read_text())
        self.assertTrue(install.launcher_is_owned(setup))
        result = subprocess.run([str(setup), "--help"], check=True, capture_output=True, text=True)
        self.assertIn("init", result.stdout)
        self.assertEqual(runtime.read_json(self.base / "paseo-home" / "config.json")["daemon"]["appendSystemPrompt"],
                         install.PR_POLICY)

    def test_explicit_paseo_is_used_without_version_checks(self):
        custom = self.fake_cli("paseo-custom", "print('custom paseo')\n")
        with patch.object(install.subprocess, "check_output") as version_probe:
            self.assertEqual(install.detect_paseo(str(custom)), str(custom))
        version_probe.assert_not_called()

    def test_missing_system_paseo_does_not_select_legacy_private_copy(self):
        private = self.base / "npm" / "paseo" / "node_modules" / ".bin" / "paseo"
        private.parent.mkdir(parents=True)
        private.write_text("#!/bin/sh\necho legacy private paseo\n")
        private.chmod(0o755)
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertIsNone(install.detect_paseo(None))

    def fake_desktop_bundle(self):
        resources = self.base / "Paseo.app" / "Contents" / "Resources"
        entry = resources / "bin" / "paseo"
        entry.parent.mkdir(parents=True)
        entry.write_text("#!/bin/sh\necho bundle\n")
        entry.chmod(0o755)
        (resources / "app.asar").write_bytes(b"")
        path_dir = self.base / "bundle-path"
        path_dir.mkdir()
        (path_dir / "paseo").symlink_to(entry)
        return path_dir

    def test_desktop_bundle_on_path_falls_back_to_previously_selected_paseo(self):
        path_dir = self.fake_desktop_bundle()
        previous = self.fake_cli("paseo-npm", "print('npm paseo')\n")
        with patch.dict(os.environ, {"PATH": str(path_dir)}):
            self.assertEqual(install.detect_paseo(None, str(previous), self.base), str(previous))
            self.assertEqual(install.detect_paseo(None, None, self.base), str(path_dir / "paseo"))
            self.assertEqual(install.detect_paseo(None, str(self.base / "gone"), self.base), str(path_dir / "paseo"))

    def test_desktop_bundle_on_path_does_not_fall_back_to_legacy_private_copy(self):
        path_dir = self.fake_desktop_bundle()
        private = self.base / "npm" / "paseo" / "node_modules" / ".bin" / "paseo"
        private.parent.mkdir(parents=True)
        private.write_text("#!/bin/sh\necho legacy private paseo\n")
        private.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(path_dir)}):
            self.assertEqual(install.detect_paseo(None, str(private), self.base), str(path_dir / "paseo"))

    def test_usable_paseo_on_path_wins_over_previous_selection(self):
        previous = self.fake_cli("paseo-previous", "print('previous')\n")
        system = self.fake_cli("paseo", "print('on path')\n")
        with patch.dict(os.environ, {"PATH": str(system.parent)}):
            self.assertEqual(install.detect_paseo(None, str(previous), self.base), str(system))

    def test_plane_prompt_is_masked_optional_and_suppressed_when_staging(self):
        opts = install.parser().parse_args(self.staged_args("--skip-paseo"))
        config, data = Path(opts.config_dir), Path(opts.data_dir)
        with patch.object(install.sys.stdin, "isatty", return_value=True), \
             patch.object(install, "masked_input", return_value="prompt-plane-token") as prompt:
            self.assertIsNone(install.prepare_plane(opts, {}, config, data, None))
            prompt.assert_not_called()
            opts.skip_login = False
            with patch.object(install, "say") as say:
                self.assertEqual(install.prepare_plane(opts, {}, config, data, None), "prompt-plane-token")
            prompt.assert_called_once_with("Plane API token (masked with *; Enter to skip): ")
            self.assertTrue(any("https://app.plane.so/settings/profile/api-tokens" in call.args[0]
                                for call in say.call_args_list))
            self.assertFalse(any("prompt-plane-token" in call.args[0] for call in say.call_args_list))
            prompt.return_value = ""
            self.assertIsNone(install.prepare_plane(opts, {}, config, data, None))
            prompt.side_effect = OSError("sensitive fallback details")
            with self.assertRaisesRegex(runtime.SetupError, "masked Plane token") as raised:
                install.prepare_plane(opts, {}, config, data, None)
            self.assertNotIn("sensitive", str(raised.exception))
        with patch.object(install.sys.stdin, "isatty", return_value=False), \
             patch.object(install, "masked_input") as prompt:
            self.assertIsNone(install.prepare_plane(opts, {}, config, data, None))
            prompt.assert_not_called()
        self.assertFalse((config / "plane-credentials.json").exists())

    def test_plane_credential_precedence_and_redacted_input_errors(self):
        opts = install.parser().parse_args(self.staged_args("--skip-paseo"))
        config, data = Path(opts.config_dir), Path(opts.data_dir)
        previous = {"plane_mcp_config": str(config / "plane-mcp.json")}
        runtime.write_json(config / "plane-credentials.json", {"workspace": "peppy", "api_key": "saved-plane-token"})
        token_file = self.base / "supplied-token"
        token_file.write_text("file-plane-token\n")
        with patch.object(install, "masked_input") as prompt:
            self.assertEqual(install.prepare_plane(opts, previous, config, data, None), "saved-plane-token")
            self.assertEqual(install.prepare_plane(opts, previous, config, data, "environment-token"), "environment-token")
            opts.plane_api_key_file = str(token_file)
            self.assertEqual(install.prepare_plane(opts, previous, config, data, "environment-token"), "file-plane-token")
            for value in ("secret-token\nsecond-line", "", "secret-token\x00"):
                token_file.write_text(value)
                with self.subTest(value=value), self.assertRaises(runtime.SetupError) as raised:
                    install.prepare_plane(opts, previous, config, data, "environment-token")
                self.assertNotIn("secret-token", str(raised.exception))
            token_file.write_bytes(b"\xff")
            with self.assertRaisesRegex(runtime.SetupError, "UTF-8"):
                install.prepare_plane(opts, previous, config, data, None)
            opts.plane_api_key_file = str(self.base / "missing-token")
            with self.assertRaisesRegex(runtime.SetupError, "Cannot read"):
                install.prepare_plane(opts, previous, config, data, None)
            opts.plane_api_key_file = None
            with self.assertRaises(runtime.SetupError):
                install.prepare_plane(opts, previous, config, data, "")
            prompt.assert_not_called()

    def test_plane_invalid_credentials_fail_before_installation_changes(self):
        opts = install.parser().parse_args(self.staged_args("--skip-paseo"))
        with patch.dict(os.environ, {"PLANE_API_KEY": "secret-token\ninvalid"}), \
             patch.object(install, "resolve_cli") as resolve, \
             patch.object(install.Runtime, "stop") as stop, \
             patch.object(install, "atomic_write") as write, \
             patch.object(install, "write_json") as write_json:
            with self.assertRaises(runtime.SetupError) as raised:
                install.install(opts)
            self.assertNotIn("secret-token", str(raised.exception))
            self.assertNotIn("PLANE_API_KEY", os.environ)
        resolve.assert_not_called()
        stop.assert_not_called()
        write.assert_not_called()
        write_json.assert_not_called()

    def test_plane_unowned_corrupt_and_symlinked_artifacts_are_preserved(self):
        opts = install.parser().parse_args(self.staged_args("--skip-paseo"))
        config, data = Path(opts.config_dir), Path(opts.data_dir)
        credentials = config / "plane-credentials.json"
        runtime.write_json(credentials, {"workspace": "another-workspace", "api_key": "secret-unrelated"})
        before = credentials.read_bytes()
        previous = {"plane_mcp_config": str(config / "plane-mcp.json")}
        for saved in ({}, previous):
            with self.subTest(saved=saved), self.assertRaises(runtime.SetupError) as raised:
                install.prepare_plane(opts, saved, config, data, "new-token")
            self.assertNotIn("secret-unrelated", str(raised.exception))
            self.assertEqual(credentials.read_bytes(), before)
        target = self.base / "external-credentials"
        credentials.rename(target)
        credentials.symlink_to(target)
        with self.assertRaisesRegex(runtime.SetupError, "symlinked"):
            install.prepare_plane(opts, previous, config, data, "new-token")
        self.assertTrue(credentials.is_symlink())
        self.assertEqual(target.read_bytes(), before)

    def test_plane_staged_shell_install_reuses_rotates_and_loads_installed_mcp(self):
        paseo = self.fake_paseo("paseo-plane", "print('Paseo')\n")
        args = ["bash", str(ROOT / "install.sh"), *self.staged_args("--paseo-bin", str(paseo))]
        env = {key: value for key, value in os.environ.items() if key != "PLANE_API_KEY"}
        env["HOME"] = str(self.base / "home")
        first_token, next_token = "fixture-plane-token-first", "fixture-plane-token-rotated"
        result = subprocess.run(args, env={**env, "PLANE_API_KEY": first_token}, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30, check=True)
        self.assertNotIn(first_token, result.stdout + result.stderr)
        config, data = Path(self.settings["config_dir"]), Path(self.settings["data_dir"])
        settings = runtime.read_json(config / "settings.json")
        credentials = config / "plane-credentials.json"
        self.assertEqual(runtime.read_json(credentials), {"workspace": "peppy", "api_key": first_token})
        self.assertEqual(settings["plane_mcp_config"], str(config / "plane-mcp.json"))
        mcp = runtime.read_json(Path(settings["plane_mcp_config"]))["mcpServers"]
        self.assertEqual(list(mcp), [install.plane_mcp.SERVER_NAME])
        server = mcp[install.plane_mcp.SERVER_NAME]
        self.assertTrue(Path(server["command"]).is_absolute())
        self.assertTrue(os.path.samefile(server["command"], shutil.which("python3")))
        self.assertEqual(server["args"], [str(data / "plane_mcp.py"), "--credentials", str(credentials)])
        self.assertEqual((data / "plane_mcp.py").read_bytes(), (ROOT / "scripts" / "plane_mcp.py").read_bytes())
        # The Paseo plugin carries the same definition, so Paseo's own Claude
        # provider gets the connector too, not only the launcher-based ones.
        connector = data / "paseo-plugin" / install.PLUGIN_CONNECTOR_FILE
        self.assertEqual(connector.read_text(), install.plane_connector_module(config / "plane-mcp.json"))
        self.assertIn(json.dumps(server["args"][0]), connector.read_text())
        self.assertNotIn(first_token, connector.read_text())
        for path in (credentials, config / "plane-mcp.json", data / "plane_mcp.py"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for path in (config / "settings.json", config / "plane-mcp.json",
                     self.base / "paseo-home" / "config.json",
                     Path(self.settings["bin_dir"]) / "claude-codex"):
            self.assertNotIn(first_token, path.read_text())
        before = credentials.read_bytes()
        credentials.chmod(0o644)
        subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
        self.assertEqual(runtime.read_json(config / "settings.json"), settings)
        self.assertEqual(credentials.read_bytes(), before)
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        # Exercise the actual generated command without any tool that contacts Plane.
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "offline-installer-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        protocol = subprocess.run([server["command"], *server["args"]], env=env, text=True,
                                  input="".join(json.dumps(message) + "\n" for message in messages),
                                  capture_output=True, timeout=10, check=True)
        replies = [json.loads(line) for line in protocol.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], install.plane_mcp.SERVER_NAME)
        self.assertEqual({tool["name"] for tool in replies[1]["result"]["tools"]},
                         {"list_projects", "list_work_items", "get_work_item"})
        self.assertNotIn(first_token, protocol.stdout + protocol.stderr)
        token_file = self.base / "rotated-token"
        token_file.write_text(next_token + "\n")
        result = subprocess.run([*args, "--plane-api-key-file", str(token_file)], env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=True)
        self.assertNotIn(next_token, result.stdout + result.stderr)
        self.assertEqual(runtime.read_json(credentials)["api_key"], next_token)
        self.assertFalse(list(config.glob("plane-credentials.json.claude-codex-backup-*")))

    def test_plane_skip_retains_connection_and_does_not_consume_replacement(self):
        opts = install.parser().parse_args(self.staged_args("--skip-paseo"))
        with patch.dict(os.environ, {"PLANE_API_KEY": "retained-plane-token"}):
            install.install(opts)
        config, data = Path(opts.config_dir), Path(opts.data_dir)
        paths = [config / "plane-credentials.json", config / "plane-mcp.json", data / "plane_mcp.py"]
        before = {path: path.read_bytes() for path in paths}
        opts.skip_plane = True
        with patch.dict(os.environ, {"PLANE_API_KEY": "ignored-replacement"}), \
             patch.object(install, "masked_input") as prompt:
            install.install(opts)
            self.assertNotIn("PLANE_API_KEY", os.environ)
            prompt.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(runtime.read_json(config / "settings.json")["plane_mcp_config"], str(config / "plane-mcp.json"))

    def test_plane_changed_data_directory_does_not_overwrite_unrelated_helper(self):
        opts = install.parser().parse_args(self.staged_args("--skip-paseo"))
        with patch.dict(os.environ, {"PLANE_API_KEY": "move-plane-token"}):
            install.install(opts)
        config = Path(opts.config_dir)
        protected = [config / name for name in ("settings.json", "plane-credentials.json", "plane-mcp.json")]
        before = {path: path.read_bytes() for path in protected}
        new_data = self.base / "new-data"
        new_data.mkdir()
        helper = new_data / "plane_mcp.py"
        helper.write_text("unrelated existing program\n")
        opts.data_dir = str(new_data)
        with patch.object(install.Runtime, "stop") as stop, \
             patch.object(install, "atomic_write") as write, \
             patch.object(install, "write_json") as write_json:
            with self.assertRaisesRegex(runtime.SetupError, "unrelated"):
                install.install(opts)
        stop.assert_not_called()
        write.assert_not_called()
        write_json.assert_not_called()
        self.assertEqual(helper.read_text(), "unrelated existing program\n")
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        # Once our fixture collision is removed, a new directory is supported.
        helper.unlink()
        install.install(opts)
        self.assertEqual(helper.read_bytes(), (ROOT / "scripts" / "plane_mcp.py").read_bytes())
        self.assertEqual(runtime.read_json(config / "settings.json")["data_dir"], str(new_data))
        self.assertEqual(runtime.read_json(config / "plane-mcp.json")["mcpServers"][install.plane_mcp.SERVER_NAME]["args"][0],
                         str(helper))

    def test_plane_input_environment_is_not_inherited_by_daemon(self):
        paseo = self.fake_paseo("paseo-plane", "print('Paseo')\n")
        opts = install.parser().parse_args(self.staged_args("--paseo-bin", str(paseo)))
        opts.skip_paseo_start = False
        def run(command, **kwargs):
            self.assertNotIn("PLANE_API_KEY", os.environ)
            self.assertNotIn("PLANE_API_KEY", kwargs.get("env", {}))
            return subprocess.CompletedProcess(command, 0)
        with patch.dict(os.environ, {"PLANE_API_KEY": "not-for-daemon"}), \
             patch.object(install.subprocess, "run", side_effect=run) as spawn:
            install.install(opts)
        self.assertEqual([call.args[0] for call in spawn.call_args_list],
                         [[str(paseo), "daemon", "restart"], [str(paseo), "reload"]])

    def test_peppy_token_precedence_validation_and_prompt(self):
        opts = install.parser().parse_args(self.staged_args("--skip-paseo"))
        token_file = self.base / "peppy-token.txt"
        token_file.write_text("sk-ant-oat01-from-file\n")
        opts.peppy_oauth_token_file = str(token_file)
        saved = {"peppy_oauth_token": "sk-ant-oat01-saved"}
        self.assertEqual(install.prepare_peppy_token(opts, saved, "sk-ant-oat01-from-env"), "sk-ant-oat01-from-file")
        opts.peppy_oauth_token_file = None
        self.assertEqual(install.prepare_peppy_token(opts, saved, "sk-ant-oat01-from-env"), "sk-ant-oat01-from-env")
        self.assertEqual(install.prepare_peppy_token(opts, saved, None), "sk-ant-oat01-saved")
        for bad in ("", "   \n", "two words", "tab\tinside"):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(runtime.SetupError, "single line"):
                    install.prepare_peppy_token(opts, {}, bad)
        token_file.write_text("with space inside\n")
        opts.peppy_oauth_token_file = str(token_file)
        with self.assertRaisesRegex(runtime.SetupError, "single line"):
            install.prepare_peppy_token(opts, {}, None)
        opts.peppy_oauth_token_file = str(self.base / "missing-token-file")
        with self.assertRaisesRegex(runtime.SetupError, "Cannot read --peppy-oauth-token-file"):
            install.prepare_peppy_token(opts, {}, None)
        opts.peppy_oauth_token_file = None
        # Selection never prompts: the interactive sign-in needs the installed
        # Claude binary and happens later in the run (acquire_peppy_token).
        for interactive in (True, False):
            opts.skip_login = False
            with patch.object(install.sys.stdin, "isatty", return_value=interactive), \
                 patch.object(install, "masked_input") as prompt:
                self.assertIsNone(install.prepare_peppy_token(opts, {}, None))
                self.assertIsNone(install.prepare_peppy_token(opts, {"peppy_oauth_token": ""}, None))
                prompt.assert_not_called()

    def test_acquire_peppy_token_offers_sign_in_paste_and_skip(self):
        settings = {**self.settings, "claude_bin": "/opt/claude", "peppy_config_dir": str(self.base / "claude-peppy")}
        with patch.object(install, "masked_input", return_value="") as prompt, \
             patch.object(install, "sign_in_second_account", return_value="sk-ant-oat01-generated") as sign_in, \
             patch.object(install, "say") as say:
            self.assertEqual(install.acquire_peppy_token(settings), "sk-ant-oat01-generated")
            sign_in.assert_called_once_with(settings)
            prompt.assert_called_once_with("Second account token (Enter to sign in now, paste a token, or type skip): ")
            messages = [call.args[0] for call in say.call_args_list]
            self.assertTrue(any("You do not need to have one yet" in message for message in messages))
            self.assertTrue(any("claude-peppy setup-token" in message for message in messages))
            self.assertFalse(any("sk-ant-oat01-generated" in call.args[0] for call in say.call_args_list))
            # A pasted token is used as is; skip saves nothing and does not sign in.
            sign_in.reset_mock()
            prompt.return_value = " sk-ant-oat01-pasted \n"
            self.assertEqual(install.acquire_peppy_token(settings), "sk-ant-oat01-pasted")
            self.assertFalse(any("sk-ant-oat01-pasted" in call.args[0] for call in say.call_args_list))
            prompt.return_value = "SKIP"
            self.assertIsNone(install.acquire_peppy_token(settings))
            sign_in.assert_not_called()
            prompt.return_value = "two words"
            with self.assertRaisesRegex(runtime.SetupError, "single line"):
                install.acquire_peppy_token(settings)
            # A failed sign-in leaves the installation usable and prints the fallback.
            prompt.return_value = ""
            sign_in.side_effect = runtime.SetupError("claude setup-token did not print a token")
            say.reset_mock()
            self.assertIsNone(install.acquire_peppy_token(settings))
            messages = [call.args[0] for call in say.call_args_list]
            self.assertTrue(any("did not print a token" in message for message in messages))
            self.assertTrue(any("claude-peppy setup-token" in message for message in messages))
            prompt.side_effect = OSError("sensitive terminal details")
            with self.assertRaisesRegex(runtime.SetupError, "masked token") as raised:
                install.acquire_peppy_token(settings)
            self.assertNotIn("sensitive", str(raised.exception))

    def test_printed_oauth_tokens_reads_wrapped_and_redrawn_ink_output(self):
        # Claude Code prints the token as its own paragraph and hard-wraps it at
        # the terminal width; earlier frames are redrawn with cursor movement.
        frame = ("\x1b[2K\x1b[1A\x1b[2K\x1b[G  Long-lived authentication token created successfully!\r\n\r\n"
                 "  Your OAuth token (valid for 1y):\r\n\r\n"
                 "  \x1b[33msk-ant-oat01-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x1b[39m\r\n"
                 "  \x1b[33mBBBBBBBBBBBBBBBB_CC-DD\x1b[39m\r\n\r\n"
                 "  \x1b[2mStore this token securely. You won't be able to see it again.\x1b[22m\r\n\r\n"
                 "  \x1b[2mUse this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\x1b[22m\r\n")
        spinner = "\x1b]0;claude\x07\x1b[?25l  ⠋ Waiting for login...\r\n\x1b[1A\x1b[2K  ⠙ Waiting for login...\r\n"
        output = (spinner + frame + frame).encode()
        token = "sk-ant-oat01-" + "A" * 56 + "BBBBBBBBBBBBBBBB_CC-DD"
        self.assertEqual(install.printed_oauth_tokens(output), {token})
        # An unwrapped token, a token followed by other text, and no token at all.
        self.assertEqual(install.printed_oauth_tokens(b"x\r\n\x1b[33msk-ant-oat01-short\x1b[39m\r\n\r\ny"), {"sk-ant-oat01-short"})
        self.assertEqual(install.printed_oauth_tokens(b"see sk-ant-oat01-short: below\r\n\r\n"), set())
        self.assertEqual(install.printed_oauth_tokens(b"\r\nsk-ant-oat02-newer_format\r\n\r\n"), {"sk-ant-oat02-newer_format"})
        self.assertEqual(install.printed_oauth_tokens(b"Browser didn't open? Use the url below\r\n"), set())

    def test_url_unwrapper_rejoins_wrapped_url_tail_and_passes_prompts_through(self):
        url = "https://claude.com/cai/oauth/authorize?code=true&state=" + "x" * 300 + "M"
        link = lambda text: f"\x1b]8;id=1;{url}\x07{text}\x1b]8;;\x07".encode()
        frame = (b" Browser didn't open? Use the url below to sign in (c to copy)\n\n"
                 + link(url[:-1]) + b"\n" + link("M") + b"\n\n Paste code here if prompted >\n")
        expected = (b" Browser didn't open? Use the url below to sign in (c to copy)\n\n"
                    + link(url[:-1]) + link("M") + b"\n\n Paste code here if prompted >\n")
        unwrapper = install.UrlUnwrapper()
        self.assertEqual(unwrapper.feed(frame) + unwrapper.flush(), expected)
        # The same frame arriving in arbitrary chunks gives the same display.
        for size in (1, 7, 64):
            unwrapper = install.UrlUnwrapper()
            shown = b"".join(unwrapper.feed(frame[i:i + size]) for i in range(0, len(frame), size)) + unwrapper.flush()
            self.assertEqual(shown, expected, size)
        # Ordinary lines after a URL stay separate; an incomplete line waits for the idle flush.
        unwrapper = install.UrlUnwrapper()
        self.assertEqual(unwrapper.feed(b"see https://example.com/a\nnext step\nPaste code here >"),
                         b"see https://example.com/a\nnext step\n")
        self.assertTrue(unwrapper.waiting)
        self.assertEqual(unwrapper.flush(), b"Paste code here >")
        self.assertFalse(unwrapper.waiting)
        self.assertEqual(unwrapper.feed(b"https://example.com/b\n"), b"")
        self.assertEqual(unwrapper.flush(), b"https://example.com/b\n")

    def test_run_on_terminal_relays_input_and_captures_output(self):
        # Like Claude Code: keys arrive from a terminal, screens go to a pipe.
        tool = self.fake_cli("interactive-tool", textwrap.dedent("""\
            import os, signal, sys, termios, tty
            signal.signal(signal.SIGINT, lambda *_: sys.exit(7))
            assert sys.stdin.isatty() and not sys.stdout.isatty()
            assert os.open("/dev/tty", os.O_RDWR) > 2
            print("Paste code here:", end="", flush=True)  # no newline: relies on the idle flush
            tty.setraw(0)
            code = b""
            while not code.endswith(b"\\r"):
                code += os.read(0, 1)
            code = code.strip().decode()
            print(" " + code, flush=True)
            print("\\x1b[33msk-ant-oat01-" + code + "\\x1b[39m\\n\\nsize=" + repr(os.get_terminal_size(0)), file=sys.stderr, flush=True)
            raise SystemExit(3 if code == "fail" else 0)
        """))
        for code, expected_status in (("abc-123", 0), ("fail", 3), ("\x03", 7)):
            with self.subTest(code=code):
                user_master, user_tty = os.openpty()
                self.addCleanup(os.close, user_master)
                shown = []

                modes = []

                def act_as_user():
                    buffered = b""
                    while b"Paste code here:" not in buffered:
                        buffered += os.read(user_master, 4096)
                    # While relaying: no echo or line buffering, but output processing stays on.
                    attributes = install.termios.tcgetattr(user_tty)
                    modes.append((attributes[3] & install.termios.ECHO, attributes[3] & install.termios.ICANON,
                                  attributes[1] & install.termios.OPOST))
                    # Enter as a raw-mode terminal sends it; Ctrl-C alone must end the tool by signal.
                    os.write(user_master, (code + ("" if code == "\x03" else "\r")).encode())
                    while code != "\x03":
                        try:
                            chunk = os.read(user_master, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        buffered += chunk
                        if b"size=" in buffered and buffered.rstrip().endswith(b")"):
                            break
                    shown.append(buffered)

                user = threading.Thread(target=act_as_user)
                user.start()
                status, captured = install.run_on_terminal(
                    [str(tool)], {"PATH": os.environ.get("PATH", ""), "HOME": str(self.base)},
                    stdin_fd=user_tty, stdout_fd=user_tty)
                user.join(timeout=10)
                self.assertFalse(user.is_alive())
                # The raw mode used for relaying keystrokes was restored afterwards.
                self.assertTrue(install.termios.tcgetattr(user_tty)[3] & install.termios.ECHO)
                os.close(user_tty)
                self.assertEqual(status, expected_status)
                self.assertIn(b"Paste code here:", captured)
                self.assertEqual(modes, [(0, 0, install.termios.OPOST)])
                if code == "\x03":
                    # Raw-mode keys generate no signal; the runner delivers Ctrl-C itself.
                    self.assertEqual(install.printed_oauth_tokens(captured), set())
                    continue
                self.assertEqual(install.printed_oauth_tokens(captured), {"sk-ant-oat01-" + code})
                self.assertIn(b"size=os.terminal_size(columns=", captured)
                # The user's terminal saw everything the tool printed, in order,
                # with newlines carrying a carriage return.
                self.assertIn(f"Paste code here: {code}\r\n".encode(), shown[0])
                self.assertLess(shown[0].index(b"Paste code here:"), shown[0].index(("sk-ant-oat01-" + code).encode()))

    def test_output_tones_apply_only_on_a_color_terminal(self):
        with patch.object(install.sys.stderr, "isatty", return_value=True), patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertEqual(install.paint("Installed", "ok"), "\x1b[32mInstalled\x1b[0m")
            self.assertEqual(install.paint("plain", None), "plain")
            with patch.object(install, "emit") as emit:
                install.step("Paseo")
                install.say("Run: claude-codex")
                self.assertEqual([c.args[0] for c in emit.call_args_list], ["", "\x1b[1;36m▸ Paseo\x1b[0m", "Run: claude-codex"])
            with patch.dict(os.environ, {"NO_COLOR": "1"}):
                self.assertEqual(install.paint("Installed", "ok"), "Installed")
            with patch.dict(os.environ, {"TERM": "dumb"}):
                self.assertEqual(install.paint("Installed", "ok"), "Installed")
        with patch.object(install.sys.stderr, "isatty", return_value=False):
            self.assertEqual(install.paint("Installed", "ok"), "Installed")
            with patch.object(install, "emit") as emit:
                install.step("Paseo")
                self.assertEqual([c.args[0] for c in emit.call_args_list], ["", "▸ Paseo"])

    def test_paseo_install_command_follows_platform(self):
        def which(available):
            return lambda name: available.get(name)
        npm_command = ["/usr/bin/npm", "install", "-g", "@getpaseo/cli@latest"]
        with patch.object(install.sys, "platform", "darwin"):
            with patch.object(install.shutil, "which", side_effect=which({"brew": "/opt/homebrew/bin/brew", "npm": "/usr/bin/npm"})):
                self.assertEqual(install.paseo_install_command(), ["/opt/homebrew/bin/brew", "install", "--cask", "paseo"])
            with patch.object(install.shutil, "which", side_effect=which({"npm": "/usr/bin/npm"})):
                self.assertEqual(install.paseo_install_command(), npm_command)
        with patch.object(install.sys, "platform", "linux"):
            # Linuxbrew is not Paseo's documented Linux install; npm is.
            with patch.object(install.shutil, "which", side_effect=which({"brew": "/home/linuxbrew/.linuxbrew/bin/brew", "npm": "/usr/bin/npm"})):
                self.assertEqual(install.paseo_install_command(), npm_command)
            with patch.object(install.shutil, "which", return_value=None):
                self.assertIsNone(install.paseo_install_command())

    def test_install_paseo_runs_the_installer_and_locates_the_executable(self):
        prefix = self.base / "npm-global"
        installed = prefix / "bin" / "paseo"
        installed.parent.mkdir(parents=True)
        installed.write_text("#!/bin/sh\n")
        installed.chmod(0o755)
        npm_command = ["/usr/bin/npm", "install", "-g", "@getpaseo/cli@latest"]
        brew_command = ["/opt/homebrew/bin/brew", "install", "--cask", "paseo"]

        def fake_run(command, install_status=0, **kwargs):
            if command[1:] == ["prefix", "-g"]:
                return subprocess.CompletedProcess(command, 0, stdout=f"{prefix}\n", stderr="")
            self.assertNotIn("capture_output", kwargs)  # the installer's output stays on the terminal
            return subprocess.CompletedProcess(command, install_status)

        with patch.object(install.subprocess, "run", side_effect=fake_run) as run, \
             patch.object(install.shutil, "which", return_value=str(installed)), \
             patch.object(install, "say") as say:
            self.assertEqual(install.install_paseo(npm_command), str(installed))
            self.assertEqual(run.call_args_list[0].args[0], npm_command)
            self.assertTrue(any(message.startswith("Installing Paseo: /usr/bin/npm install -g @getpaseo/cli@latest")
                                for message in (call.args[0] for call in say.call_args_list)))
        # A global npm bin directory that is not on PATH yet is still found.
        with patch.object(install.subprocess, "run", side_effect=fake_run), \
             patch.object(install.shutil, "which", return_value=None), \
             patch.object(install, "say") as say:
            self.assertEqual(install.install_paseo(npm_command), str(installed))
            self.assertTrue(any("is not on PATH" in call.args[0] for call in say.call_args_list))
            with self.assertRaisesRegex(runtime.SetupError, "no paseo executable was found"):
                install.install_paseo(brew_command)
        # Failures explain the npm prefix fix only for npm.
        with patch.object(install.subprocess, "run", side_effect=lambda command, **kw: fake_run(command, 1)), \
             patch.object(install, "say"):
            with self.assertRaisesRegex(runtime.SetupError, "npm config set prefix") as raised:
                install.install_paseo(npm_command)
            self.assertIn("--skip-paseo", str(raised.exception))
            with self.assertRaisesRegex(runtime.SetupError, "Paseo installation failed") as raised:
                install.install_paseo(brew_command)
            self.assertNotIn("npm config", str(raised.exception))

    def test_missing_paseo_is_installed_on_request(self):
        paseo = self.fake_paseo("paseo", "print('paseo')\n")
        args = self.staged_args("--skip-plane")
        command = ["/usr/bin/npm", "install", "-g", "@getpaseo/cli@latest"]
        settings_file = Path(self.settings["config_dir"]) / "settings.json"
        paseo_config = self.base / "paseo-home" / "config.json"
        with self.assertRaises(SystemExit):
            install.parser().parse_args([*args, "--install-paseo", "--skip-paseo"])
        with patch.object(install, "detect_paseo", return_value=None), \
             patch.object(install, "paseo_install_command", return_value=command):
            # Staged and noninteractive runs do not install; they say how to.
            with patch.object(install, "install_paseo") as installer, patch.object(install, "ask") as prompt, \
                 patch.object(install, "say") as say:
                install.install(install.parser().parse_args(args))
                installer.assert_not_called()
                prompt.assert_not_called()
                self.assertIn("Paseo not detected (pass --install-paseo to install it); skipping Paseo configuration",
                              [call.args[0] for call in say.call_args_list])
            self.assertFalse(paseo_config.exists())
            # Interactive runs ask; skip leaves Paseo out, Enter installs and configures it.
            for answer, expect_install in (("skip", False), ("", True)):
                with self.subTest(answer=answer), \
                     patch.object(runtime.Runtime, "has_login", return_value=True), \
                     patch.object(runtime.Runtime, "doctor"), \
                     patch.object(install.sys.stdin, "isatty", return_value=True), \
                     patch.object(install, "ask", return_value=answer) as prompt, \
                     patch.object(install, "masked_input", return_value="skip"), \
                     patch.object(install, "install_paseo", return_value=str(paseo)) as installer, \
                     patch.object(install, "say") as say:
                    opts = install.parser().parse_args(args)
                    opts.skip_login = False
                    install.install(opts)
                    prompt.assert_called_once_with("Install Paseo now? (Enter for yes; type skip to skip Paseo): ")
                    self.assertTrue(any(shlex.join(command) in call.args[0] for call in say.call_args_list))
                    self.assertEqual(installer.called, expect_install)
                    self.assertEqual(paseo_config.exists(), expect_install)
                    self.assertEqual(runtime.read_json(settings_file).get("paseo_bin"), str(paseo) if expect_install else None)
            self.assertIn("claude-codex", runtime.read_json(paseo_config)["agents"]["providers"])
            # --install-paseo installs without asking, even when staged.
            paseo_config.unlink()
            with patch.object(install, "install_paseo", return_value=str(paseo)) as installer, \
                 patch.object(install, "ask") as prompt:
                install.install(install.parser().parse_args([*args, "--install-paseo"]))
                installer.assert_called_once_with(command)
                prompt.assert_not_called()
            self.assertTrue(paseo_config.exists())
        # Without Homebrew or npm the reason is stated; --skip-paseo never installs.
        with patch.object(install, "detect_paseo", return_value=None), \
             patch.object(install, "paseo_install_command", return_value=None), \
             patch.object(install, "install_paseo") as installer, patch.object(install, "say") as say:
            install.install(install.parser().parse_args([*args, "--install-paseo"]))
            installer.assert_not_called()
            self.assertIn("Paseo not detected and neither Homebrew nor npm is available to install it; "
                          "skipping Paseo configuration", [call.args[0] for call in say.call_args_list])
        with patch.object(install, "paseo_install_command") as command_lookup, \
             patch.object(install, "install_paseo") as installer, patch.object(install, "say") as say:
            install.install(install.parser().parse_args([*args, "--skip-paseo"]))
            command_lookup.assert_not_called()
            installer.assert_not_called()
            self.assertIn("Paseo integration skipped; skipping Paseo configuration", [call.args[0] for call in say.call_args_list])

    def test_paseo_listen_update_changes_only_loopback_listeners(self):
        cases = (
            ({}, "0.0.0.0", "0.0.0.0:6767"),
            ({"daemon": {"listen": "127.0.0.1:7000"}}, "0.0.0.0", "0.0.0.0:7000"),
            ({"daemon": {"listen": "6767"}}, "0.0.0.0", "0.0.0.0:6767"),
            ({"daemon": {"listen": "localhost:6767"}}, "0.0.0.0:8000", "0.0.0.0:8000"),
            ({"daemon": {"listen": "[::1]:6767"}}, "[::]", "[::]:6767"),
            ({"daemon": {"listen": "100.101.102.103:6767"}}, "0.0.0.0", None),  # a deliberate address
            ({"daemon": {"listen": "/tmp/paseo.sock"}}, "0.0.0.0", None),  # a socket
            ({}, "keep", None),
            ({}, "127.0.0.1", None),
        )
        for config, requested, expected in cases:
            with self.subTest(config=config, requested=requested):
                self.assertEqual(install.paseo_listen_update(config, requested), expected)
        for bad in ("/tmp/paseo.sock", "0.0.0.0:port"):
            with self.assertRaisesRegex(runtime.SetupError, "--paseo-listen"):
                install.paseo_listen_update({}, bad)

    def test_paseo_network_access_opens_loopback_listeners(self):
        config_file = self.base / "paseo-home" / "config.json"
        config_file.parent.mkdir()
        opts = install.parser().parse_args(self.staged_args())

        def run(listen="0.0.0.0", **daemon):
            runtime.write_json(config_file, {"version": 1, "daemon": {"listen": "127.0.0.1:6767", **daemon}, "agents": {}})
            opts.paseo_listen = listen
            with patch.object(install, "say") as say:
                install.configure_paseo_network(config_file, opts)
            return runtime.read_json(config_file)["daemon"], [call.args[0] for call in say.call_args_list]

        daemon, messages = run(relay={"enabled": False})
        self.assertEqual(daemon, {"listen": "0.0.0.0:6767", "relay": {"enabled": False}})
        self.assertTrue(any(message.startswith("Paseo daemon listens on 0.0.0.0:6767") for message in messages))
        self.assertFalse(any("password" in message for message in messages))
        self.assertTrue(list(config_file.parent.glob("config.json.claude-codex-backup-*")))
        daemon, messages = run(listen="keep")
        self.assertEqual(daemon, {"listen": "127.0.0.1:6767"})
        self.assertIn("Paseo daemon listen address unchanged: 127.0.0.1:6767", messages)

    def test_install_configures_paseo_network_before_restarting(self):
        paseo = self.fake_paseo("paseo", "print('paseo')\n")
        paseo_config = self.base / "paseo-home" / "config.json"
        paseo_config.parent.mkdir()
        runtime.write_json(paseo_config, {"version": 1, "daemon": {"listen": "127.0.0.1:6767"}})
        opts = install.parser().parse_args(self.staged_args("--paseo-bin", str(paseo)))
        with patch.object(install, "say") as say:
            install.install(opts)
        messages = [call.args[0] for call in say.call_args_list]
        self.assertEqual(runtime.read_json(paseo_config)["daemon"]["listen"], "0.0.0.0:6767")
        self.assertIn("claude-codex", runtime.read_json(paseo_config)["agents"]["providers"])
        self.assertTrue(any(message.startswith("Paseo daemon not restarted (--skip-paseo-start)") for message in messages))
        # The restart happens after the configuration is written.
        opts.skip_paseo_start = False
        seen = []
        def restart(command, **kwargs):
            seen.append((command[1:], runtime.read_json(paseo_config)["daemon"]["listen"]))
            return subprocess.CompletedProcess(command, 0)
        with patch.object(install.subprocess, "run", side_effect=restart), patch.object(install, "say"):
            install.install(opts)
        self.assertEqual(seen, [(["daemon", "restart"], "0.0.0.0:6767"), (["reload"], "0.0.0.0:6767")])
        # --paseo-listen keep leaves an existing listener alone.
        runtime.write_json(paseo_config, {"version": 1, "daemon": {"listen": "127.0.0.1:6767"}})
        install.install(install.parser().parse_args(self.staged_args("--paseo-bin", str(paseo), "--paseo-listen", "keep")))
        self.assertEqual(runtime.read_json(paseo_config)["daemon"]["listen"], "127.0.0.1:6767")

    def test_codex_sign_in_can_be_declined_interactively_or_by_flag(self):
        paseo = self.fake_paseo("paseo", "print('paseo')\n")
        args = self.staged_args("--paseo-bin", str(paseo), "--skip-plane")
        settings_file = Path(self.settings["config_dir"]) / "settings.json"
        cases = (("skip", None, False), ("", None, True), ("SKIP", None, False), ("anything", "--skip-codex-login", False))
        for answer, flag, expect_login in cases:
            with self.subTest(answer=answer, flag=flag):
                opts = install.parser().parse_args([*args, flag] if flag else args)
                opts.skip_login = False
                with patch.object(runtime.Runtime, "has_login", return_value=False), \
                     patch.object(runtime.Runtime, "login") as login, \
                     patch.object(runtime.Runtime, "doctor") as doctor, \
                     patch.object(install.sys.stdin, "isatty", return_value=True), \
                     patch.object(install, "ask", return_value=answer) as prompt, \
                     patch.object(install, "masked_input", return_value="sk-ant-oat01-pasted"), \
                     patch.object(install, "say") as say:
                    install.install(opts)
                messages = [call.args[0] for call in say.call_args_list]
                if flag:
                    prompt.assert_not_called()
                else:
                    prompt.assert_called_once_with("Sign in with ChatGPT for Claude Codex now? (Enter for yes; type skip to skip): ")
                self.assertEqual(login.called, expect_login)
                self.assertEqual(doctor.called, expect_login)
                self.assertEqual(any(message.startswith("Claude Codex sign-in skipped") for message in messages), not expect_login)
                # Declining Claude Codex does not skip the second account's step.
                self.assertEqual(runtime.read_json(settings_file)["peppy_oauth_token"], "sk-ant-oat01-pasted")
                saved = runtime.read_json(settings_file)
                del saved["peppy_oauth_token"]
                runtime.write_json(settings_file, saved)
        # An existing login is verified without asking; noninteractive runs sign in as before.
        for interactive in (True, False):
            with patch.object(runtime.Runtime, "has_login", return_value=interactive), \
                 patch.object(runtime.Runtime, "login") as login, \
                 patch.object(runtime.Runtime, "doctor") as doctor, \
                 patch.object(install.sys.stdin, "isatty", return_value=interactive), \
                 patch.object(install, "ask") as prompt, \
                 patch.object(install, "masked_input", return_value="skip"), \
                 patch.object(install, "say"):
                opts = install.parser().parse_args(args)
                opts.skip_login = False
                install.install(opts)
                prompt.assert_not_called()
                self.assertEqual(login.called, not interactive)
                doctor.assert_called_once()
        # --skip-login stages everything and never asks.
        with patch.object(install, "ask") as prompt, patch.object(runtime.Runtime, "login") as login:
            install.install(install.parser().parse_args(args))
            prompt.assert_not_called()
            login.assert_not_called()

    def test_interactive_install_signs_in_the_second_account_for_paseo(self):
        paseo = self.fake_paseo("paseo", "print('paseo')\n")
        opts = install.parser().parse_args(self.staged_args("--paseo-bin", str(paseo), "--skip-plane"))
        opts.skip_login = False
        settings_file = Path(self.settings["config_dir"]) / "settings.json"
        peppy_dir = str(self.base / "claude-peppy")
        claude_bin = opts.claude_bin
        frame = "Your OAuth token:\r\n\r\n  \x1b[33msk-ant-oat01-fresh\x1b[39m\r\n\r\n  Store this token securely.\r\n"

        def fake_setup_token(argv, env):
            self.assertEqual(argv, [claude_bin, "setup-token"])
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], peppy_dir)
            for name in ("CLAUDE_PEPPY_PASEO", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
                self.assertNotIn(name, env)
            return 0, frame.encode()

        ambient = {"CLAUDE_PEPPY_PASEO": "1", "CLAUDE_CODE_OAUTH_TOKEN": "ambient-token", "ANTHROPIC_API_KEY": "key"}
        with patch.dict(os.environ, ambient), \
             patch.object(runtime.Runtime, "has_login", return_value=True), \
             patch.object(runtime.Runtime, "doctor"), \
             patch.object(install.sys.stdin, "isatty", return_value=True), \
             patch.object(install, "masked_input", return_value=""), \
             patch.object(install, "run_on_terminal", side_effect=fake_setup_token) as setup_token, \
             patch.object(install, "say") as say:
            install.install(opts)
            setup_token.assert_called_once()
            messages = [call.args[0] for call in say.call_args_list]
            self.assertTrue(any("Saved the second account's token" in message for message in messages))
            self.assertFalse(any("sk-ant-oat01-fresh" in message for message in messages))
            self.assertFalse(any("No token is saved" in message for message in messages))
            self.assertEqual(runtime.read_json(settings_file)["peppy_oauth_token"], "sk-ant-oat01-fresh")
            self.assertEqual(settings_file.stat().st_mode & 0o777, 0o600)
            # A rerun keeps the saved token and does not ask again.
            install.install(install.parser().parse_args(self.staged_args("--paseo-bin", str(paseo), "--skip-plane")))
            setup_token.assert_called_once()
            # A skipped or failed sign-in completes the installation and prints the fallback.
            for outcome in ("skip", "failed"):
                with self.subTest(outcome=outcome):
                    saved = runtime.read_json(settings_file)
                    saved.pop("peppy_oauth_token", None)
                    runtime.write_json(settings_file, saved)
                    setup_token.reset_mock()
                    say.reset_mock()
                    install.masked_input.return_value = "skip" if outcome == "skip" else ""
                    setup_token.side_effect = None
                    setup_token.return_value = (1, b"Login cancelled\r\n")
                    install.install(opts)
                    self.assertEqual(setup_token.call_count, 0 if outcome == "skip" else 1)
                    self.assertNotIn("peppy_oauth_token", runtime.read_json(settings_file))
                    messages = [call.args[0] for call in say.call_args_list]
                    self.assertTrue(any("No token is saved" in message for message in messages))
                    self.assertTrue(any("claude-peppy setup-token" in message for message in messages))
        self.assertNotIn("sk-ant-oat01", (self.base / "paseo-home" / "config.json").read_text())
        # Staged and noninteractive runs never start a sign-in.
        for interactive in (True, False):
            with patch.object(install.sys.stdin, "isatty", return_value=interactive), \
                 patch.object(install, "run_on_terminal") as setup_token, \
                 patch.object(install, "masked_input") as prompt:
                install.install(install.parser().parse_args(self.staged_args("--paseo-bin", str(paseo), "--skip-plane")))
                setup_token.assert_not_called()
                prompt.assert_not_called()

    def test_peppy_token_is_saved_privately_retained_and_kept_from_paseo(self):
        paseo = self.fake_paseo("paseo", "print('paseo')\n")
        args = self.staged_args("--paseo-bin", str(paseo))
        settings_file = Path(self.settings["config_dir"]) / "settings.json"
        paseo_config = self.base / "paseo-home" / "config.json"
        with patch.dict(os.environ, {"CLAUDE_PEPPY_OAUTH_TOKEN": "sk-ant-oat01-env-token"}):
            install.install(install.parser().parse_args(args))
            self.assertNotIn("CLAUDE_PEPPY_OAUTH_TOKEN", os.environ)
        self.assertEqual(runtime.read_json(settings_file)["peppy_oauth_token"], "sk-ant-oat01-env-token")
        self.assertEqual(settings_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("sk-ant-oat01", paseo_config.read_text())
        self.assertEqual(runtime.read_json(paseo_config)["agents"]["providers"]["claude-peppy"]["env"],
                         {"CLAUDE_PEPPY_PASEO": "1"})
        # Reruns keep the saved token; a file replaces it; --skip-peppy retains it.
        install.install(install.parser().parse_args(args))
        self.assertEqual(runtime.read_json(settings_file)["peppy_oauth_token"], "sk-ant-oat01-env-token")
        token_file = self.base / "rotated.txt"
        token_file.write_text("sk-ant-oat01-rotated")
        install.install(install.parser().parse_args([*args, "--peppy-oauth-token-file", str(token_file)]))
        self.assertEqual(runtime.read_json(settings_file)["peppy_oauth_token"], "sk-ant-oat01-rotated")
        skip_args = [*args, "--skip-peppy"]
        del skip_args[skip_args.index("--peppy-config-dir"):skip_args.index("--peppy-config-dir") + 2]
        install.install(install.parser().parse_args(skip_args))
        self.assertEqual(runtime.read_json(settings_file)["peppy_oauth_token"], "sk-ant-oat01-rotated")
        # The daemon is restarted without the input token in its environment.
        opts = install.parser().parse_args(args)
        opts.skip_paseo_start = False

        def run(command, **kwargs):
            self.assertNotIn("CLAUDE_PEPPY_OAUTH_TOKEN", os.environ)
            self.assertNotIn("CLAUDE_PEPPY_OAUTH_TOKEN", kwargs.get("env", {}))
            return subprocess.CompletedProcess(command, 0)
        with patch.dict(os.environ, {"CLAUDE_PEPPY_OAUTH_TOKEN": "sk-ant-oat01-not-for-daemon"}), \
             patch.object(install.subprocess, "run", side_effect=run) as spawn:
            install.install(opts)
        self.assertEqual([call.args[0] for call in spawn.call_args_list],
                         [[str(paseo), "daemon", "restart"], [str(paseo), "reload"]])

    def test_peppy_launcher_under_paseo_uses_daemon_profile_and_saved_token(self):
        claude = self.fake_cli("claude-reporting", "import json,os,sys\n"
                               "print(json.dumps({'args':sys.argv[1:],'config':os.environ.get('CLAUDE_CONFIG_DIR'),"
                               "'token':os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'),'marker':os.environ.get('CLAUDE_PEPPY_PASEO')}))\n")
        token_file = self.base / "token.txt"
        token_file.write_text("sk-ant-oat01-paseo\n")
        install.install(install.parser().parse_args(self.staged_args(
            "--skip-paseo", "--claude-bin", str(claude), "--peppy-oauth-token-file", str(token_file))))
        wrapper = Path(self.settings["bin_dir"]) / "claude-peppy"
        # The test shell may itself run inside a Claude profile; start from a clean one.
        ambient = {name: value for name, value in os.environ.items() if name != "CLAUDE_CONFIG_DIR"}
        ambient.update({"CLAUDE_CODE_OAUTH_TOKEN": "ambient-token", "ANTHROPIC_API_KEY": "ambient-key"})
        # A provider entry from an older installer still pins the peppy profile;
        # Paseo launches drop it and select the account by the saved token.
        for pinned in (None, str(self.base / "claude-peppy")):
            with self.subTest(pinned=pinned):
                env = {**ambient, "CLAUDE_PEPPY_PASEO": "1", **({"CLAUDE_CONFIG_DIR": pinned} if pinned else {})}
                result = subprocess.run([str(wrapper), "-p", "hi"], check=True, capture_output=True, text=True, env=env)
                self.assertEqual(json.loads(result.stdout),
                                 {"args": ["-p", "hi"], "config": None, "token": "sk-ant-oat01-paseo", "marker": "1"})
        daemon = subprocess.run([str(wrapper), "--version"], check=True, capture_output=True, text=True,
                                env={**ambient, "CLAUDE_PEPPY_PASEO": "1", "CLAUDE_CONFIG_DIR": "/daemon/claude"})
        self.assertEqual(json.loads(daemon.stdout)["config"], "/daemon/claude")
        terminal = subprocess.run([str(wrapper), "--version"], check=True, capture_output=True, text=True, env=ambient)
        self.assertEqual(json.loads(terminal.stdout), {"args": ["--version"], "config": str(self.base / "claude-peppy"),
                                                       "token": None, "marker": None})
        # Without a saved token a Paseo launch fails instead of using the daemon's own login.
        settings_file = Path(self.settings["config_dir"]) / "settings.json"
        saved = runtime.read_json(settings_file)
        del saved["peppy_oauth_token"]
        runtime.write_json(settings_file, saved)
        failed = subprocess.run([str(wrapper), "-p", "hi"], capture_output=True, text=True,
                                env={**ambient, "CLAUDE_PEPPY_PASEO": "1"})
        self.assertEqual(failed.returncode, 1)
        self.assertIn("claude-peppy setup-token", failed.stderr)
        self.assertEqual(failed.stdout, "")

    def test_launcher_refuses_to_replace_unrelated_program(self):
        target = self.base / "claude-codex"
        target.write_text("existing program")
        with self.assertRaises(runtime.SetupError):
            install.write_launcher(target, ["false"])
        self.assertEqual(target.read_text(), "existing program")

    def test_peppy_launcher_runs_shared_cli_in_its_own_profile(self):
        claude = self.fake_cli("claude-reporting", "import json,os,sys\n"
                               "print(json.dumps({'args':sys.argv[1:],'config':os.environ.get('CLAUDE_CONFIG_DIR'),"
                               "'base':os.environ.get('ANTHROPIC_BASE_URL'),'key':os.environ.get('ANTHROPIC_API_KEY')}))\n")
        install.install(install.parser().parse_args(self.staged_args("--skip-paseo", "--claude-bin", str(claude))))
        settings = runtime.read_json(Path(self.settings["config_dir"]) / "settings.json")
        wrapper = Path(self.settings["bin_dir"]) / "claude-peppy"
        environment = {**os.environ, "ANTHROPIC_BASE_URL": "http://ambush.example",
                       "ANTHROPIC_API_KEY": "ambient-key", "ANTHROPIC_MODEL": "ambient-model",
                       "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1050000"}
        result = subprocess.run([str(wrapper), "-p", "hello", "world"], check=True,
                                capture_output=True, text=True, env=environment)
        self.assertEqual(json.loads(result.stdout), {
            "args": ["-p", "hello", "world"],
            "config": str(self.base / "claude-peppy"),
            "base": None,
            "key": None,
        })
        self.assertNotIn("ambient-model", result.stdout)
        with patch.object(runtime.os, "execve") as execute:
            runtime.launch_peppy(settings, ["--version"])
        binary, forwarded, env = execute.call_args.args
        self.assertEqual((binary, forwarded), (settings["claude_bin"], [settings["claude_bin"], "--version"]))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.base / "claude-peppy"))

    def test_peppy_profile_directory_and_settings_round_trip(self):
        custom = self.base / "second-profile"
        install.install(install.parser().parse_args(self.staged_args("--skip-paseo", "--peppy-config-dir", str(custom))))
        settings_file = Path(self.settings["config_dir"]) / "settings.json"
        self.assertEqual(runtime.read_json(settings_file)["peppy_config_dir"], str(custom))
        # A plain rerun without the flag keeps the previously chosen directory.
        rerun = self.staged_args("--skip-paseo")
        del rerun[rerun.index("--peppy-config-dir"):rerun.index("--peppy-config-dir") + 2]
        install.install(install.parser().parse_args(rerun))
        self.assertEqual(runtime.read_json(settings_file)["peppy_config_dir"], str(custom))
        self.assertEqual(custom.stat().st_mode & 0o777, 0o700)
        # A user-prepared profile keeps its own permissions.
        prepared = self.base / "prepared-profile"
        prepared.mkdir()
        prepared.chmod(0o755)  # mkdir's mode argument is masked by the shell umask.
        install.install(install.parser().parse_args(self.staged_args("--skip-paseo", "--peppy-config-dir", str(prepared))))
        self.assertEqual(prepared.stat().st_mode & 0o777, 0o755)

    def test_peppy_profile_must_differ_from_primary_claude_profile(self):
        for forbidden in (Path.home() / ".claude", Path(self.settings["config_dir"]) / "claude"):
            with self.subTest(profile=forbidden):
                args = self.staged_args("--skip-paseo", "--peppy-config-dir", str(forbidden))
                with patch.object(install, "atomic_write") as write:
                    with self.assertRaisesRegex(runtime.SetupError, "must not be the primary Claude profile"):
                        install.install(install.parser().parse_args(args))
                write.assert_not_called()

    def test_peppy_directory_change_requires_updating_paseo_provider(self):
        paseo = self.fake_paseo("paseo", "print('paseo')\n")
        install.install(install.parser().parse_args(self.staged_args("--paseo-bin", str(paseo))))
        config_file = self.base / "paseo-home" / "config.json"
        self.assertIn("claude-peppy", runtime.read_json(config_file)["agents"]["providers"])
        # Without Paseo in this run the retained provider entry would keep
        # pinning the old profile while the launcher moves to the new one.
        moved = self.staged_args("--skip-paseo", "--peppy-config-dir", str(self.base / "moved-peppy"))
        with patch.object(install, "atomic_write") as write:
            with self.assertRaisesRegex(runtime.SetupError, "claude-peppy provider entry"):
                install.install(install.parser().parse_args(moved))
        write.assert_not_called()

    def test_skip_peppy_writes_no_launcher_or_entry_and_retains_previous(self):
        install.install(install.parser().parse_args(self.staged_args("--skip-paseo")))
        settings_file = Path(self.settings["config_dir"]) / "settings.json"
        bin_dir = Path(self.settings["bin_dir"])
        launcher = bin_dir / "claude-peppy"
        installed = launcher.read_bytes()
        skip_args = self.staged_args("--skip-paseo")
        del skip_args[skip_args.index("--peppy-config-dir"):skip_args.index("--peppy-config-dir") + 2]
        skip_args.append("--skip-peppy")
        # An explicit directory together with --skip-peppy is contradictory.
        with self.assertRaises(SystemExit):
            install.parser().parse_args([*skip_args, "--peppy-config-dir", str(self.base / "other")])
        with patch.dict(os.environ, {"HOME": str(self.base / "isolated-home")}):
            install.install(install.parser().parse_args(skip_args))
        # Skipping preserves the existing launcher and the saved directory it reads.
        self.assertEqual(launcher.read_bytes(), installed)
        self.assertTrue((bin_dir / "claude-codex").exists())
        self.assertEqual(runtime.read_json(settings_file)["peppy_config_dir"], str(self.base / "claude-peppy"))
        with patch.dict(os.environ, {"HOME": str(self.base / "isolated-home")}):
            fresh = ["--config-dir", str(self.base / "fresh-config"), "--data-dir", str(self.base / "fresh-data"),
                     "--state-dir", str(self.base / "fresh-state"), "--bin-dir", str(self.base / "fresh-bin"),
                     "--skip-login", "--skip-paseo", "--no-path", "--skip-peppy"]
            claude = self.fake_cli("claude-original", "print('Claude Code')\n")
            fresh += ["--claude-bin", str(claude), "--proxy-binary", str(self.fake_cli("p", "raise SystemExit(1)\n"))]
            install.install(install.parser().parse_args(fresh))
        self.assertNotIn("peppy_config_dir", runtime.read_json(self.base / "fresh-config" / "settings.json"))
        self.assertFalse((self.base / "isolated-home" / ".claude-peppy").exists())

    def test_claude_bin_pointing_at_peppy_wrapper_is_rejected(self):
        wrapper = self.base / "bin" / "claude-peppy"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(install.MARKER + "\n")
        wrapper.chmod(0o755)
        args = self.staged_args("--skip-paseo", "--claude-bin", str(wrapper))
        with patch.object(install, "atomic_write") as write:
            with self.assertRaisesRegex(runtime.SetupError, "--claude-bin must point to the original Claude executable"):
                install.install(install.parser().parse_args(args))
        write.assert_not_called()
        # An unrelated program named claude-peppy is never overwritten.
        wrapper.write_text("someone else's program")
        with self.assertRaisesRegex(runtime.SetupError, "Refusing to overwrite unrelated program"):
            install.install(install.parser().parse_args(self.staged_args("--skip-paseo")))


if __name__ == "__main__":
    unittest.main()

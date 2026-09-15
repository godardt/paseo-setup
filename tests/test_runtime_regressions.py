"""Offline regressions for argument boundaries, login verification, and locks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import claude_codex as runtime


class ArgumentBoundaryTests(unittest.TestCase):
    def test_required_operands_are_not_wrapper_options(self):
        for option in ("--append-system-prompt", "--system-prompt", "--settings",
                       "--system-prompt-file", "--append-system-prompt-file",
                       "--permission-prompt-tool", "--json-schema", "--name", "-n"):
            for value in ("--model=gpt-6-astra(max)", "--reasoning=low", "--effort=max", ""):
                with self.subTest(option=option, value=value):
                    original = [option, value, "-p", "Hi", "--reasoning", "xhigh"]
                    args, effort = runtime.parse_launch_args(original, "high")
                    self.assertEqual(effort, "xhigh")
                    self.assertEqual(args, ["--model", "gpt-6-astra(xhigh)", *original[:-2]])

    def test_required_operand_can_be_a_literal_separator(self):
        original = ["--append-system-prompt", "--", "--reasoning=low", "-p", "Hi"]
        args, effort = runtime.parse_launch_args(original, "high")
        self.assertEqual(effort, "low")
        self.assertEqual(args, ["--model", "gpt-6-astra(low)",
                                "--append-system-prompt", "--", "-p", "Hi"])

    def test_inline_operands_and_unknown_flags_are_forwarded(self):
        original = ["--append-system-prompt=--reasoning=low", "--settings={}",
                    "--future-sdk-flag", "value", "--future-sdk-flag=--model=literal",
                    "--model", "gpt-6-astra(max)", "-p", "Hi"]
        args, effort = runtime.parse_launch_args(original, "high")
        self.assertEqual(effort, "max")
        self.assertEqual(args, ["--model", "gpt-6-astra(max)", *original[:5], *original[7:]])

    def test_optional_operands_and_boolean_flags_do_not_hide_selectors(self):
        for option in ("--resume", "-r", "--debug", "-d", "--worktree", "-w"):
            for operand in ([], ["session-or-filter"], ["-"], [""]):
                with self.subTest(option=option, operand=operand):
                    original = [option, *operand, "--model=gpt-6-astra(max)", "-p", "Hi"]
                    args, effort = runtime.parse_launch_args(original, "high")
                    self.assertEqual(effort, "max")
                    self.assertEqual(args, ["--model", "gpt-6-astra(max)",
                                            option, *operand, "-p", "Hi"])
        args, effort = runtime.parse_launch_args(["-p", "--reasoning=low", "Hi"], "high")
        self.assertEqual((args, effort), (["--model", "gpt-6-astra(low)", "-p", "Hi"], "low"))

    def test_variadic_options_preserve_first_required_operand(self):
        for option in ("--mcp-config", "--tools", "--allowedTools", "--allowed-tools",
                       "--disallowedTools", "--disallowed-tools", "--add-dir", "--betas", "--file"):
            with self.subTest(option=option):
                original = [option, "--model=gpt-6-astra(max)", "another-value", "-",
                            "--reasoning=low", "-p", "Hi"]
                args, effort = runtime.parse_launch_args(original, "high")
                self.assertEqual(effort, "low")
                self.assertEqual(args, ["--model", "gpt-6-astra(low)", *original[:4], *original[5:]])
        args, effort = runtime.parse_launch_args(
            ["--tools=Read", "Edit", "--reasoning=low", "-p", "Hi"], "high")
        self.assertEqual((args, effort),
                         (["--model", "gpt-6-astra(low)", "--tools=Read", "Edit", "-p", "Hi"], "low"))

    def test_separator_preserves_literal_tail_after_forwarded_values(self):
        original = ["--append-system-prompt", "--model=literal", "--resume", "-p",
                    "--", "--reasoning=low", "--model=literal"]
        args, effort = runtime.parse_launch_args(original, "high")
        self.assertEqual(effort, "high")
        self.assertEqual(args, ["--model", "gpt-6-astra(high)", *original])

    def test_native_missing_operand_is_not_filled_by_normalized_effort(self):
        with self.assertRaisesRegex(runtime.SetupError, "--system-prompt needs a value"):
            runtime.parse_launch_args(["--reasoning=ultracode", "--system-prompt"], "high")


class PlaneMcpArgumentTests(unittest.TestCase):
    PATH = "/private/config with spaces/plane-mcp.json"
    MODEL_ARGS = ["--model", runtime.model_id("high")]

    def setUp(self):
        self.settings = {"plane_mcp_config": self.PATH}
        for target, name in ((runtime, "read_json"), (Path, "open")):
            reader = patch.object(target, name, side_effect=AssertionError("MCP files must stay opaque"))
            reader.start()
            self.addCleanup(reader.stop)

    def assert_applied(self, original, expected):
        # The helper's contract is normalized launch args, not raw positional prompts.
        args, _ = runtime.parse_launch_args(original, "high")
        before = list(args)
        result = runtime.apply_plane_mcp(args, self.settings)
        self.assertEqual(result, expected)
        self.assertEqual(args, before)
        self.assertEqual(runtime.apply_plane_mcp(result, self.settings), result)

    def test_unconfigured_is_unchanged(self):
        args = [*self.MODEL_ARGS, "--mcp-config", "caller.json", "-p", "Hi"]
        for settings in ({}, {"plane_mcp_config": None}, {"plane_mcp_config": ""}):
            with self.subTest(settings=settings):
                self.assertIs(runtime.apply_plane_mcp(args, settings), args)

    def test_new_group_is_bounded_by_normalized_model(self):
        for original in ([], ["Hello"], ["-p", "Hi"],
                         ["--", "--mcp-config", self.PATH, "--strict-mcp-config"]):
            with self.subTest(original=original):
                self.assert_applied(original, ["--mcp-config", self.PATH, *self.MODEL_ARGS, *original])

    def test_existing_groups_preserve_all_file_and_json_operands(self):
        inline = '{ "mcpServers": {"caller": {"command": "unchanged"}} }'
        for group in (["--mcp-config", "caller.json"],
                      ["--mcp-config", inline, "other config.json", "-"],
                      ["--mcp-config=caller.json"],
                      ["--mcp-config=" + inline, "other config.json", "-"],
                      ["--mcp-config=", "other.json"]):
            for after in ([], ["-p", "Hi"], ["--", "literal prompt"]):
                with self.subTest(group=group, after=after):
                    self.assert_applied([*group, *after], [*self.MODEL_ARGS, *group, self.PATH, *after])

    def test_append_to_last_group_without_repeating_the_flag(self):
        first = ["--mcp-config=first.json", "second.json", "--verbose"]
        for last in (["--mcp-config", "third.json", "fourth.json"],
                     ["--mcp-config=third.json", "fourth.json"]):
            with self.subTest(last=last):
                self.assert_applied([*first, *last, "-p", "Hi"],
                                    [*self.MODEL_ARGS, *first, *last, self.PATH, "-p", "Hi"])

    def test_exact_managed_operand_in_any_group_is_not_duplicated(self):
        for group in (["--mcp-config", self.PATH, "caller.json"],
                      ["--mcp-config", "caller.json", self.PATH],
                      ["--mcp-config=" + self.PATH, "caller.json"],
                      ["--mcp-config=caller.json", self.PATH]):
            for other in ([], ["--verbose", "--mcp-config", "last.json"]):
                with self.subTest(group=group, other=other):
                    original = [*group, *other, "-p", "Hi"]
                    self.assert_applied(original, [*self.MODEL_ARGS, *original])

    def test_only_exact_config_operands_count_as_already_managed(self):
        for original in (["--append-system-prompt", self.PATH], ["--tools", self.PATH],
                         ["-p", self.PATH], ["--", self.PATH]):
            with self.subTest(original=original):
                self.assert_applied(original, ["--mcp-config", self.PATH, *self.MODEL_ARGS, *original])
        for operand in (self.PATH + ".other", json.dumps({"path": self.PATH})):
            with self.subTest(operand=operand):
                original = ["--mcp-config", operand]
                self.assert_applied(original, [*self.MODEL_ARGS, *original, self.PATH])

    def test_genuine_strict_option_always_opts_out(self):
        for strict in ("--strict-mcp-config", "--strict-mcp-config=true", "--strict-mcp-config=false"):
            for original in ([strict, "-p", "Hi"],
                             ["--mcp-config", "caller.json", strict, "-p", "Hi"],
                             [strict, "--mcp-config=caller.json", "other.json"]):
                with self.subTest(original=original):
                    self.assert_applied(original, [*self.MODEL_ARGS, *original])

    def test_required_flag_looking_operands_are_not_options(self):
        for option in sorted(runtime.CLAUDE_VALUE_OPTIONS - {"--mcp-config"}):
            for operand in ("--strict-mcp-config", "--mcp-config", "--"):
                with self.subTest(option=option, operand=operand):
                    original = [option, operand, "--verbose", "-p", "Hi"]
                    self.assert_applied(original, ["--mcp-config", self.PATH, *self.MODEL_ARGS, *original])
        # Normalization moves native --effort to the end; its value is still opaque.
        self.assert_applied(["--effort", "--strict-mcp-config", "-p", "Hi"],
                            ["--mcp-config", self.PATH, *self.MODEL_ARGS,
                             "-p", "Hi", "--effort", "--strict-mcp-config"])

    def test_variadic_config_first_operand_can_look_like_a_flag_or_separator(self):
        for operand in ("--strict-mcp-config", "--mcp-config", "--", ""):
            for group in (["--mcp-config", operand, "another.json", "-"],
                          ["--mcp-config=" + operand, "another.json", "-"]):
                with self.subTest(group=group):
                    self.assert_applied([*group, "--", "--strict-mcp-config"],
                                        [*self.MODEL_ARGS, *group, self.PATH, "--", "--strict-mcp-config"])

    def test_required_separator_operand_does_not_hide_later_options(self):
        for option in sorted(runtime.CLAUDE_VALUE_OPTIONS):
            with self.subTest(option=option):
                original = [option, "--", "--mcp-config", "caller.json"]
                self.assert_applied(original, [*self.MODEL_ARGS, *original, self.PATH])
                self.assert_applied([*original, "--strict-mcp-config"],
                                    [*self.MODEL_ARGS, *original, "--strict-mcp-config"])

    def test_optional_and_variadic_boundaries_leave_real_flags_visible(self):
        for option in sorted(runtime.CLAUDE_OPTIONAL_VALUE_OPTIONS):
            for operand in ([], ["filter"], ["-"], [""]):
                with self.subTest(option=option, operand=operand):
                    original = [option, *operand, "--mcp-config", "caller.json"]
                    self.assert_applied(original, [*self.MODEL_ARGS, *original, self.PATH])
                    self.assert_applied([*original, "--strict-mcp-config"],
                                        [*self.MODEL_ARGS, *original, "--strict-mcp-config"])
            original = [option + "=--strict-mcp-config", "-p", "Hi"]
            self.assert_applied(original, ["--mcp-config", self.PATH, *self.MODEL_ARGS, *original])
        for option in sorted(runtime.CLAUDE_VARIADIC_OPTIONS - {"--mcp-config"}):
            with self.subTest(option=option):
                original = [option + "=first", "second", "-", "--mcp-config", "caller.json"]
                self.assert_applied(original, [*self.MODEL_ARGS, *original, self.PATH])
                self.assert_applied([*original, "--strict-mcp-config"],
                                    [*self.MODEL_ARGS, *original, "--strict-mcp-config"])


class PlaneMcpLaunchTests(unittest.TestCase):
    MODES = ("terminal", "paseo", "paseo-stream")

    def setUp(self):
        self.settings = {
            "config_dir": "/unused/config", "state_dir": "/unused/state", "port": 8317,
            "api_key": "fixture-proxy-key", "claude_bin": "/unused/claude", "reasoning": "high",
            "plane_mcp_config": PlaneMcpArgumentTests.PATH,
        }
        self.source = {"PATH": "/unused/bin", "HOME": "/unused/home",
                       "PLANE_API_KEY": "fixture-plane-token", "ANTHROPIC_API_KEY": "fixture-old-key",
                       "CLAUDE_CODE_EFFORT_LEVEL": "max", "CLAUDE_CONFIG_DIR": "/unused/daemon-profile"}
        for attribute, target, name, options in (
            ("factory", runtime, "Runtime", {"autospec": True}),
            ("execve", runtime.os, "execve", {"side_effect": SystemExit(0)}),
            ("stream", runtime, "run_paseo_stream", {"return_value": 0}),
            ("adopt", runtime, "adopt_isolated_transcript", {}),
            ("read_json", runtime, "read_json", {"side_effect": AssertionError("No credential reads")}),
            ("open", Path, "open", {"side_effect": AssertionError("No config reads")}),
        ):
            patcher = patch.object(target, name, **options)
            setattr(self, attribute, patcher.start())
            self.addCleanup(patcher.stop)
        self.factory.return_value.has_login.return_value = True

    def mode_args(self, args, mode):
        return ["--output-format", "stream-json", *args] if mode == "paseo-stream" else args

    def intercepted_launch(self, args, mode, settings=None, auto=False):
        for mocked in (self.factory, self.execve, self.stream, self.adopt):
            mocked.reset_mock()
        source = dict(self.source)
        if mode != "terminal":
            source["CLAUDE_CODEX_PASEO_USAGE"] = "1"
        if auto:
            source[runtime.AUTO_MODE_OVERRIDE] = "1"
        with patch.dict(os.environ, source, clear=True), self.assertRaises(SystemExit) as exited:
            runtime.launch(self.settings if settings is None else settings, self.mode_args(args, mode))
        self.assertEqual(exited.exception.code, 0)
        if mode == "paseo-stream":
            self.execve.assert_not_called()
            self.stream.assert_called_once()
            binary, forwarded, env = self.stream.call_args.args
        else:
            self.stream.assert_not_called()
            self.execve.assert_called_once()
            binary, argv, env = self.execve.call_args.args
            self.assertEqual(argv[0], binary)
            forwarded = argv[1:]
        self.assertEqual(binary, self.settings["claude_bin"])
        for name in ("PLANE_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_EFFORT_LEVEL"):
            self.assertNotIn(name, env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/unused/config/claude" if mode == "terminal"
                         else "/unused/daemon-profile")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], self.settings["api_key"])
        return forwarded, env

    def test_launch_preserves_models_settings_and_caller_mcp_configs(self):
        args = ["--model", runtime.ULTRACODE_MODEL, "--settings=" + json.dumps({"fastMode": True}),
                "--mcp-config", '{ "mcpServers": {} }', "caller config.json", "-p", "Hi"]
        for mode in self.MODES:
            for auto in (False, True):
                with self.subTest(mode=mode, auto=auto):
                    forwarded, env = self.intercepted_launch(args, mode, auto=auto)
                    expected, _ = runtime.parse_launch_args(self.mode_args(args, mode), "high")
                    if not auto:
                        expected = runtime.apply_launcher_settings(expected)
                    end = expected.index("caller config.json") + 1
                    expected.insert(end, self.settings["plane_mcp_config"])
                    self.assertEqual(forwarded, expected)
                    self.assertEqual(env["ANTHROPIC_MODEL"], runtime.ULTRACODE_MODEL)
                    self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], runtime.ULTRACODE_MODEL)
                    self.factory.assert_called_once_with(self.settings)
                    self.factory.return_value.has_login.assert_called_once_with()
                    self.factory.return_value.start.assert_called_once_with()
                    if mode == "terminal":
                        self.adopt.assert_not_called()
                    else:
                        self.adopt.assert_called_once_with(self.settings, forwarded, env)

    def test_launch_prepends_new_group_before_model_not_positional_prompt(self):
        for mode in self.MODES:
            for auto in (False, True):
                with self.subTest(mode=mode, auto=auto):
                    forwarded, _ = self.intercepted_launch(["Hello"], mode, auto=auto)
                    expected, _ = runtime.parse_launch_args(self.mode_args(["Hello"], mode), "high")
                    if not auto:
                        expected = runtime.apply_launcher_settings(expected)
                    self.assertEqual(forwarded, ["--mcp-config", self.settings["plane_mcp_config"], *expected])

    def test_injection_runs_after_settings_and_before_runtime(self):
        calls = Mock()
        with patch.object(runtime, "parse_launch_args", wraps=runtime.parse_launch_args) as parse, \
             patch.object(runtime, "apply_launcher_settings", wraps=runtime.apply_launcher_settings) as settings, \
             patch.object(runtime, "apply_plane_mcp", wraps=runtime.apply_plane_mcp) as plane:
            for name, mocked in (("parse", parse), ("settings", settings), ("plane", plane),
                                 ("Runtime", self.factory)):
                calls.attach_mock(mocked, name)
            self.intercepted_launch(["-p", "Hi"], "terminal")
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["parse", "settings", "plane", "Runtime", "Runtime().has_login", "Runtime().start"])

    def test_strict_and_unconfigured_launch_arguments_are_unchanged(self):
        cases = ((False, ["-p", "Hi"]),
                 (False, ["--mcp-config", "caller.json", "-p", "Hi"]),
                 (True, ["--strict-mcp-config", "-p", "Hi"]),
                 (True, ["--mcp-config=caller.json", "other.json", "--strict-mcp-config", "-p", "Hi"]))
        for mode in self.MODES:
            for configured, args in cases:
                with self.subTest(mode=mode, configured=configured, args=args):
                    settings = dict(self.settings)
                    if not configured:
                        del settings["plane_mcp_config"]
                    forwarded, _ = self.intercepted_launch(args, mode, settings)
                    expected, _ = runtime.parse_launch_args(self.mode_args(args, mode), "high")
                    self.assertEqual(forwarded, runtime.apply_launcher_settings(expected))

    def test_probes_skip_argument_helpers_login_proxy_and_file_reads(self):
        with patch.object(runtime, "parse_launch_args", side_effect=AssertionError("Probe must not parse")), \
             patch.object(runtime, "apply_launcher_settings", side_effect=AssertionError("Probe settings")), \
             patch.object(runtime, "apply_plane_mcp", side_effect=AssertionError("Probe Plane config")):
            for mode in ("terminal", "paseo"):
                for configured in (False, True):
                    for args in (["--version"], ["-v"], ["--help"], ["-h"],
                                 ["auth", "status"], ["auth", "status", "--json"]):
                        with self.subTest(mode=mode, configured=configured, args=args):
                            settings = dict(self.settings)
                            if not configured:
                                del settings["plane_mcp_config"]
                            forwarded, _ = self.intercepted_launch(args, mode, settings)
                            self.assertEqual(forwarded, args)
                            self.factory.assert_not_called()
                            self.adopt.assert_not_called()
                            self.read_json.assert_not_called()
                            self.open.assert_not_called()


class PeppyProfileTests(unittest.TestCase):
    """The second account forwards native arguments in its own profile."""

    PATH = "/private/config with spaces/plane-mcp.json"

    def setUp(self):
        self.settings = {"claude_bin": "/private/bin/claude", "peppy_config_dir": "/private/claude-peppy",
                         "plane_mcp_config": self.PATH, "node_dir": None}

    def assert_appended(self, original, expected):
        result = runtime.apply_plane_mcp(original, self.settings, prepend=False)
        self.assertEqual(result, expected)
        self.assertEqual(runtime.apply_plane_mcp(result, self.settings, prepend=False), result)

    def test_group_is_appended_so_a_leading_prompt_stays_positional(self):
        for original in ([], ["Hello", "world"], ["-p", "Hi"], ["--resume"],
                         ["--settings", '{"model": "sonnet"}', "Hi"]):
            with self.subTest(original=original):
                self.assert_appended(original, [*original, "--mcp-config", self.PATH])

    def test_group_is_inserted_before_the_literal_separator(self):
        self.assert_appended(["Hello", "--", "--mcp-config", "literal"],
                             ["Hello", "--mcp-config", self.PATH, "--", "--mcp-config", "literal"])
        self.assert_appended(["--", "prompt text"], ["--mcp-config", self.PATH, "--", "prompt text"])

    def test_strict_and_managed_and_unconfigured_arguments_are_unchanged(self):
        managed = ["--mcp-config", "caller.json", self.PATH, "-p", "Hi"]
        for args in (["--strict-mcp-config", "-p", "Hi"],
                     ["-p", "Hi", "--strict-mcp-config"],
                     ["--mcp-config=" + self.PATH],
                     managed):
            with self.subTest(args=args):
                self.assertIs(runtime.apply_plane_mcp(args, self.settings, prepend=False), args)

    def test_caller_groups_are_extended_without_repeating_the_flag(self):
        for group in (["--mcp-config", "caller.json"],
                      ["--mcp-config=caller.json", "other.json"],
                      ["--mcp-config", "caller.json", "second.json"]):
            for after in ([], ["-p", "Hi"], ["--", "literal"]):
                with self.subTest(group=group, after=after):
                    self.assert_appended([*group, *after], [*group, self.PATH, *after])

    def test_environment_is_scrubbed_and_pinned_to_the_profile(self):
        scrubbed = {"ANTHROPIC_API_KEY": "ambient", "ANTHROPIC_AUTH_TOKEN": "ambient",
                    "ANTHROPIC_BASE_URL": "http://ambient.example", "ANTHROPIC_CUSTOM_HEADERS": "x",
                    "ANTHROPIC_MODEL": "m", "ANTHROPIC_DEFAULT_MODEL": "m",
                    "ANTHROPIC_SMALL_FAST_MODEL": "m", "CLAUDE_CODE_SUBAGENT_MODEL": "m",
                    "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CODE_USE_BEDROCK": "1",
                    "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
                    "CLAUDE_CODE_USE_MANTLE": "1", "CLAUDE_CODE_API_KEY_HELPER": "/bin/primary-key.sh",
                    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "1",
                    "CLAUDE_CODE_EFFORT_LEVEL": "high", "MAX_THINKING_TOKENS": "1",
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1050000",
                    "CLAUDE_CODEX_PASEO_USAGE": "1", "CLAUDE_CODEX_AUTO_MODE": "1",
                    "PLANE_API_KEY": "secret"}
        scrubbed.update({f"ANTHROPIC_DEFAULT_{alias}_MODEL": "m"
                         for alias in ("OPUS", "SONNET", "HAIKU", "FABLE")})
        source = {"CLAUDE_CONFIG_DIR": "/inherited-profile", "HTTPS_PROXY": "http://proxy.example",
                  "NO_PROXY": "internal.example", "PATH": "/usr/bin", "HOME": "/Users/test"}
        env = runtime.peppy_env({**self.settings, "node_dir": "/private/node/bin"}, {**source, **scrubbed})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/private/claude-peppy")
        self.assertEqual(env["PATH"], "/private/node/bin:/usr/bin")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example")
        self.assertEqual(env["NO_PROXY"], "internal.example")
        self.assertEqual(env["HOME"], "/Users/test")
        for name in scrubbed:
            self.assertNotIn(name, env, name)

    def test_launch_forwards_arguments_and_requires_saved_settings(self):
        with patch.object(runtime.os, "execve") as execute:
            runtime.launch_peppy(self.settings, ["--settings", '{"env": {}}', "-p"])
        binary, forwarded, env = execute.call_args.args
        self.assertEqual(binary, "/private/bin/claude")
        self.assertEqual(forwarded, [binary, "--settings", '{"env": {}}', "-p", "--mcp-config", self.PATH])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/private/claude-peppy")
        for args in (["--version"], ["-v"], ["--help"], ["-h"], ["auth", "status"]):
            with self.subTest(args=args):
                with patch.object(runtime.os, "execve") as execute:
                    runtime.launch_peppy(self.settings, args)
                self.assertEqual(execute.call_args.args[1], [self.settings["claude_bin"], *args])
        for settings in ({"claude_bin": "/private/bin/claude"}, {"peppy_config_dir": "/private/claude-peppy"}):
            with self.subTest(settings=sorted(settings)):
                with self.assertRaises(runtime.SetupError):
                    runtime.launch_peppy(settings, ["-p", "Hi"])


class PaseoTranscriptTests(unittest.TestCase):
    """Sessions recorded by older launchers move into the profile Paseo reads."""

    SESSION = "0b7d9a8c-1111-4222-8333-444455556666"

    def test_resume_operand_must_look_like_a_session_id(self):
        session = self.SESSION
        for args in (["--model", "x", "--resume", session, "--output-format", "stream-json"],
                     ["-r", session], [f"--resume={session}"], ["-p", "Hi", "-r", session.upper()]):
            with self.subTest(args=args):
                self.assertEqual(runtime.resumed_session(args).lower(), session)
        for args in ([], ["--resume"], ["--resume", "--model=x"], ["--resume", "filter text"],
                     ["--resume", "-"], ["--resume="], ["--", "--resume", session],
                     ["--append-system-prompt", "--resume", session]):
            with self.subTest(args=args):
                self.assertIsNone(runtime.resumed_session(args))

    def test_resumed_session_moves_into_daemon_profile_once(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-transcript-test-") as temp:
            base = Path(temp)
            settings = {"config_dir": str(base / "config")}
            project = "-home-user-project"
            source = base / "config" / "claude" / "projects" / project
            source.mkdir(parents=True)
            (source / f"{self.SESSION}.jsonl").write_text('{"type":"user"}\n')
            (source / self.SESSION / "subagents").mkdir(parents=True)
            (source / self.SESSION / "subagents" / "agent-1.jsonl").write_text("{}\n")
            (source / "unrelated.jsonl").write_text("{}\n")
            env = {"CLAUDE_CODEX_PASEO_USAGE": "1", "CLAUDE_CONFIG_DIR": str(base / "daemon")}
            args = ["--model", "gpt-6-astra(high)", "--resume", self.SESSION, "--output-format", "stream-json"]
            runtime.adopt_isolated_transcript(settings, args, env)
            target = base / "daemon" / "projects" / project
            self.assertEqual((target / f"{self.SESSION}.jsonl").read_text(), '{"type":"user"}\n')
            self.assertTrue((target / self.SESSION / "subagents" / "agent-1.jsonl").is_file())
            self.assertFalse((source / f"{self.SESSION}.jsonl").exists())
            self.assertFalse((source / self.SESSION).exists())
            self.assertTrue((source / "unrelated.jsonl").is_file())
            # The transcript Paseo already reads is never replaced by an older copy.
            (target / f"{self.SESSION}.jsonl").write_text("current\n")
            (source / f"{self.SESSION}.jsonl").write_text("stale\n")
            runtime.adopt_isolated_transcript(settings, args, env)
            self.assertEqual((target / f"{self.SESSION}.jsonl").read_text(), "current\n")
            self.assertEqual((source / f"{self.SESSION}.jsonl").read_text(), "stale\n")
            # Without a resumed session, or when both profiles coincide, nothing moves.
            runtime.adopt_isolated_transcript(settings, ["-p", "Hi"], env)
            runtime.adopt_isolated_transcript(settings, args, {"CLAUDE_CONFIG_DIR": str(base / "config" / "claude")})
            self.assertEqual((source / f"{self.SESSION}.jsonl").read_text(), "stale\n")

    def test_daemon_profile_defaults_to_home_claude(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-transcript-test-") as temp:
            base = Path(temp)
            settings = {"config_dir": str(base / "config")}
            source = base / "config" / "claude" / "projects" / "-work"
            source.mkdir(parents=True)
            (source / f"{self.SESSION}.jsonl").write_text("{}\n")
            with patch.dict(os.environ, {"HOME": str(base / "home")}):
                self.assertEqual(runtime.daemon_profile({}), base / "home" / ".claude")
                runtime.adopt_isolated_transcript(settings, [f"--resume={self.SESSION}"], {})
            self.assertTrue((base / "home" / ".claude" / "projects" / "-work" / f"{self.SESSION}.jsonl").is_file())
            self.assertFalse((source / f"{self.SESSION}.jsonl").exists())


class LoginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claude-codex-login-test-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.auth_dir = self.base / "config" / "auth"
        self.auth_dir.mkdir(parents=True)
        self.state_dir = self.base / "state"
        self.state_dir.mkdir()
        self.runtime = runtime.Runtime({"config_dir": str(self.auth_dir.parent),
                                        "state_dir": str(self.state_dir), "proxy_bin": "/unused/proxy"})
        self.old_file = self.auth_dir / "old-account.json"
        self.old_auth = {"type": "codex", "refresh_token": "old-test-token"}
        runtime.write_json(self.old_file, self.old_auth)
        stop = patch.object(self.runtime, "stop")
        self.stop = stop.start()
        self.addCleanup(stop.stop)

    def test_zero_exit_without_saved_credentials_rejects_old_login(self):
        before = self.old_file.read_bytes()
        self.assertTrue(self.runtime.has_login())
        with patch.object(runtime.subprocess, "run") as run, \
             self.assertRaisesRegex(runtime.SetupError, "No new or updated Codex OAuth credentials"):
            self.runtime.login()
        self.stop.assert_called_once_with()
        run.assert_called_once_with(["/unused/proxy", "-config", str(self.auth_dir.parent / "proxy.yaml"),
                                     "-codex-login"], cwd=self.state_dir, check=True)
        self.assertEqual(self.old_file.read_bytes(), before)
        self.assertTrue(self.runtime.has_login())

    def test_zero_exit_without_any_credentials_fails(self):
        self.old_file.unlink()
        self.assertFalse(self.runtime.has_login())
        with patch.object(runtime.subprocess, "run"), self.assertRaises(runtime.SetupError):
            self.runtime.login()
        self.assertEqual(list(self.auth_dir.iterdir()), [])

    def test_new_credentials_and_device_flags_are_accepted(self):
        saved = self.auth_dir / "new-account.json"
        def save(*args, **kwargs):
            runtime.write_json(saved, {"type": "codex", "refresh_token": "new-test-token"})
            saved.chmod(0o644)
        with patch.object(runtime.subprocess, "run", side_effect=save) as run:
            self.runtime.login(device=True, no_browser=True)
        self.assertEqual(run.call_args.args[0][-2:], ["-codex-device-login", "-no-browser"])
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(runtime.read_json(self.old_file), self.old_auth)

    def test_updated_credentials_are_accepted_even_if_mtime_is_unchanged(self):
        original_stat = self.old_file.stat()
        def save(*args, **kwargs):
            self.old_file.write_text('{"type":"codex","refresh_token":"new-test-token"}')
            os.utime(self.old_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with patch.object(runtime.subprocess, "run", side_effect=save):
            self.runtime.login()
        self.assertEqual(runtime.read_json(self.old_file)["refresh_token"], "new-test-token")

    def test_same_content_rewrite_is_accepted(self):
        before = self.old_file.read_bytes()
        original_stat = self.old_file.stat()
        def save(*args, **kwargs):
            self.old_file.write_bytes(before)
            os.utime(self.old_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000))
        with patch.object(runtime.subprocess, "run", side_effect=save):
            self.runtime.login()
        self.assertEqual(self.old_file.read_bytes(), before)

    def test_same_content_atomic_replacement_is_accepted(self):
        original_stat = self.old_file.stat()
        def save(*args, **kwargs):
            runtime.write_json(self.old_file, self.old_auth)
            os.utime(self.old_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with patch.object(runtime.subprocess, "run", side_effect=save):
            self.runtime.login()
        self.assertEqual(runtime.read_json(self.old_file), self.old_auth)

    def test_invalid_new_credentials_cannot_reuse_old_login(self):
        saved = self.auth_dir / "invalid.json"
        for content in ('not json', '[]', '{}',
                        '{"type":"other","refresh_token":"test"}',
                        '{"type":"codex","refresh_token":"test","disabled":true}',
                        '{"type":"codex","refresh_token":""}'):
            with self.subTest(content=content):
                def save(*args, **kwargs):
                    saved.write_text(content)
                with patch.object(runtime.subprocess, "run", side_effect=save), \
                     self.assertRaises(runtime.SetupError):
                    self.runtime.login()
                self.assertEqual(runtime.read_json(self.old_file), self.old_auth)

    def test_nonzero_exit_does_not_accept_credentials(self):
        before = self.old_file.read_bytes()
        with patch.object(runtime.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "login")), \
             self.assertRaises(subprocess.CalledProcessError):
            self.runtime.login()
        self.assertEqual(self.old_file.read_bytes(), before)


class FileLockTests(unittest.TestCase):
    def test_timeout_release_and_private_stable_lock_file(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-lock-test-") as temp:
            path = Path(temp) / "state" / "operation.lock"
            with self.assertRaisesRegex(ValueError, "fixture failure"):
                with runtime.file_lock(path):
                    inode = path.stat().st_ino
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    with self.assertRaisesRegex(runtime.SetupError, "fixture busy"):
                        with runtime.file_lock(path, timeout=0, busy_message="fixture busy"):
                            self.fail("A second lock was acquired")
                    raise ValueError("fixture failure")
            with runtime.file_lock(path, timeout=0):
                self.assertEqual(path.stat().st_ino, inode)


if __name__ == "__main__":
    unittest.main()

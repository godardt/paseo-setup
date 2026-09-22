"""Optional real Paseo daemon → wrapper → Claude → proxy integration test."""

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_proxy_integration import Upstream, free_port

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime
import install


def copy_paseo_package(binary, destination):
    """Copy, never hard-link, the CLI and its server before running an installer."""
    entrypoint = Path(binary).resolve(strict=True)
    for root in entrypoint.parents:
        manifest = root / "package.json"
        if manifest.is_file() and json.loads(manifest.read_text()).get("name") == "@getpaseo/cli":
            break
    else:
        raise AssertionError(f"Cannot locate the selected Paseo CLI package for {entrypoint}")
    reader = Path("node_modules/@getpaseo/server/dist/server/server/agent/providers/claude/agent.js")
    if not (root / reader).is_file():
        raise AssertionError(f"Selected CLI has no bundled Claude provider module at {root / reader}")
    # Dereference package/.bin links as regular private files too, so the
    # private daemon never resolves back into the user's installed server.
    shutil.copytree(root, destination, symlinks=False)
    private_entrypoint = destination / entrypoint.relative_to(root)
    private_reader = destination / reader
    for private, source in ((private_entrypoint, entrypoint), (private_reader, root / reader)):
        if not private.is_file() or private.is_symlink():
            raise AssertionError(f"Private package entry must be a regular file: {private}")
        if destination.resolve() not in private.resolve().parents:
            raise AssertionError(f"Private package entry resolves outside the copy: {private}")
        if private.samefile(source):
            raise AssertionError(f"Private copy shares an inode with {source}")
    return private_entrypoint


FAKE_PEPPY_CLAUDE = r'''
import json, os, re, sys, threading, uuid
from pathlib import Path

# The SDK delivers the prompt over stdin; answer without depending on it.
threading.Thread(target=sys.stdin.read, daemon=True).start()
args = sys.argv[1:]
profile = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
profile.mkdir(parents=True, exist_ok=True)
if "--version" in args or "-v" in args:
    print("2.1.246 (Claude Code)")
    raise SystemExit(0)
(profile / "spawn-report.json").write_text(json.dumps({
    "args": args, "config": os.environ.get("CLAUDE_CONFIG_DIR"), "cwd": os.getcwd(),
    "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), "marker": os.environ.get("CLAUDE_PEPPY_PASEO")}))
if args[:2] == ["auth", "status"]:
    print(json.dumps({"type": "auth", "loggedIn": True, "account": "peppy"}))
    raise SystemExit(0)
session = str(uuid.uuid4())
cwd = os.getcwd()
model = "claude-sonnet-5"
stamp = "2026-09-15T12:00:00.000Z"


def emit(message):
    print(json.dumps(message), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session, "model": model,
      "permissionMode": "default", "tools": [{"name": "Bash"}, {"name": "Read"}], "cwd": cwd})
emit({"type": "assistant", "session_id": session,
      "message": {"id": "msg_peppy", "type": "message", "role": "assistant", "model": model,
                  "content": [{"type": "text", "text": "OK"}], "stop_reason": None,
                  "usage": {"input_tokens": 10, "output_tokens": 2}}})
entries = [
    {"parentUuid": None, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": session,
     "version": "2.1.246", "gitBranch": None, "type": "user", "uuid": str(uuid.uuid4()),
     "timestamp": stamp, "message": {"role": "user", "content": "Reply with OK."}},
    {"parentUuid": None, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": session,
     "version": "2.1.246", "gitBranch": None, "type": "assistant", "uuid": str(uuid.uuid4()),
     "timestamp": stamp,
     "message": {"id": "msg_peppy", "type": "message", "role": "assistant", "model": model,
                 "content": [{"type": "text", "text": "OK"}], "stop_reason": None,
                 "usage": {"input_tokens": 10, "output_tokens": 2}}},
]
project = profile / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
project.mkdir(parents=True, exist_ok=True)
(project / (session + ".jsonl")).write_text("".join(json.dumps(entry) + "\n" for entry in entries))
emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 12, "duration_api_ms": 10,
      "num_turns": 1, "result": "OK", "session_id": session, "total_cost_usd": 0.0,
      "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0}})
'''


class PrivatePaseoCopyTests(unittest.TestCase):
    def test_launcher_and_symlinked_server_become_private_regular_files(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-copy-") as temp:
            base = Path(temp)
            root = base / "installed-cli"
            entrypoint = root / "dist" / "index.js"
            entrypoint.parent.mkdir(parents=True)
            (root / "package.json").write_text(json.dumps({"name": "@getpaseo/cli"}))
            entrypoint.write_text("#!/usr/bin/env node\n")
            entrypoint.chmod(0o755)
            server = base / "installed-server"
            reader = server / "dist/server/server/agent/providers/claude/agent.js"
            reader.parent.mkdir(parents=True)
            reader.write_text("original reader\n")
            server_link = root / "node_modules/@getpaseo/server"
            server_link.parent.mkdir(parents=True)
            server_link.symlink_to(server, target_is_directory=True)
            selected = base / "paseo"
            selected.symlink_to(entrypoint)
            destination = base / "private-cli"
            private_entrypoint = copy_paseo_package(selected, destination)
            self.assertEqual(private_entrypoint, destination / "dist/index.js")
            private_reader = destination / "node_modules/@getpaseo/server" / reader.relative_to(server)
            private_reader.write_text("patched private reader\n")
            self.assertEqual(reader.read_text(), "original reader\n")
            self.assertFalse((destination / "node_modules/@getpaseo/server").is_symlink())
            self.assertTrue(os.access(private_entrypoint, os.X_OK))


@unittest.skipUnless(all(os.environ.get(key) for key in
    ("PASEO_TEST_BINARY", "CLAUDE_TEST_BINARY", "CLIPROXYAPI_TEST_BINARY")),
    "Set PASEO_TEST_BINARY, CLAUDE_TEST_BINARY and CLIPROXYAPI_TEST_BINARY")
class PaseoIntegrationTests(unittest.TestCase):
    def test_installed_provider_completes_a_session(self):
        self.run_paseo_session("max")

    def test_ultracode_is_a_direct_model_selection(self):
        self.run_paseo_session("ultracode")

    def test_ultracode_runs_through_native_paseo_thinking_option(self):
        self.run_paseo_session("xhigh", "ultracode")

    def test_context_meter_uses_final_input_and_cached_tokens(self):
        self.run_paseo_session("max", check_usage=True)

    def test_plugin_keeps_agents_requesting_auto_mode_in_prompting_mode(self):
        self.run_paseo_session("high", check_auto_mode=True)

    def isolated_daemon_environment(self, base):
        """A private daemon environment: no host profiles, credentials, or selectors.

        The daemon's CLAUDE_CONFIG_DIR is where Paseo reloads transcripts for
        providers that do not pin their own profile.
        """
        paseo_home = base / "paseo"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PASEO_", "CLAUDE_", "ANTHROPIC_", "OPENAI_", "CODEX_"))
               and key != "CLAUDECODE"}
        env["PASEO_HOME"] = str(paseo_home)
        # Paseo commands select the daemon by PASEO_HOME; PASEO_HOST as well
        # is ambiguous since 0.9. Direct client connections use the endpoint
        # instead (client_environment).
        endpoint = f"127.0.0.1:{free_port()}"
        home = base / "home"
        home.mkdir()
        env["HOME"] = str(home)
        for name, directory in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                                ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
            env[name] = str(home / directory)
        daemon_profile = base / "daemon-claude"
        env["CLAUDE_CONFIG_DIR"] = str(daemon_profile)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        runtime.write_json(paseo_home / "config.json", {
            "version": 1,
            "daemon": {"listen": endpoint, "relay": {"enabled": False},
                       "mcp": {"enabled": False, "injectIntoAgents": False}},
            "agents": {"providers": {name: {"enabled": False} for name in
                       ("claude", "codex", "copilot", "opencode", "pi", "omp")}},
        })
        return env, paseo_home, endpoint, daemon_profile

    def client_environment(self, env, endpoint):
        """Environment for direct daemon client connections: the endpoint, not the home."""
        return {**{name: value for name, value in env.items() if name != "PASEO_HOME"}, "PASEO_HOST": endpoint}

    def install_command(self, base, claude_bin, paseo_bin, *extra):
        return [
            "bash", str(ROOT / "install.sh"), "--config-dir", str(base / "config"),
            "--data-dir", str(base / "data"), "--state-dir", str(base / "state"),
            "--bin-dir", str(base / "bin"), "--paseo-home", str(base / "paseo"),
            "--port", str(free_port()), "--claude-bin", claude_bin, "--paseo-bin", paseo_bin,
            "--proxy-binary", os.environ["CLIPROXYAPI_TEST_BINARY"],
            "--skip-login", "--skip-paseo-start", "--no-path", *extra,
        ]

    def test_peppy_provider_sessions_use_the_daemon_profile_with_the_saved_token(self):
        """The second account is selected by its token; history replays from the daemon's profile."""
        with tempfile.TemporaryDirectory(prefix="claude-codex-peppy-") as temp:
            base = Path(temp)
            private_root = base / "private-paseo-cli"
            private_paseo = copy_paseo_package(os.environ["PASEO_TEST_BINARY"], private_root)
            client_module = private_root / "dist" / "utils" / "client.js"
            env, paseo_home, endpoint, daemon_profile = self.isolated_daemon_environment(base)
            client_env = self.client_environment(env, endpoint)
            fake_claude = base / "fake-claude"
            fake_claude.write_text(f"#!{sys.executable}\n" + FAKE_PEPPY_CLAUDE)
            fake_claude.chmod(0o755)
            peppy_profile = base / "claude-peppy"
            token_file = base / "peppy-token.txt"
            token_file.write_text("sk-ant-oat01-integration-test\n")
            install_command = self.install_command(base, str(fake_claude), str(private_paseo),
                                                   "--peppy-config-dir", str(peppy_profile),
                                                   "--peppy-oauth-token-file", str(token_file))
            result = subprocess.run(install_command, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            providers = runtime.read_json(paseo_home / "config.json")["agents"]["providers"]
            self.assertEqual(providers["claude-peppy"]["env"], {"CLAUDE_PEPPY_PASEO": "1"})
            self.assertNotIn("sk-ant-oat01", (paseo_home / "config.json").read_text())
            paseo = str(base / "bin" / "paseo-codex")
            try:
                # The installer's own relaunch: stop (a no-op here), start, reload.
                install.restart_paseo_daemon(paseo, env)
                install.reload_paseo(paseo, env)
                project = base / "project"
                project.mkdir()
                run = self.run_paseo(paseo, env, "run", "--provider", "claude-peppy",
                                     "--model", "claude-sonnet-5", "--cwd", str(project),
                                     "--wait-timeout", "90s", "--json", "Reply with OK.")
                self.assertIn("OK", run.stdout)
                agent_id = re.search(r'"agentId"\s*:\s*"([^"]+)"', run.stdout).group(1)
                report = json.loads((daemon_profile / "spawn-report.json").read_text())
                self.assertEqual(report["config"], str(daemon_profile))
                self.assertEqual(report["token"], "sk-ant-oat01-integration-test")
                self.assertEqual(report["marker"], "1")
                self.assertEqual(len(list(daemon_profile.glob("projects/*/*.jsonl"))), 1,
                                 "the second account's Paseo session must record its transcript where the daemon reads")
                self.assertFalse(list(peppy_profile.rglob("*.jsonl")),
                                 "Paseo sessions must not land in the terminal profile of the second account")
                # A fresh daemon rebuilds the conversation without any patch.
                self.run_paseo(paseo, env, "daemon", "restart", "--json", timeout=90)
                snapshot = self.fetch_agent(client_module, client_env, agent_id, with_timeline=True)
                timeline_text = json.dumps(snapshot["timeline"])
                self.assertIn("Reply with OK.", timeline_text)
                self.assertIn("OK", timeline_text)
                self.assertNotEqual(snapshot["timeline"], [],
                                    "replay after restart must come from the daemon's profile")
            finally:
                subprocess.run([paseo, "daemon", "stop", "--timeout", "10"], env=env,
                               capture_output=True, text=True, timeout=30)

    def run_paseo_session(self, effort, thinking=None, check_usage=False, check_auto_mode=False):
        with tempfile.TemporaryDirectory(prefix="claude-codex-paseo-") as temp:
            base = Path(temp)
            private_root = base / "private-paseo-cli"
            private_paseo = copy_paseo_package(os.environ["PASEO_TEST_BINARY"], private_root)
            client_module = private_root / "dist" / "utils" / "client.js"
            env, _, endpoint, daemon_profile = self.isolated_daemon_environment(base)
            client_env = self.client_environment(env, endpoint)
            install_command = self.install_command(base, os.environ["CLAUDE_TEST_BINARY"], str(private_paseo))
            for attempt in range(2):
                result = subprocess.run(install_command, env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, f"Install {attempt + 1}: {result.stderr}")
            settings = runtime.read_json(base / "config" / "settings.json")
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            upstream.requests = queue.Queue()
            if check_usage:
                upstream.usage = {"input_tokens": 120000, "output_tokens": 500, "total_tokens": 120500,
                                  "input_tokens_details": {"cached_tokens": 90000}}
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            config = runtime.proxy_config(settings)
            # The launcher's login presence check uses a dummy file, while the
            # real proxy reads a separate EMPTY auth directory. It can only route
            # using the fake API-key credential and the local upstream below.
            config["auth-dir"] = str(base / "empty-proxy-auth")
            (base / "empty-proxy-auth").mkdir()
            config["request-retry"] = 0
            config["codex-api-key"] = [{"api-key": "fake-test-upstream-key",
                "base-url": f"http://127.0.0.1:{upstream.server_port}",
                "models": [{"name": runtime.MODEL, "alias": runtime.MODEL},
                           {"name": runtime.MODEL, "alias": runtime.ULTRACODE_MODEL}]}]
            runtime.write_json(base / "config" / "proxy.yaml", config)
            runtime.write_json(base / "config" / "auth" / "test.json",
                               {"type": "codex", "refresh_token": "dummy-presence-check-only"})
            proxy = runtime.Runtime(settings)
            paseo = str(base / "bin" / "paseo-codex")
            try:
                proxy.start()
                # The installer's own relaunch: stop (a no-op here), start, reload.
                install.restart_paseo_daemon(paseo, env)
                install.reload_paseo(paseo, env)
                if effort == "ultracode":
                    catalog = subprocess.run(["node", "--input-type=module", "-e",
                        "const {connectToDaemon}=await import(process.argv[1]); "
                        "const client=await connectToDaemon({target:{kind:'endpoint',host:process.env.PASEO_HOST}}); "
                        "try {console.log(JSON.stringify(await client.listProviderModels('claude-codex',{cwd:process.argv[2]})));} "
                        "finally {await client.close();}", client_module.as_uri(), str(base)],
                        env=client_env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(catalog.returncode, 0, catalog.stderr)
                    models = json.loads(catalog.stdout)["models"]
                    selected = next(model for model in models if model["id"] == runtime.ULTRACODE_MODEL)
                    self.assertIn("Ultra Code", selected["label"])
                    self.assertEqual(selected["thinkingOptions"][0]["id"], "ultracode")
                if check_auto_mode:
                    self.run_auto_mode_session(paseo, client_module, env, client_env, base, upstream)
                    return
                result = subprocess.run([
                    paseo, "run", "--provider", "claude-codex", "--model", runtime.model_id(effort),
                    *(["--thinking", thinking] if thinking else []),
                    "--cwd", str(base), "--wait-timeout", "60s", "--json", "Reply with OK.",
                ], env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-3000:])
                self.assertIn("OK", result.stdout)
                # Claude flushes the transcript shortly after the turn completes.
                deadline = time.monotonic() + 20
                while not list((daemon_profile / "projects").rglob("*.jsonl")) and time.monotonic() < deadline:
                    time.sleep(0.5)
                self.assertTrue(list((daemon_profile / "projects").rglob("*.jsonl")),
                                "Paseo session transcript is missing from the daemon's Claude profile")
                self.assertFalse((base / "config" / "claude" / "projects").exists(),
                                 "Paseo session was recorded in the isolated terminal profile")
                if check_usage:
                    agent_id = re.search(r'"agentId"\s*:\s*"([^"]+)"', result.stdout).group(1)
                    report = subprocess.run(["node", "--input-type=module", "-e",
                        "const {connectToDaemon}=await import(process.argv[1]); "
                        "const client=await connectToDaemon({target:{kind:'endpoint',host:process.env.PASEO_HOST}}); "
                        "try {const result=await client.fetchAgent({agentId:process.argv[2]}); "
                        "console.log(JSON.stringify(result.agent.lastUsage));} finally {await client.close();}",
                        client_module.as_uri(), agent_id], env=client_env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(report.returncode, 0, report.stderr)
                    usage = json.loads(report.stdout)
                    self.assertEqual(usage.get("contextWindowMaxTokens"), 1050000, usage)
                    self.assertEqual(usage.get("contextWindowUsedTokens"), 120500, usage)
                    upstream.usage = {"input_tokens": 180000, "output_tokens": 650, "total_tokens": 180650,
                                      "input_tokens_details": {"cached_tokens": 150000}}
                    sent = subprocess.run([paseo, "send", agent_id, "Reply with OK again.", "--json"],
                                          env=env, capture_output=True, text=True, timeout=90)
                    self.assertEqual(sent.returncode, 0, sent.stderr + sent.stdout)
                    report = subprocess.run(["node", "--input-type=module", "-e",
                        "const {connectToDaemon}=await import(process.argv[1]); "
                        "const client=await connectToDaemon({target:{kind:'endpoint',host:process.env.PASEO_HOST}}); "
                        "try {const result=await client.fetchAgent({agentId:process.argv[2]}); "
                        "console.log(JSON.stringify(result.agent.lastUsage));} finally {await client.close();}",
                        client_module.as_uri(), agent_id], env=client_env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(report.returncode, 0, report.stderr)
                    usage = json.loads(report.stdout)
                    self.assertEqual(usage.get("contextWindowMaxTokens"), 1050000, usage)
                    self.assertEqual(usage.get("contextWindowUsedTokens"), 180650, usage)
                requests = []
                while not upstream.requests.empty():
                    requests.append(upstream.requests.get_nowait()[1])
                self.assertTrue(requests, result.stdout)
                self.assertTrue(all(req["model"] == runtime.MODEL for req in requests))
                self.assertTrue(any(req.get("reasoning", {}).get("effort") == ("xhigh" if effort == "ultracode" else effort) for req in requests))
                if thinking or effort == "ultracode":
                    tool_names = {tool.get("name", tool.get("type", ""))
                                  for req in requests for tool in req.get("tools", [])}
                    self.assertIn("Workflow", tool_names)
                    self.assertTrue(all(req.get("reasoning", {}).get("effort") == "xhigh" for req in requests))
            finally:
                if getattr(upstream, "sequence", None) is not None:
                    upstream.sequence.close()
                try:
                    subprocess.run([paseo, "daemon", "stop", "--timeout", "10"], env=env,
                                   capture_output=True, text=True, timeout=30)
                finally:
                    try:
                        proxy.stop()
                    finally:
                        upstream.shutdown()
                        upstream.server_close()

    def fetch_agent(self, client_module, env, agent_id, with_timeline=False):
        report = subprocess.run([
            "node", "--input-type=module", "-e",
            "const {connectToDaemon}=await import(process.argv[1]); "
            "const client=await connectToDaemon({target:{kind:'endpoint',host:process.env.PASEO_HOST}}); "
            "try {const result=await client.fetchAgent({agentId:process.argv[2]}); "
            "if (!result) throw new Error('Temporary agent was not found'); "
            "const agent=result.agent; let timeline; "
            "if (process.argv[3]) {const {fetchAgentTimelineItems}=await import(process.argv[3]); "
            "timeline=await fetchAgentTimelineItems(client,agent.id);} "
            "const subagents=(await client.listProviderSubagents(agent.id)).subagents; "
            "console.log(JSON.stringify({status:agent.status,lastUsage:agent.lastUsage??null,timeline,"
            "currentModeId:agent.currentModeId??null,runtimeModeId:agent.runtimeInfo?.modeId??null,"
            "availableModes:(agent.availableModes??[]).map(mode=>mode.id),subagents})); "
            "} finally {await client.close();}",
            client_module.as_uri(), agent_id,
            *([(client_module.parent.parent / "commands/agent/logs.js").as_uri()] if with_timeline else []),
        ], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(report.returncode, 0, report.stderr)
        return json.loads(report.stdout)

    def run_paseo(self, paseo, env, *args, timeout=180):
        result = subprocess.run([paseo, *args], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-3000:])
        return result

    def run_auto_mode_session(self, paseo, client_module, env, client_env, base, upstream):
        project = base / "project"
        project.mkdir()
        plugins = self.run_paseo(paseo, env, "plugin", "ls", "--json")
        self.assertIn("claude-codex", plugins.stdout)
        self.assertNotIn("failed", plugins.stdout)
        # Without a mode, the provider's default applies. The upstream only ever
        # answers with text, so a classifier request would be a Bash-free anomaly.
        result = self.run_paseo(paseo, env, "run", "--provider", "claude-codex", "--model", runtime.model_id("high"),
                                "--cwd", str(project), "--wait-timeout", "60s", "--json", "Reply with OK.")
        default_agent = re.search(r'"agentId"\s*:\s*"([^"]+)"', result.stdout).group(1)
        snapshot = self.fetch_agent(client_module, client_env, default_agent)
        self.assertEqual(snapshot["runtimeModeId"], "default", snapshot)
        self.assertIn("bypassPermissions", snapshot["availableModes"], snapshot)
        # An agent that explicitly asks for auto mode, such as one created from
        # an older profile, is created in the prompting mode by the plugin's
        # hook, and Claude runs it that way.
        result = self.run_paseo(paseo, env, "run", "--provider", "claude-codex", "--model", runtime.model_id("high"),
                                "--mode", "auto", "--cwd", str(project), "--wait-timeout", "60s", "--json", "Reply with OK.")
        auto_agent = re.search(r'"agentId"\s*:\s*"([^"]+)"', result.stdout).group(1)
        snapshot = self.fetch_agent(client_module, client_env, auto_agent)
        self.assertEqual(snapshot["currentModeId"], "default", snapshot)
        self.assertEqual(snapshot["runtimeModeId"], "default", snapshot)
        requests = []
        while not upstream.requests.empty():
            requests.append(upstream.requests.get_nowait()[1])
        self.assertTrue(requests)
        self.assertFalse([req for req in requests if "<transcript>" in json.dumps(req.get("input", []))],
                         "No auto-mode classifier request may reach the gateway")

if __name__ == "__main__":
    unittest.main()

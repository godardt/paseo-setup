"""Offline coverage for the Paseo plugin shipped with the installer.

The plugin is validated against Paseo's published plugin rules (manifest,
entry file, import boundaries, module placement) and its hook logic is
executed with Node's TypeScript type stripping when available. When a Paseo
installation is on PATH, its own plugin compiler bundles the installed copy
too. No daemon is started.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime
import install
import plane_mcp

PLUGIN = ROOT / "scripts" / "paseo_plugin"
NODE = shutil.which("node")
# Paseo's daemon loads only these entry names and bundles only these SDK
# specifiers without a local installation (plugins/compiler.js, 0.8.0).
SERVER_ENTRY = "index.server.ts"
ALLOWED_SPECIFIERS = ("@getpaseo/plugin", "@getpaseo/plugin/server", "@getpaseo/plugin/server/provider",
                      "@getpaseo/plugin/server/acp", "zod")
CONNECTOR_IMPORT = "./" + install.PLUGIN_CONNECTOR_FILE
# Paseo's compiler as installed with the paseo CLI (@getpaseo/server, 0.8.x).
PASEO_COMPILER = Path("dist", "server", "server", "plugins", "compiler.js")


def plugin_files(directory):
    return {str(path.relative_to(directory)) for path in Path(directory).rglob("*") if path.is_file()}


def run_node(script, *argv):
    result = subprocess.run([NODE, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e",
                             script, *argv], capture_output=True, text=True, timeout=60)
    if result.returncode and "bad option" in result.stderr:
        raise unittest.SkipTest("This Node.js cannot strip TypeScript types")
    return result


def run_plugin(script, entry=PLUGIN / SERVER_ENTRY):
    return run_node(script, entry.as_uri())


def paseo_compiler():
    """Paseo's plugin compiler module next to the paseo executable on PATH, if any."""
    found = shutil.which("paseo")
    if not found:
        return None
    for ancestor in Path(found).resolve().parents:
        candidate = ancestor / "node_modules" / "@getpaseo" / "server" / PASEO_COMPILER
        if candidate.is_file():
            return candidate
    return None


class PluginPackageTests(unittest.TestCase):
    def test_manifest_follows_paseo_rules(self):
        manifest = json.loads((PLUGIN / "paseo-plugin.json").read_text())
        self.assertEqual(set(manifest), {"id", "requirements"})
        self.assertEqual(manifest["id"], install.PLUGIN_ID)
        self.assertRegex(manifest["id"], r"^[a-z][a-z0-9-]*$")
        # Without a requirement Paseo 0.8+ refuses the plugin as pre-0.8.
        self.assertEqual(manifest["requirements"], {"paseo": ">=0.8.0"})
        self.assertEqual({*install.PLUGIN_FILES, install.PLUGIN_CONNECTOR_FILE}, plugin_files(PLUGIN))

    def test_server_entry_imports_only_the_sdk_types_and_the_connector_module(self):
        source = (PLUGIN / SERVER_ENTRY).read_text()
        imports = re.findall(r'^\s*import\s+(type\s+)?[^;]*?from\s+"([^"]+)";', source, re.MULTILINE)
        self.assertTrue(imports)
        runtime_imports = []
        for type_only, specifier in imports:
            if type_only:
                self.assertIn(specifier, ALLOWED_SPECIFIERS)
            else:
                runtime_imports.append(specifier)
        # The generated connector module is the only runtime import: bundled by
        # Paseo's compiler and loaded by Node's type stripping, no build step.
        self.assertEqual(runtime_imports, [CONNECTOR_IMPORT])
        # Paseo places plugin modules under client/, server/, or shared/; the
        # explicit .ts extension resolves in both esbuild and Node.
        self.assertRegex(install.PLUGIN_CONNECTOR_FILE, r"^(server|shared)/[a-z-]+\.ts$")
        self.assertNotIn("require(", source)
        self.assertIn("export default function contribute(", source)
        self.assertNotIn("build", json.loads((PLUGIN / "paseo-plugin.json").read_text()))

    def test_repository_connector_module_is_the_empty_default(self):
        self.assertEqual((PLUGIN / install.PLUGIN_CONNECTOR_FILE).read_text(), install.plane_connector_module(None))
        self.assertIn("PLANE_MCP_SERVERS: Readonly<Record<string, PlaneMcpServer>> = {};",
                      install.plane_connector_module(None))

    def test_connector_module_embeds_the_installed_stdio_servers_only(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-plugin-") as temp:
            config = Path(temp) / "space ' quote $dollar" / "plane-mcp.json"
            command, helper = "/opt/py thon/bin/python3", str(config.parent / "plane_mcp.py")
            runtime.write_json(config, {"mcpServers": {plane_mcp.SERVER_NAME: {
                "type": "stdio", "command": command, "args": [helper, "--credentials", str(config)],
                "env": {"UNUSED": "1"}}}})
            module = install.plane_connector_module(str(config))
            literal = re.search(r"PLANE_MCP_SERVERS: Readonly<Record<string, PlaneMcpServer>> = (\{.*?\});\n",
                                module, re.DOTALL).group(1)
            self.assertEqual(json.loads(literal), {plane_mcp.SERVER_NAME: {
                "type": "stdio", "command": command, "args": [helper, "--credentials", str(config)]}})
            self.assertTrue(module.isascii())
            for broken in ({"mcpServers": {"x": {"type": "http", "url": "https://example.invalid"}}},
                           {"mcpServers": {"x": {"type": "stdio", "command": "python3"}}},
                           {"mcpServers": {"x": {"type": "stdio", "command": "python3", "args": [1]}}},
                           {"mcpServers": []}, {}):
                runtime.write_json(config, broken)
                with self.assertRaises(runtime.SetupError):
                    install.plane_connector_module(str(config))
            with self.assertRaises(runtime.SetupError):
                install.plane_connector_module(str(config.parent / "missing.json"))

    def test_installed_copy_matches_the_repository_and_regenerates_the_connector(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-plugin-") as temp:
            target = install.install_plugin_files(temp)
            self.assertEqual(target, Path(temp) / "paseo-plugin")
            self.assertEqual(plugin_files(target), {*install.PLUGIN_FILES, install.PLUGIN_CONNECTOR_FILE})
            for name in (*install.PLUGIN_FILES, install.PLUGIN_CONNECTOR_FILE):
                self.assertEqual((target / name).read_text(), (PLUGIN / name).read_text())
                self.assertEqual((target / name).stat().st_mode & 0o777, 0o644)
            config = Path(temp) / "plane-mcp.json"
            runtime.write_json(config, {"mcpServers": {plane_mcp.SERVER_NAME: {
                "type": "stdio", "command": "/usr/bin/python3", "args": ["/data/plane_mcp.py"]}}})
            (target / "leftover.ts").write_text("// stale\n")
            (target / "server" / "old-connector.ts").write_text("// stale\n")
            install.install_plugin_files(temp, str(config))
            self.assertEqual(plugin_files(target), {*install.PLUGIN_FILES, install.PLUGIN_CONNECTOR_FILE})
            self.assertEqual((target / install.PLUGIN_CONNECTOR_FILE).read_text(),
                             install.plane_connector_module(str(config)))
            self.assertIn('"/data/plane_mcp.py"', (target / install.PLUGIN_CONNECTOR_FILE).read_text())
            # A later run without a connection (e.g. after removing it) resets the module.
            install.install_plugin_files(temp, None)
            self.assertEqual((target / install.PLUGIN_CONNECTOR_FILE).read_text(), install.plane_connector_module(None))
            self.assertEqual(install.plugin_entry(temp),
                             {"source": "directory", "path": str(target), "enabled": True})


@unittest.skipUnless(NODE, "Node.js is required to run the plugin")
class PluginBehaviorTests(unittest.TestCase):
    SCRIPT = r"""
const plugin = await import(process.argv[1]);
const events = [];
const server = {
  before(name, handler) { events.push(["before", name]); server.handler = handler; return () => events.push(["off", name]); },
  on(name) { events.push(["on", name]); return () => {}; },
};
const cleanup = plugin.default(server);
const run = (config) => server.handler({ request: { config, env: { KEEP: "1" } } }, { signal: null }) ?? null;
const out = {
  registrations: [...events],
  auto: run({ provider: "claude-codex", cwd: "/w", modeId: "auto", model: "gpt-6-astra(high)" }),
  bypass: run({ provider: "claude-codex", cwd: "/w", modeId: "bypassPermissions" }),
  unset: run({ provider: "claude-codex", cwd: "/w" }),
  peppy: run({ provider: "claude-peppy", cwd: "/w", modeId: "auto" }),
  claude: run({ provider: "claude", cwd: "/w", modeId: "auto" }),
  other: run({ provider: "codex", cwd: "/w", modeId: "auto" }),
  present: run({ provider: "claude", cwd: "/w", mcpServers: { [process.argv[2]]: { type: "http", url: "https://example.invalid/mcp" } } }),
  merged: run({ provider: "claude", cwd: "/w", mcpServers: { own: { type: "http", url: "https://example.invalid/own" } } }),
};
cleanup();
out.cleanup = events.slice(out.registrations.length);
console.log(JSON.stringify(out));
"""

    def run_script(self, entry):
        result = run_node(self.SCRIPT, entry.as_uri(), plane_mcp.SERVER_NAME)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["registrations"], [["before", "agent.create"]])
        self.assertEqual(out["cleanup"], [["off", "agent.create"]])
        return out

    def test_without_a_connector_only_explicit_auto_mode_for_the_gateway_provider_is_rewritten(self):
        out = self.run_script(PLUGIN / SERVER_ENTRY)
        self.assertEqual(out["auto"], {"config": {"provider": "claude-codex", "cwd": "/w", "modeId": "default",
                                                  "model": "gpt-6-astra(high)"}, "env": {"KEEP": "1"}})
        # Other modes, inherited modes, and other providers pass through untouched.
        for key in ("bypass", "unset", "peppy", "claude", "other", "present", "merged"):
            self.assertIsNone(out[key], key)

    def test_installed_connector_is_added_to_every_claude_code_provider(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-plugin-") as temp:
            config = Path(temp) / "plane-mcp.json"
            server = {"type": "stdio", "command": "/usr/bin/python3",
                      "args": ["/data/plane_mcp.py", "--credentials", "/config/plane-credentials.json"]}
            runtime.write_json(config, {"mcpServers": {plane_mcp.SERVER_NAME: server}})
            target = install.install_plugin_files(temp, str(config))
            out = self.run_script(target / SERVER_ENTRY)
        connector = {plane_mcp.SERVER_NAME: server}
        # Paseo's built-in provider never runs the launchers, so the hook is its
        # only path to the connector; the mode rewrite still applies alongside.
        self.assertEqual(out["claude"], {"config": {"provider": "claude", "cwd": "/w", "modeId": "auto",
                                                    "mcpServers": connector}, "env": {"KEEP": "1"}})
        self.assertEqual(out["auto"], {"config": {"provider": "claude-codex", "cwd": "/w", "modeId": "default",
                                                  "model": "gpt-6-astra(high)", "mcpServers": connector},
                                       "env": {"KEEP": "1"}})
        self.assertEqual(out["bypass"]["config"], {"provider": "claude-codex", "cwd": "/w",
                                                   "modeId": "bypassPermissions", "mcpServers": connector})
        self.assertEqual(out["unset"]["config"], {"provider": "claude-codex", "cwd": "/w", "mcpServers": connector})
        self.assertEqual(out["peppy"]["config"], {"provider": "claude-peppy", "cwd": "/w", "modeId": "auto",
                                                  "mcpServers": connector})
        # A caller's servers are kept and extended; its own definition of the
        # connector's name wins; non-Claude providers are never touched.
        self.assertEqual(out["merged"]["config"]["mcpServers"],
                         {"own": {"type": "http", "url": "https://example.invalid/own"}, **connector})
        self.assertIsNone(out["present"])
        self.assertIsNone(out["other"])


@unittest.skipUnless(NODE and paseo_compiler(), "A Paseo installation with its plugin compiler is required")
class PluginCompilationTests(unittest.TestCase):
    """Bundle the installed plugin with Paseo's own compiler and run the bundle."""

    SCRIPT = r"""
const { compilePlugin } = await import(process.argv[1]);
const bundles = await compilePlugin({ server: process.argv[2] });
const factory = (0, eval)(bundles.serverBundle);
const exports = factory((name) => { if (name === "@getpaseo/plugin/server") return {}; throw new Error("unexpected " + name); });
let handler;
const cleanup = exports.default({ before(name, h) { handler = h; return () => {}; }, on() { return () => {}; } });
const run = (config) => handler({ request: { config, env: {} } }, {}) ?? null;
console.log(JSON.stringify({ claude: run({ provider: "claude", cwd: "/w" }), other: run({ provider: "codex", cwd: "/w" }) }));
cleanup();
"""

    def test_paseo_compiles_and_runs_the_installed_plugin(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-plugin-") as temp:
            config = Path(temp) / "plane-mcp.json"
            server = {"type": "stdio", "command": "/usr/bin/python3", "args": ["/data/plane_mcp.py"]}
            runtime.write_json(config, {"mcpServers": {plane_mcp.SERVER_NAME: server}})
            target = install.install_plugin_files(temp, str(config))
            result = subprocess.run([NODE, "--no-warnings", "--input-type=module", "-e", self.SCRIPT,
                                     paseo_compiler().as_uri(), str(target / SERVER_ENTRY)],
                                    capture_output=True, text=True, timeout=120, env={**os.environ, "NO_COLOR": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {
            "claude": {"config": {"provider": "claude", "cwd": "/w", "mcpServers": {plane_mcp.SERVER_NAME: server}},
                       "env": {}},
            "other": None,
        })


if __name__ == "__main__":
    unittest.main()

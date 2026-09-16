"""Offline coverage for the Paseo plugin shipped with the installer.

The plugin is validated against Paseo's published plugin rules (manifest,
entry file, import boundaries) and its hook logic is executed with Node's
TypeScript type stripping when available. No daemon is started.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import install

PLUGIN = ROOT / "scripts" / "paseo_plugin"
NODE = shutil.which("node")
# Paseo's daemon loads only these entry names and bundles only these SDK
# specifiers without a local installation (plugins/compiler.js, 0.8.0).
SERVER_ENTRY = "index.server.ts"
ALLOWED_SPECIFIERS = ("@getpaseo/plugin", "@getpaseo/plugin/server", "@getpaseo/plugin/server/provider",
                      "@getpaseo/plugin/server/acp", "zod")


def run_plugin(script):
    result = subprocess.run([NODE, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script,
                             (PLUGIN / SERVER_ENTRY).as_uri()], capture_output=True, text=True, timeout=30)
    if result.returncode and "bad option" in result.stderr:
        raise unittest.SkipTest("This Node.js cannot strip TypeScript types")
    return result


class PluginPackageTests(unittest.TestCase):
    def test_manifest_follows_paseo_rules(self):
        manifest = json.loads((PLUGIN / "paseo-plugin.json").read_text())
        self.assertEqual(set(manifest), {"id", "requirements"})
        self.assertEqual(manifest["id"], install.PLUGIN_ID)
        self.assertRegex(manifest["id"], r"^[a-z][a-z0-9-]*$")
        # Without a requirement Paseo 0.8+ refuses the plugin as pre-0.8.
        self.assertEqual(manifest["requirements"], {"paseo": ">=0.8.0"})
        self.assertEqual(set(install.PLUGIN_FILES), {name.name for name in PLUGIN.iterdir()})

    def test_server_entry_has_no_runtime_dependencies(self):
        source = (PLUGIN / SERVER_ENTRY).read_text()
        imports = re.findall(r'^\s*import\s+(type\s+)?[^;]*?from\s+"([^"]+)";', source, re.MULTILINE)
        self.assertTrue(imports)
        for type_only, specifier in imports:
            self.assertTrue(type_only, f"runtime import of {specifier} would need a build step")
            self.assertIn(specifier, ALLOWED_SPECIFIERS)
        self.assertNotIn("require(", source)
        self.assertIn("export default function contribute(", source)
        self.assertNotIn("build", json.loads((PLUGIN / "paseo-plugin.json").read_text()))

    def test_installed_copy_matches_the_repository(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-plugin-") as temp:
            target = install.install_plugin_files(temp)
            self.assertEqual(target, Path(temp) / "paseo-plugin")
            (target / "leftover.ts").write_text("// stale\n")
            (target / "server").mkdir()
            install.install_plugin_files(temp)
            self.assertEqual({path.name for path in target.iterdir() if path.is_file()}, set(install.PLUGIN_FILES))
            for name in install.PLUGIN_FILES:
                self.assertEqual((target / name).read_text(), (PLUGIN / name).read_text())
                self.assertEqual((target / name).stat().st_mode & 0o777, 0o644)
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
};
cleanup();
out.cleanup = events.slice(out.registrations.length);
console.log(JSON.stringify(out));
"""

    def test_only_explicit_auto_mode_for_the_gateway_provider_is_rewritten(self):
        result = run_plugin(self.SCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["registrations"], [["before", "agent.create"]])
        self.assertEqual(out["cleanup"], [["off", "agent.create"]])
        self.assertEqual(out["auto"], {"config": {"provider": "claude-codex", "cwd": "/w", "modeId": "default",
                                                  "model": "gpt-6-astra(high)"}, "env": {"KEEP": "1"}})
        # Other modes, inherited modes, and other providers pass through untouched.
        for key in ("bypass", "unset", "peppy", "claude"):
            self.assertIsNone(out[key], key)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""One-command, per-user installer. No sudo; the only global npm install is Paseo, and only on request."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pty
import re
import secrets
import select
import shlex
import shutil
import signal
import string
import subprocess
import sys
import tarfile
import tempfile
import termios
import time

import plane_mcp
from claude_codex import (
    CONTEXT_WINDOW, EFFORTS, REASONING_MODES, ULTRACODE_MODEL, MARKER, MODEL, PASEO_MARKER, PEPPY_PASEO_MARKER,
    WORKTREE_SETUP_COMMAND, Runtime, SetupError, atomic_write, model_id, file_lock, peppy_env, proxy_config,
    read_json, write_json,
)
from claude_codex import say as emit

# Output tones, applied only on a color terminal (see paint).
TONES = {"step": "\x1b[1;36m", "ok": "\x1b[32m", "warn": "\x1b[33m", "prompt": "\x1b[1m"}


def color_enabled():
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def paint(text, tone):
    if tone is None or not color_enabled():
        return text
    return f"{TONES[tone]}{text}\x1b[0m"


def say(message, tone=None):
    emit(paint(message, tone))


def step(title):
    """Announce the installer stage the following messages and prompts belong to."""
    emit("")
    say(f"▸ {title}", "step")

PROXY_VERSION = "7.2.155"
# The Paseo plugin shipped in this repository; installed as a directory plugin
# from a private copy so the checkout can move or be removed afterwards.
PLUGIN_ID = "claude-codex"
# Appended to every Paseo agent's system prompt (daemon.appendSystemPrompt)
# between these marker lines, so a rerun updates the policy without touching
# text the user added around it.
PR_POLICY_START = "[claude-codex pull-request policy]"
PR_POLICY_END = "[/claude-codex pull-request policy]"
PR_POLICY = f"""{PR_POLICY_START}
Pull requests: when the task is finished and applicable, open a pull request before you stop. Applicable means \
the workspace is a git worktree or is on a branch other than the repository's default branch (the target of \
refs/remotes/origin/HEAD), the work is complete and produced changes, no pull request exists for the branch \
yet (check with `gh pr view`), and the user did not ask for a different workflow. To do so: commit the finished \
work with a clear message, push the branch to origin, create the pull request against the default branch with \
`gh pr create` (title, summary of the change, how it was tested), and report its URL. If a pull request already \
exists for the branch, push to it instead. Otherwise say briefly why no pull request was opened. Never push to \
the default branch. A Paseo worktree already starts from the latest default branch on origin; do not rebase or \
reset it unless asked.
{PR_POLICY_END}"""


def pull_request_policy(existing):
    """The system prompt text with the current policy block, keeping other text."""
    text = existing if isinstance(existing, str) else ""
    start, end = text.find(PR_POLICY_START), text.find(PR_POLICY_END)
    if start != -1 and end > start:
        before = text[:start].rstrip()
        after = text[end + len(PR_POLICY_END):].lstrip()
        parts = [part for part in (before, PR_POLICY, after) if part]
        return "\n\n".join(parts)
    text = text.strip()
    return f"{text}\n\n{PR_POLICY}" if text else PR_POLICY


PLUGIN_SOURCE = Path(__file__).with_name("paseo_plugin")
PLUGIN_FILES = ("paseo-plugin.json", "index.server.ts", "package.json")
# Generated from the installed Plane MCP configuration rather than copied, so
# the plugin can add the connector to every Claude Code agent Paseo creates.
PLUGIN_CONNECTOR_FILE = "server/plane-connector.ts"
PLANE_TOKEN_PAGE = "https://app.plane.so/settings/profile/api-tokens"
PLANE_PROMPT_ATTEMPTS = 3
# Earlier versions of this installer patched this module inside the selected
# Paseo package. That is no longer done or undone here; a module still carrying
# the marker is only reported.
LEGACY_AGENT_PATH = Path("dist/server/server/agent/providers/claude/agent.js")
LEGACY_PATCH_MARKER = "// Managed by claude-codex: live context usage"
CLAUDE_VERSION = "2.1.246"
# Published release checksums, pinned along with the default version.
CHECKSUMS = {
    "darwin_aarch64": "f90c503ce41a798c85b6f61dfe5fe8b812c1b889634f0c80d04ee376424fe305",
    "darwin_amd64": "198794a2fafb9fb8083476ac18232647c57d443422aa3f008d19ed7e75ca4604",
    "linux_aarch64_no-plugin": "ac1a8a38541cbdf4b83cb21e700e452340b31f76583d6effff97114259cc22a9",
    "linux_amd64_no-plugin": "2e4d1280baf48cb9184eb9e95c92079848202bb2ab9c23341176e9a4eca88881",
}


def absolute(value):
    return Path(value).expanduser().resolve()


def xdg_home(variable, home_suffix):
    return os.environ.get(variable, str(Path.home() / home_suffix))


def default_dir(variable, home_suffix, name):
    return os.path.join(xdg_home(variable, home_suffix), name)


def launcher_is_owned(path):
    """True when an existing launcher file was written by this installer."""
    try:
        return MARKER in Path(path).read_text()
    except (OSError, UnicodeError):
        return False


def assert_original_binary(value, bin_dir, wrappers, flag, program):
    """Reject CLI selections that point at this installer's own wrappers."""
    resolved = Path(value).resolve()
    if Path(value).name in wrappers or resolved in {(bin_dir / name).resolve() for name in wrappers}:
        raise SetupError(f"{flag} must point to the original {program} executable")


def backup(path):
    path = Path(path)
    if path.exists():
        destination = path.with_name(f"{path.name}.claude-codex-backup-{time.time_ns()}")
        shutil.copy2(path, destination)
        destination.chmod(0o600)
        say(f"Backup: {destination}")


def download(url, path):
    subprocess.run([
        "curl", "--fail", "--location", "--show-error", "--silent",
        "--retry", "3", "--connect-timeout", "20", "--max-time", "600",
        "--proto", "=https", "--proto-redir", "=https", "--output", str(path), url,
    ], check=True)


def install_proxy(data_dir, version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SetupError("--proxy-version must be a numeric release, such as 7.2.155")
    system = platform.system().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "aarch64", "aarch64": "aarch64"}.get(
        platform.machine().lower()
    )
    if system not in ("linux", "darwin") or not arch:
        raise SetupError("Supported platforms: Linux/WSL2 and macOS, x86_64 or ARM64")
    # The Linux no-plugin build is static and avoids requiring a particular glibc.
    target = f"{system}_{arch}" + ("_no-plugin" if system == "linux" else "")
    destination = data_dir / "releases" / version / "cli-proxy-api"
    if destination.is_file() and os.access(destination, os.X_OK):
        return destination
    filename = f"CLIProxyAPI_{version}_{target}.tar.gz"
    base = f"https://github.com/router-for-me/CLIProxyAPI/releases/download/v{version}"
    say(f"Downloading CLIProxyAPI {version} ({target})...")
    with tempfile.TemporaryDirectory(prefix="claude-codex-download-") as temp:
        archive = Path(temp) / filename
        download(f"{base}/{filename}", archive)
        expected = CHECKSUMS.get(target) if version == PROXY_VERSION else None
        if expected is None:
            checksum_file = Path(temp) / "checksums.txt"
            download(f"{base}/checksums.txt", checksum_file)
            for line in checksum_file.read_text().splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1].lstrip("*") == filename:
                    expected = fields[0]
                    break
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if expected is None or not secrets.compare_digest(digest, expected):
            raise SetupError(f"SHA-256 verification failed for {filename}")
        # Extract only a regular binary file; never extract arbitrary archive paths.
        with tarfile.open(archive, "r:gz") as tar:
            matches = [member for member in tar.getmembers()
                       if Path(member.name).name == "cli-proxy-api" and member.isfile()]
            if len(matches) != 1:
                raise SetupError("Release archive does not contain exactly one cli-proxy-api binary")
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = destination.with_suffix(".new")
            with tar.extractfile(matches[0]) as src, stage.open("wb") as out:
                shutil.copyfileobj(src, out)
            stage.chmod(0o700)
            os.replace(stage, destination)
    return destination


def executable(value, name):
    found = shutil.which(str(value))
    if not found:
        raise SetupError(f"Cannot find executable {name}: {value}")
    # Preserve the entrypoint symlink; npm CLIs can depend on their invocation path.
    return str(Path(found).absolute())


def resolve_cli(name, explicit, package, data_dir, previous=None):
    if explicit:
        return executable(explicit, name)
    prefix = data_dir / "npm" / name
    candidates = [shutil.which(name), previous, str(prefix / "node_modules" / ".bin" / name)]
    for found in candidates:
        if not found or not Path(found).is_file() or not os.access(found, os.X_OK):
            continue
        return str(Path(found).absolute())
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise SetupError(f"Installing missing {name} requires Node.js 22+ and npm. Install them and rerun.")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(version.lstrip("v").split(".")[0]) < 22:
        raise SetupError(f"Installing {name} requires Node.js 22+; found {version}")
    say(f"Installing {package} privately in {prefix}...")
    subprocess.run([npm, "install", "--prefix", str(prefix), "--no-audit", "--no-fund", package], check=True)
    return executable(prefix / "node_modules" / ".bin" / name, name)


def desktop_bundle_root(entry):
    # Paseo's desktop app ships its CLI shim next to an Electron app.asar.
    for directory in entry.parents:
        if (directory / "app.asar").is_file():
            return directory
    return None


def legacy_agent_module(paseo_bin):
    """The Claude provider module of an npm-installed Paseo package, if locatable."""
    entry = Path(paseo_bin).resolve()
    if desktop_bundle_root(entry) is not None:
        return None
    for directory in entry.parents:
        manifest = directory / "package.json"
        try:
            if manifest.is_file() and read_json(manifest).get("name") == "@getpaseo/cli":
                for root in (directory, *directory.parents):
                    package = root / "node_modules" / "@getpaseo" / "server"
                    if package.exists():
                        module = package / LEGACY_AGENT_PATH
                        return module if module.is_file() else None
                return None
        except SetupError:
            return None
    return None


def warn_legacy_patch(paseo_bin):
    """Report a module still carrying the patch of an earlier installer version."""
    module = legacy_agent_module(paseo_bin)
    try:
        patched = module is not None and LEGACY_PATCH_MARKER in module.read_text()
    except (OSError, UnicodeError):
        patched = False
    if patched:
        say(f"Paseo's Claude provider module still carries the source patch of an earlier version of this "
            f"installer: {module}. This installer no longer modifies or restores Paseo's files; reinstall "
            "Paseo at the same version (for example npm install -g @getpaseo/cli@$(paseo --version)) or "
            "restore the oldest agent.js.claude-codex-backup-* beside it, then restart the daemon.")
    return patched


def detect_paseo(explicit, previous=None, data_dir=None):
    # Paseo is managed by the user (installed on request by install_paseo, never
    # here). Never select a cached version from an earlier claude-codex
    # installation over the user's PATH.
    if explicit:
        return executable(explicit, "paseo")
    found = shutil.which("paseo")
    found = str(Path(found).absolute()) if found else None
    if found and desktop_bundle_root(Path(found).resolve()) is None:
        return found
    # PATH offers only the desktop app bundle, whose packed server module cannot
    # be patched, or nothing at all. Reuse the executable selected last time as
    # long as it is still a user-managed package, not a legacy private copy that
    # an older installer cached under the data directory.
    if previous and os.access(previous, os.X_OK) and not Path(previous).is_dir():
        legacy_prefix = (Path(data_dir) / "npm").resolve() if data_dir else None
        resolved = Path(previous).resolve()
        if desktop_bundle_root(resolved) is None and (
                legacy_prefix is None or legacy_prefix not in resolved.parents):
            if found:
                say(f"Paseo on PATH is the desktop app bundle {found}; reusing previously selected {previous}")
            return str(Path(previous).absolute())
    return found


PASEO_NPM_PACKAGE = "@getpaseo/cli"


def paseo_install_command():
    """Paseo's documented install for this platform: the Homebrew cask on macOS, npm elsewhere."""
    brew = shutil.which("brew") if sys.platform == "darwin" else None
    if brew:
        return [brew, "install", "--cask", "paseo"]
    npm = shutil.which("npm")
    if npm:
        return [npm, "install", "-g", f"{PASEO_NPM_PACKAGE}@latest"]
    return None


def wants_paseo_install(command):
    say("Paseo was not detected. It can be installed now with the latest release, using:")
    say("  " + shlex.join(command))
    return ask("Install Paseo now? (Enter for yes; type skip to skip Paseo): ").lower() != "skip"


def install_paseo(command):
    """Run Paseo's installer on this terminal and return the installed executable."""
    say(f"Installing Paseo: {shlex.join(command)}")
    if subprocess.run(command).returncode != 0:
        hint = (" If npm reported a permissions error, give it a user-writable prefix "
                "(npm config set prefix ~/.local) and rerun." if Path(command[0]).name == "npm" else "")
        raise SetupError("Paseo installation failed; see the output above." + hint
                         + " Install Paseo yourself and rerun ./install.sh, or use --skip-paseo.")
    found = shutil.which("paseo")
    if not found and Path(command[0]).name == "npm":
        # The global npm bin directory may not be on PATH yet.
        prefix = subprocess.run([command[0], "prefix", "-g"], capture_output=True, text=True).stdout.strip()
        candidate = Path(prefix) / "bin" / "paseo"
        if prefix and candidate.is_file() and os.access(candidate, os.X_OK):
            say(f"{candidate.parent} is not on PATH; add it to use the paseo command directly.")
            found = str(candidate)
    if not found:
        raise SetupError("Paseo was installed but no paseo executable was found on PATH; open a new terminal and "
                         "rerun ./install.sh, or pass --paseo-bin")
    say(f"Installed Paseo: {found}", "ok")
    return str(Path(found).absolute())


def provider(settings):
    return {
        "extends": "claude",
        "label": "Claude Codex · GPT-6 Astra",
        "description": "Claude Code harness via CLIProxyAPI and ChatGPT subscription OAuth",
        "command": [str(Path(settings["bin_dir"]) / "claude-codex")],
        "enabled": True,
        # No CLAUDE_CONFIG_DIR here: Paseo reloads transcripts from the profile
        # its daemon resolves, so the launcher keeps that profile for Paseo.
        "env": {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{settings['port']}",
            # Advertises gateway authentication to Paseo's availability detection.
            # The launcher replaces this placeholder with the private local key.
            "ANTHROPIC_AUTH_TOKEN": "provided-by-claude-codex-launcher",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(CONTEXT_WINDOW),
            PASEO_MARKER: "1",
        },
        "disallowedTools": ["WebSearch"],
        "models": [{
            "id": model_id(effort),
            "label": f"GPT-6 Astra · {effort}",
            "isDefault": effort == settings["reasoning"],
            "thinkingOptions": ([
                {"id": "default", "label": "Standard", "isDefault": True},
                {"id": "ultracode", "label": "Ultra Code",
                 "description": "Claude Code dynamic workflows with xhigh reasoning"},
            ] if effort == "xhigh" else []),
        } for effort in EFFORTS] + [{
            "id": ULTRACODE_MODEL,
            "label": "GPT-6 Astra · Ultra Code",
            "description": "Claude Code dynamic workflows with xhigh reasoning",
            "isDefault": settings["reasoning"] == "ultracode",
            "thinkingOptions": [{"id": "ultracode", "label": "Ultra Code", "isDefault": True}],
        }],
    }


def peppy_provider(settings):
    return {
        "extends": "claude",
        "label": "Claude Peppy",
        "description": "Second Claude Code account, selected by its own long-lived token",
        "command": [str(Path(settings["bin_dir"]) / "claude-peppy")],
        "enabled": True,
        # No CLAUDE_CONFIG_DIR: Paseo reloads transcripts from the profile its
        # daemon resolves, so the launcher runs Paseo sessions there and selects
        # the account with the saved token instead (never written into Paseo's
        # configuration; the marker tells the launcher it was started by Paseo).
        "env": {PEPPY_PASEO_MARKER: "1"},
    }


def plugin_entry(data_dir):
    return {"source": "directory", "path": str(Path(data_dir) / "paseo-plugin"), "enabled": True}


def plane_connector_module(plane_mcp_config):
    """TypeScript source declaring the installed Plane MCP servers for the plugin.

    Paseo evaluates the compiled plugin from memory, so the definition is
    embedded at install time instead of being read from disk by the plugin.
    Without a configured connection the module exports no servers and the
    plugin leaves agents' MCP servers alone.
    """
    servers = {}
    if plane_mcp_config:
        servers = read_json(plane_mcp_config).get("mcpServers")
        if not isinstance(servers, dict) or any(
                not isinstance(server, dict) or server.get("type") != "stdio"
                or not isinstance(server.get("command"), str)
                or not isinstance(server.get("args"), list)
                or any(not isinstance(arg, str) for arg in server["args"])
                for server in servers.values()):
            raise SetupError(f"Plane MCP configuration {plane_mcp_config} must define stdio servers with command and args")
        servers = {name: {"type": "stdio", "command": server["command"], "args": server["args"]}
                   for name, server in servers.items()}
    # JSON is a TypeScript object literal; ensure_ascii keeps the file 7-bit
    # clean regardless of the path characters in the installation directories.
    literal = json.dumps(servers, indent=2, ensure_ascii=True, sort_keys=True)
    return (
        "// Plane connector definition for Paseo agents. The installer regenerates this\n"
        "// module from the installed plane-mcp.json; the repository copy is the empty\n"
        "// default used when no Plane connection is configured. Do not edit by hand.\n"
        f"export const PLANE_MCP_SERVERS: Readonly<Record<string, PlaneMcpServer>> = {literal};\n"
        "\n"
        "export interface PlaneMcpServer {\n"
        "  type: \"stdio\";\n"
        "  command: string;\n"
        "  args: string[];\n"
        "}\n"
    )


def install_plugin_files(data_dir, plane_mcp_config=None):
    """Copy the repository's Paseo plugin into the data directory.

    The connector module is generated from the installed Plane configuration
    (empty when Plane is not configured) so the plugin can add the read-only
    connector to every Claude Code agent Paseo creates.
    """
    target = Path(data_dir) / "paseo-plugin"
    target.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_FILES:
        atomic_write(target / name, (PLUGIN_SOURCE / name).read_text(), mode=0o644)
    atomic_write(target / PLUGIN_CONNECTOR_FILE, plane_connector_module(plane_mcp_config), mode=0o644)
    managed = {target / name for name in (*PLUGIN_FILES, PLUGIN_CONNECTOR_FILE)}
    for stale in (*target.iterdir(), *(target / "server").iterdir()):
        if stale not in managed and stale.is_file():
            stale.unlink()
    return target


def load_paseo(path):
    config = read_json(path) if path.exists() else {"version": 1}
    if config.get("version", 1) != 1:
        raise SetupError(f"Unsupported Paseo configuration version in {path}")
    agents = config.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise SetupError(f"agents must be an object in {path}")
    providers = agents.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise SetupError(f"agents.providers must be an object in {path}")
    return config


def provider_update_names(include_peppy):
    """Provider keys this installer writes into Paseo's configuration."""
    return ("claude-codex", "claude-peppy") if include_peppy else ("claude-codex",)


def provider_updates(settings, include_peppy):
    builders = {"claude-codex": provider, "claude-peppy": peppy_provider}
    return {name: builders[name](settings) for name in provider_update_names(include_peppy)}


def plugin_conflict(config, path, settings, previous):
    plugins = config.get("plugins")
    if plugins is not None and not isinstance(plugins, dict):
        raise SetupError(f"plugins must be an object in {path}")
    existing = (plugins or {}).get(PLUGIN_ID)
    if existing is None or existing == plugin_entry(settings["data_dir"]):
        return
    if str(path) == previous.get("paseo_config") and isinstance(existing, dict) \
            and existing.get("path") == plugin_entry(previous.get("data_dir", settings["data_dir"]))["path"]:
        return
    raise SetupError(f"{path} already configures an unrelated plugin named {PLUGIN_ID}; "
                     "remove it (paseo plugin remove claude-codex) before installing")


def merge_paseo(path, settings, previous, include_peppy, pull_requests=True):
    config = load_paseo(path)
    providers = config["agents"]["providers"]
    updates = provider_updates(settings, include_peppy)
    for name in updates:
        if name in providers and str(path) != previous.get("paseo_config"):
            raise SetupError(f"{path} already defines {name}; rename that provider before installing")
    plugin_conflict(config, path, settings, previous)
    plugins = config.get("plugins") or {}
    entry = plugin_entry(settings["data_dir"])
    existing = plugins.get(PLUGIN_ID)
    if isinstance(existing, dict) and existing.get("enabled") is False:
        # A deliberate `paseo plugin disable claude-codex` survives reruns.
        entry["enabled"] = False
    daemon = config.get("daemon")
    if daemon is None:
        daemon = {}
    if not isinstance(daemon, dict):
        raise SetupError(f"daemon must be an object in {path}")
    prompt = daemon.get("appendSystemPrompt")
    if pull_requests:
        prompt = pull_request_policy(prompt)
    if all(providers.get(name) == updated for name, updated in updates.items()) \
            and config.get("pluginsEnabled") is True and plugins.get(PLUGIN_ID) == entry \
            and daemon.get("appendSystemPrompt") == prompt:
        return
    providers.update(updates)
    if config.get("pluginsEnabled") is not True:
        say("Enabling Paseo plugins (pluginsEnabled) for the claude-codex plugin.")
        config["pluginsEnabled"] = True
    plugins[PLUGIN_ID] = entry
    config["plugins"] = plugins
    if pull_requests:
        daemon["appendSystemPrompt"] = prompt
        config["daemon"] = daemon
    backup(path)
    write_json(path, config)


LOOPBACK_HOSTS = {"", "127.0.0.1", "localhost", "::1"}
DEFAULT_PASEO_PORT = "6767"


def paseo_listen(config):
    return (config.get("daemon") or {}).get("listen") or f"127.0.0.1:{DEFAULT_PASEO_PORT}"


def split_listen(listen):
    """(host, port) of a TCP listen string, or None for a socket or pipe."""
    if listen.startswith(("/", "~", "unix://", "pipe://", "\\\\.\\pipe\\")):
        return None
    if listen.endswith("]"):  # [::] without a port
        return listen.strip("[]"), DEFAULT_PASEO_PORT
    host, separator, port = listen.rpartition(":")
    if not separator:
        return ("", listen) if listen.isdigit() else (listen, DEFAULT_PASEO_PORT)
    return host.strip("[]"), port


def paseo_listen_update(config, requested):
    """The daemon.listen value to write, or None when it should stay as it is.

    Only a loopback TCP listener is changed: a socket, or an address the user
    chose deliberately (a Tailscale IP, for example), is kept.
    """
    if requested == "keep":
        return None
    current = split_listen(paseo_listen(config))
    if current is None or current[0] not in LOOPBACK_HOSTS:
        return None
    wanted = split_listen(requested)
    if wanted is None or not wanted[1].isdigit():
        raise SetupError("--paseo-listen must be HOST, HOST:PORT, or keep")
    host = wanted[0] or "0.0.0.0"
    explicit_port = requested.isdigit() or (":" in requested and not requested.endswith("]"))
    port = wanted[1] if explicit_port else current[1]
    if host in LOOPBACK_HOSTS and port == current[1]:
        return None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def daemon_path(bin_dir, node_dir, current):
    """PATH for the Paseo daemon: the launchers and Node first, without duplicates."""
    entries = []
    for entry in [str(bin_dir), node_dir, *(current or os.defpath).split(os.pathsep)]:
        if entry and entry not in entries:
            entries.append(entry)
    return os.pathsep.join(entries)


def reload_paseo(paseo_bin, env, timeout=90):
    """Reload the restarted daemon, waiting while it still answers 503 during startup."""
    deadline = time.monotonic() + timeout
    while True:
        result = subprocess.run([paseo_bin, "reload"], env=env, capture_output=True, text=True)
        if result.returncode == 0:
            return
        if time.monotonic() >= deadline:
            output = ((result.stderr or "") + (result.stdout or "")).strip()
            raise SetupError(f"Paseo daemon did not accept a reload within {timeout}s; run paseo-codex reload once "
                             f"it is up.\n{output}")
        time.sleep(1)


def configure_paseo_network(paseo_config, opts):
    """Make the daemon reachable from other devices on the network.

    Paseo validates the Host header (IP addresses always pass) and, by this
    installer's default, runs without a daemon password; `paseo daemon
    set-password` adds one if wanted.
    """
    config = read_json(paseo_config)
    current = paseo_listen(config)
    new_listen = paseo_listen_update(config, opts.paseo_listen)
    if new_listen is None:
        say(f"Paseo daemon listen address unchanged: {current}")
        return
    backup(paseo_config)
    config.setdefault("daemon", {})["listen"] = new_listen
    write_json(paseo_config, config)
    port = split_listen(new_listen)[1]
    say(f"Paseo daemon listens on {new_listen}; other devices connect to this machine's address on port {port}.", "ok")


def write_launcher(path, command, environment=None, path_prepend=None):
    path = Path(path)
    if (path.exists() or path.is_symlink()) and not launcher_is_owned(path):
        raise SetupError(f"Refusing to overwrite an existing unrelated program: {path}")
    lines = ["#!/bin/sh", MARKER]
    for name, value in (environment or {}).items():
        lines.append(f"export {name}={shlex.quote(str(value))}")
    if path_prepend is not None:
        lines.append(f'export PATH={shlex.quote(str(path_prepend))}:"$PATH"')
    lines.append("exec " + shlex.join([str(part) for part in command]) + ' "$@"')
    atomic_write(path, "\n".join(lines) + "\n", mode=0o755)


def add_path(bin_dir):
    home_dir = Path.home()
    shell = Path(os.environ.get("SHELL", "bash")).name
    if shell == "zsh":
        zsh_dir = absolute(os.environ.get("ZDOTDIR", str(home_dir)))
        profiles = [zsh_dir / ".zshrc", zsh_dir / ".zprofile"]
    elif shell == "bash":
        login = next((home_dir / name for name in (".bash_profile", ".bash_login", ".profile")
                      if (home_dir / name).exists()), home_dir / ".profile")
        profiles = [home_dir / ".bashrc", login]
    elif shell == "fish":
        config_dir = absolute(xdg_home("XDG_CONFIG_HOME", ".config"))
        target = config_dir / "fish" / "conf.d" / "claude-codex.fish"
        # POSIX single-quote escaping is also accepted by fish for ordinary paths.
        content = f"{MARKER}\nfish_add_path -- {shlex.quote(str(bin_dir))}\n"
        if not target.exists() or target.read_text() != content:
            backup(target)
            atomic_write(target, content)
        return
    else:
        profiles = [home_dir / ".profile"]
    block = f"\n{MARKER}\nexport PATH={shlex.quote(str(bin_dir))}:\"$PATH\"\n"
    for path in profiles:
        old = path.read_text() if path.exists() else ""
        if block.strip() in old:
            continue
        backup(path)
        # Preserve symlinks used by dotfile managers.
        target = path.resolve() if path.is_symlink() else path
        atomic_write(target, old + block, mode=path.stat().st_mode & 0o777 if path.exists() else 0o644)


def masked_input(prompt):
    """Echo stars on a POSIX terminal, including Python versions before 3.14."""
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    masked = original[:]
    masked[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)
    masked[6] = original[6][:]
    masked[6][termios.VMIN] = 1
    masked[6][termios.VTIME] = 0
    value = []
    try:
        termios.tcsetattr(fd, termios.TCSAFLUSH, masked)
        sys.stderr.write(paint(prompt, "prompt"))
        sys.stderr.flush()
        while True:
            char = sys.stdin.read(1)
            if char in ("\n", "\r"):
                return "".join(value)
            if not char or char == "\x04":
                raise EOFError
            if char == "\x03":
                raise KeyboardInterrupt
            if char in ("\x08", "\x7f"):
                if value:
                    value.pop()
                    sys.stderr.write("\b \b")
            elif char == "\x15":
                sys.stderr.write("\b \b" * len(value))
                value.clear()
            elif char.isprintable():
                value.append(char)
                sys.stderr.write("*")
            sys.stderr.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, original)
        sys.stderr.write("\n")
        sys.stderr.flush()


def plane_key_rejected(api_key, check=True):
    """True only when Plane itself rejects the token.

    A staged install (--skip-login) never asks, and an unreachable or rate
    limited Plane says nothing about the token, so neither counts as a
    rejection: an offline install must still be able to carry a good token.
    """
    if not check:
        return False
    try:
        plane_mcp.verify_api_key(api_key)
    except plane_mcp.AuthError:
        return True
    except plane_mcp.ToolError as exc:
        say(f"Cannot reach Plane to check the token ({exc}); keeping it as provided.", "warn")
    return False


def checked_plane_key(api_key, source, check=True):
    """Fail an explicitly supplied token here rather than on every later read."""
    if plane_key_rejected(api_key, check):
        raise SetupError(f"Plane rejected the token from {source}; create a new one at "
                         f"{PLANE_TOKEN_PAGE}, or rerun with --skip-plane to leave Plane alone")
    return api_key


def prompt_plane_key(check=True):
    """Ask for a token until Plane accepts one, the user skips, or the attempts run out."""
    say("Optional read-only Plane connection: https://app.plane.so/peppy/")
    say(f"Create a token at {PLANE_TOKEN_PAGE} (Add personal access token).")
    say("Use a least-privilege account. Only this connector is read-only; the token itself may have write permissions.")
    say("The token stays in private local storage.")
    for _ in range(PLANE_PROMPT_ATTEMPTS):
        try:
            value = masked_input("Plane API token (masked with *; Enter to skip): ")
        except (EOFError, OSError, ValueError, termios.error):
            raise SetupError("Cannot read a masked Plane token; use --plane-api-key-file or --skip-plane") from None
        if not value.strip():
            return None
        try:
            api_key = plane_mcp.validate_api_key(value)
        except SetupError as exc:
            # Never echoes the token itself, so it is safe to reprompt instead of aborting.
            say(str(exc), "warn")
            continue
        if not plane_key_rejected(api_key, check):
            return api_key
        say("Plane rejected that token; check that it was copied whole and has not been revoked.", "warn")
    say(f"No accepted token after {PLANE_PROMPT_ATTEMPTS} attempts; continuing without a new Plane token.", "warn")
    return None


def prepare_plane(opts, previous, config_dir, data_dir, environment_key):
    """Select credentials, checking them against Plane before anything is written."""
    if opts.skip_plane:
        return None
    credentials = config_dir / "plane-credentials.json"
    mcp_config = config_dir / "plane-mcp.json"
    configured = previous.get("plane_mcp_config")
    if configured and configured != str(mcp_config):
        raise SetupError("Saved Plane configuration is outside this installation; inspect it before reconfiguring")
    managed_paths = [(credentials, configured), (mcp_config, configured),
                     (data_dir / "plane_mcp.py", configured and previous.get("data_dir") == str(data_dir))]
    for path, owned in managed_paths:
        if path.is_symlink() or (path.exists() and not owned):
            raise SetupError(f"Refusing to overwrite an unrelated or symlinked Plane setup file: {path}")
    saved_key = plane_mcp.load_credentials(credentials) if configured and credentials.exists() else None
    # A staged install neither authenticates nor prompts, so it cannot check a token either.
    check = not opts.skip_login
    if opts.plane_api_key_file:
        try:
            value = absolute(opts.plane_api_key_file).read_text()
        except (OSError, UnicodeError):
            raise SetupError("Cannot read --plane-api-key-file; provide a UTF-8 file containing only the token") from None
        return checked_plane_key(plane_mcp.validate_api_key(value), "--plane-api-key-file", check)
    if environment_key is not None:
        return checked_plane_key(plane_mcp.validate_api_key(environment_key), "PLANE_API_KEY", check)
    if configured:
        if saved_key is None:
            raise SetupError("Saved Plane credentials are missing; provide --plane-api-key-file or use --skip-plane")
        if not plane_key_rejected(saved_key, check):
            return saved_key
        # Reusing it silently is what leaves a rotated token failing on every read.
        say("Plane rejected the saved token; it was revoked, expired, or belongs to another account.", "warn")
        if not sys.stdin.isatty():
            say(f"Create a new token at {PLANE_TOKEN_PAGE}, then rerun the installer interactively or with "
                "--plane-api-key-file. The saved token stays in place and Plane reads keep failing.", "warn")
            return None
        return prompt_plane_key(check)
    if opts.skip_login or not sys.stdin.isatty():
        return None
    return prompt_plane_key(check)


def validate_oauth_token(value):
    token = value.strip()
    if not token or any(char.isspace() or not char.isprintable() for char in token):
        raise SetupError("The second account's token must be a single line of printable characters "
                         "as printed by claude setup-token")
    return token


def prepare_peppy_token(opts, previous, environment_token):
    """Select the second account's long-lived token without writing files.

    Interactive runs are not asked here: the token comes from `claude
    setup-token`, which needs the Claude binary this run may still have to
    install, so the sign-in is offered later by acquire_peppy_token.
    """
    if opts.peppy_oauth_token_file:
        try:
            value = absolute(opts.peppy_oauth_token_file).read_text()
        except (OSError, UnicodeError):
            raise SetupError("Cannot read --peppy-oauth-token-file; provide a UTF-8 file containing only the token") from None
        return validate_oauth_token(value)
    if environment_token is not None:
        return validate_oauth_token(environment_token)
    return previous.get("peppy_oauth_token") or None


# Long-lived tokens from claude setup-token; the two digits are the token format version.
OAUTH_TOKEN_START = re.compile(r"sk-ant-oat\d+-")
OAUTH_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-_")
# CSI and OSC sequences, other escapes, and control characters other than newlines.
TERMINAL_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|[\x00-\x08\x0b-\x1f\x7f]")
SETUP_TOKEN_FALLBACK = ("For Paseo: rerun ./install.sh and sign in with the second account when asked, or run "
                        "claude-peppy setup-token and pass the token with --peppy-oauth-token-file.")


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


URL_TAIL = re.compile(rb"[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{1,8}$")


class UrlUnwrapper:
    """Rejoin the tail Claude Code's non-terminal renderer wraps off a long URL.

    When its output is not a terminal, Claude Code wraps the sign-in URL one
    or a few characters early whatever the terminal width, leaving them alone
    on the next line. This holds a line containing a URL until the next one
    arrives and appends such a tail to it. Incomplete lines are held too, so a
    URL arriving in pieces is still recognized; the caller flushes them after
    a short pause so a prompt without a trailing newline still shows.
    """

    IDLE_FLUSH_SECONDS = 0.05

    def __init__(self):
        self.pending = b""
        self.held = None

    @property
    def waiting(self):
        return self.held is not None or bool(self.pending)

    def feed(self, data):
        out = []
        self.pending += data
        while True:
            newline = self.pending.find(b"\n")
            if newline < 0:
                break
            line, self.pending = self.pending[:newline], self.pending[newline + 1:]
            visible = TERMINAL_CONTROL.sub("", line.decode("utf-8", "replace")).strip().encode()
            if self.held is not None:
                out.append(self.held + (line if URL_TAIL.fullmatch(visible) else b"\n" + line) + b"\n")
                self.held = None
            elif b"https://" in visible:
                self.held = line
            else:
                out.append(line + b"\n")
        return b"".join(out)

    def flush(self):
        out = (self.held + b"\n" if self.held is not None else b"") + self.pending
        self.held, self.pending = None, b""
        return out


def run_on_terminal(argv, env, stdin_fd=None, stdout_fd=None):
    """Run an interactive command, relaying it to this terminal and capturing its output.

    The command's input is a pseudo-terminal wired to this one, so browser
    sign-in prompts, pasted codes, and Ctrl-C behave normally. Its output is a
    pipe, echoed here as it arrives: Claude Code's screen renderer only redraws
    changed cells when it writes to a terminal, so what it shows can then not
    be reassembled from the bytes it sends, while on a pipe it prints each
    screen as plain lines. Returns the exit status and the captured output.
    """
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    stdout_fd = sys.stdout.fileno() if stdout_fd is None else stdout_fd
    master, slave = pty.openpty()

    def resize(*_):
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\0" * 8))
        except OSError:
            pass

    def become_terminal_owner():  # pragma: no cover - child process
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except OSError:
            pass

    resize()
    child = subprocess.Popen(argv, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             start_new_session=True, preexec_fn=become_terminal_owner)
    os.close(slave)
    output = child.stdout.fileno()
    captured = []
    display = UrlUnwrapper()
    original_handler = signal.signal(signal.SIGWINCH, resize)
    original_mode = termios.tcgetattr(stdin_fd)
    # Pass keys through unbuffered and unechoed, but keep output processing so
    # the relayed screens' newlines still return to the first column.
    relay_mode = original_mode[:]
    relay_mode[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
    relay_mode[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
    relay_mode[6] = original_mode[6][:]
    relay_mode[6][termios.VMIN] = 1
    relay_mode[6][termios.VTIME] = 0
    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, relay_mode)
    try:
        watched = [output, master, stdin_fd]
        interrupts = 0
        while output in watched:
            ready, _, _ = select.select(watched, [], [], display.IDLE_FLUSH_SECONDS if display.waiting else None)
            if not ready:
                write_all(stdout_fd, display.flush())
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:  # Linux reports EIO on a pseudo-terminal whose other side closed.
                    data = b""
                if not data:
                    watched.remove(fd)
                elif fd == stdin_fd:
                    try:
                        write_all(master, data)
                    except OSError:  # Typed after the command closed its terminal.
                        pass
                    if b"\x03" in data:
                        # The command reads raw keys, so its terminal generates no
                        # signal for Ctrl-C; deliver one (twice: terminate).
                        interrupts += 1
                        child.send_signal(signal.SIGINT if interrupts == 1 else signal.SIGTERM)
                elif fd == output:
                    captured.append(data)
                    write_all(stdout_fd, display.feed(data))
                else:
                    write_all(stdout_fd, data)
        write_all(stdout_fd, display.flush())
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, original_mode)
        signal.signal(signal.SIGWINCH, original_handler)
        os.close(master)
        child.stdout.close()
    return child.wait(), b"".join(captured)


def printed_oauth_tokens(output):
    """Tokens shown by `claude setup-token` in its captured output.

    Claude Code prints the token as a paragraph of its own, separated from the
    surrounding text by blank lines, and may hard-wrap it. Continuation lines
    are joined up to the next blank line. Each screen is printed in full, so a
    token can repeat; the result is a set.
    """
    text = TERMINAL_CONTROL.sub("", output.decode("utf-8", "replace"))
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    tokens = set()
    for index, line in enumerate(lines):
        match = OAUTH_TOKEN_START.search(line)
        if match is None:
            continue
        token = line[match.start():]
        for continuation in lines[index + 1:]:
            if not continuation:
                break
            token += continuation
        if set(token) <= OAUTH_TOKEN_CHARS:
            tokens.add(token)
    return tokens


def sign_in_second_account(settings):
    """Run `claude setup-token` for the second account and return the token it prints."""
    source = {name: value for name, value in os.environ.items() if name != PEPPY_PASEO_MARKER}
    status, output = run_on_terminal([settings["claude_bin"], "setup-token"], peppy_env(settings, source))
    tokens = printed_oauth_tokens(output)
    if status != 0 or not tokens:
        raise SetupError("claude setup-token did not print a token")
    if len(tokens) > 1:
        raise SetupError("claude setup-token printed more than one token")
    return validate_oauth_token(tokens.pop())


def acquire_peppy_token(settings):
    """Interactively obtain the token for the second account's Paseo sessions.

    Called once the Claude binary and the launcher exist, so the sign-in can
    happen in the same run. Returns None when the user skips or the sign-in
    fails; the installation is complete either way and the fallback is printed.
    """
    say("In Paseo, 'Claude Peppy' agents run as your second Claude account. To sign in as that account,")
    say("Paseo needs a long-lived Claude Code token (what `claude setup-token` prints; valid for one year).")
    say("You do not need to have one yet: the installer can create it for you now.")
    say("  Enter      sign in as the second account in the browser now; the token is created and saved")
    say("  paste      if you already ran `claude-peppy setup-token`, paste its token (it starts with sk-ant-oat01-)")
    say("  skip       set this up later by rerunning ./install.sh")
    say("The token is stored only in this installation's private settings, never in Paseo's configuration.")
    try:
        value = masked_input("Second account token (Enter to sign in now, paste a token, or type skip): ").strip()
    except (EOFError, OSError, ValueError, termios.error):
        raise SetupError("Cannot read a masked token; use --peppy-oauth-token-file or CLAUDE_PEPPY_OAUTH_TOKEN") from None
    if value.lower() == "skip":
        return None
    if value:
        return validate_oauth_token(value)
    say("Sign in with the second account in the browser that opens, or at the printed URL.")
    try:
        token = sign_in_second_account(settings)
    except SetupError as exc:
        say(f"{exc}; no token was saved for the second account's Paseo sessions.", "warn")
        say(SETUP_TOKEN_FALLBACK, "warn")
        return None
    say("Saved the second account's token for Paseo.", "ok")
    return token


def ask(prompt):
    """Read one line of plain (non-secret) input from the terminal; EOF counts as an empty answer."""
    sys.stderr.write(paint(prompt, "prompt"))
    sys.stderr.flush()
    line = sys.stdin.readline()
    return line.strip()


CODEX_LOGIN_SKIPPED = ("Claude Codex sign-in skipped: claude-codex and the Paseo 'Claude Codex' provider cannot run "
                       "until you sign in with: claude-codex-proxy login")


def wants_codex_login():
    """Ask an interactive user whether to sign in with ChatGPT for Claude Codex now."""
    say("Claude Codex runs Claude Code with GPT-6 Astra through a ChatGPT subscription and needs a ChatGPT sign-in.")
    say("Press Enter to sign in now, or type skip to leave it for later (claude-codex-proxy login).")
    return ask("Sign in with ChatGPT for Claude Codex now? (Enter for yes; type skip to skip): ").lower() != "skip"


def configure_plane(config_dir, data_dir, api_key):
    credentials = config_dir / "plane-credentials.json"
    helper = data_dir / "plane_mcp.py"
    mcp_config = config_dir / "plane-mcp.json"
    atomic_write(helper, Path(__file__).with_name("plane_mcp.py").read_text())
    if not credentials.exists() or plane_mcp.load_credentials(credentials) != api_key:
        # Do not leave backup copies of rotated tokens behind.
        write_json(credentials, {"workspace": "peppy", "api_key": api_key})
    credentials.chmod(0o600)
    write_json(mcp_config, {"mcpServers": {plane_mcp.SERVER_NAME: {
        "type": "stdio", "command": str(Path(sys.executable).absolute()),
        "args": [str(helper), "--credentials", str(credentials)],
    }}})
    return str(mcp_config)


def parser():
    p = argparse.ArgumentParser(description="Install claude-codex and CLIProxyAPI; configure Paseo only if detected")
    p.add_argument("--reasoning", choices=REASONING_MODES, help="Default reasoning level (first install: high)")
    p.add_argument("--port", type=int, help="Local proxy port (first install: 8317)")
    p.add_argument("--config-dir", default=default_dir("XDG_CONFIG_HOME", ".config", "claude-codex"))
    p.add_argument("--data-dir", default=default_dir("XDG_DATA_HOME", ".local/share", "claude-codex"))
    p.add_argument("--state-dir", default=default_dir("XDG_STATE_HOME", ".local/state", "claude-codex"))
    p.add_argument("--bin-dir", default=str(Path.home() / ".local/bin"))
    p.add_argument("--paseo-home", default=os.environ.get("PASEO_HOME", str(Path.home() / ".paseo")))
    p.add_argument("--claude-bin", help="Existing Claude executable to use")
    p.add_argument("--paseo-bin", help="Existing Paseo executable to use")
    peppy = p.add_mutually_exclusive_group()
    peppy.add_argument("--peppy-config-dir", help="Profile directory for the second Claude account (first install: ~/.claude-peppy)")
    peppy.add_argument("--skip-peppy", action="store_true",
                       help="Skip the second-account launcher and Paseo provider, retaining any existing ones")
    p.add_argument("--peppy-oauth-token-file",
                   help="File containing the second account's long-lived token from claude setup-token, "
                        "used for its Paseo sessions (or set CLAUDE_PEPPY_OAUTH_TOKEN)")
    p.add_argument("--proxy-binary", help="Use an existing trusted CLIProxyAPI binary instead of downloading")
    p.add_argument("--proxy-version", default=PROXY_VERSION)
    p.add_argument("--device-login", action="store_true", help="Use ChatGPT device-code login")
    p.add_argument("--no-browser", action="store_true", help="Print OAuth URL without opening a browser")
    p.add_argument("--skip-login", action="store_true", help="Stage installation without authenticating or prompting for Plane credentials")
    p.add_argument("--skip-codex-login", action="store_true",
                   help="Skip the ChatGPT sign-in and live verification for Claude Codex; the other steps still run")
    plane = p.add_mutually_exclusive_group()
    plane.add_argument("--plane-api-key-file", help="File containing a Plane personal API token for read-only peppy access (or set PLANE_API_KEY)")
    plane.add_argument("--skip-plane", action="store_true", help="Skip Plane setup, preserving any previously configured connection")
    p.add_argument("--skip-smoke-test", action="store_true", help="Skip the small live Astra verification request")
    paseo_group = p.add_mutually_exclusive_group()
    paseo_group.add_argument("--skip-paseo", action="store_true", help="Install terminal integration only")
    paseo_group.add_argument("--install-paseo", action="store_true",
                             help="When no Paseo is detected, install the latest release without asking "
                                  "(Homebrew cask on macOS, npm install -g elsewhere)")
    p.add_argument("--skip-paseo-start", action="store_true", help="Write Paseo configuration without restarting its daemon")
    p.add_argument("--skip-pull-requests", action="store_true",
                   help="Leave Paseo's daemon.appendSystemPrompt alone instead of adding the pull request policy")
    p.add_argument("--paseo-listen", default="0.0.0.0", metavar="HOST[:PORT]",
                   help="Where the Paseo daemon listens when it is still on loopback (default: 0.0.0.0, all interfaces, "
                        "keeping the port). Use 'keep' to leave it unchanged")
    p.add_argument("--no-path", action="store_true", help="Do not edit shell startup files")
    return p


def install(opts):
    # Input credentials must not be inherited by npm, the proxy, or the daemon.
    plane_environment_key = os.environ.pop("PLANE_API_KEY", None)
    peppy_environment_token = os.environ.pop("CLAUDE_PEPPY_OAUTH_TOKEN", None)
    if sys.version_info < (3, 9):
        raise SetupError("Python 3.9+ is required")
    if sys.platform not in ("linux", "darwin"):
        raise SetupError("Run this installer on Linux/WSL2 or macOS")
    if not opts.proxy_binary and not shutil.which("curl"):
        raise SetupError("curl is required to download CLIProxyAPI")
    config_dir, data_dir, state_dir, bin_dir = (
        absolute(getattr(opts, key)) for key in ("config_dir", "data_dir", "state_dir", "bin_dir")
    )
    with file_lock(config_dir / "install.lock"):
        return _install_locked(opts, config_dir, data_dir, state_dir, bin_dir, plane_environment_key,
                               peppy_environment_token)


def _install_locked(opts, config_dir, data_dir, state_dir, bin_dir, plane_environment_key, peppy_environment_token):
    settings_file = config_dir / "settings.json"
    previous = read_json(settings_file) if settings_file.exists() else {}
    port = opts.port if opts.port is not None else previous.get("port", 8317)
    if not 1024 <= port <= 65535:
        raise SetupError("--port must be between 1024 and 65535")
    use_peppy = not opts.skip_peppy
    peppy_dir = absolute(opts.peppy_config_dir or previous.get("peppy_config_dir")
                         or Path.home() / ".claude-peppy")
    # The second account exists to be distinguishable from the primary profile,
    # and must not silently share the installer's isolated gateway profile.
    if use_peppy and peppy_dir in (absolute(Path.home() / ".claude"), absolute(config_dir / "claude")):
        raise SetupError("--peppy-config-dir must not be the primary Claude profile ~/.claude "
                         "or the installer's isolated Claude profile")
    step("Paseo")
    detected_paseo = None if opts.skip_paseo else detect_paseo(opts.paseo_bin, previous.get("paseo_bin"), data_dir)
    paseo_skip_reason = "Paseo integration skipped" if opts.skip_paseo else "Paseo not detected"
    if detected_paseo is None and not opts.skip_paseo:
        command = paseo_install_command()
        if command is None:
            paseo_skip_reason += " and neither Homebrew nor npm is available to install it"
        elif opts.install_paseo or (not opts.skip_login and sys.stdin.isatty() and wants_paseo_install(command)):
            detected_paseo = install_paseo(command)
        elif not opts.install_paseo and (opts.skip_login or not sys.stdin.isatty()):
            paseo_skip_reason += " (pass --install-paseo to install it)"
    if detected_paseo:
        assert_original_binary(detected_paseo, bin_dir, ("paseo-codex",), "--paseo-bin", "Paseo")
        say(f"Using installed Paseo: {detected_paseo}", "ok")
    use_paseo = detected_paseo is not None
    if use_peppy and not use_paseo and previous.get("peppy_config_dir"):
        # A retained Paseo provider entry keeps pinning the old profile; moving
        # the launcher without updating it would split live sessions from replay.
        retained_config = Path(previous.get("paseo_config") or "")
        if absolute(previous["peppy_config_dir"]) != peppy_dir and retained_config.is_file():
            try:
                retained = read_json(retained_config)
                retained_providers = retained.get("agents", {}).get("providers", {})
            except SetupError:
                retained_providers = {}
            if isinstance(retained_providers, dict) and "claude-peppy" in retained_providers:
                raise SetupError("--peppy-config-dir changed, but this run leaves Paseo's configuration "
                                 "untouched; rerun with Paseo detected so the claude-peppy provider entry "
                                 "can be updated, or remove that entry first")
    if not use_paseo:
        say(f"{paseo_skip_reason}; skipping Paseo configuration", "warn")
    paseo_home = absolute(opts.paseo_home)
    paseo_config = paseo_home / "config.json"
    if use_paseo:
        config = load_paseo(paseo_config)
        providers = config["agents"]["providers"]
        for name in provider_update_names(use_peppy):
            if name in providers and str(paseo_config) != previous.get("paseo_config"):
                raise SetupError(f"{paseo_config} already has an unrelated {name} provider")
    for path in (config_dir, data_dir, state_dir, config_dir / "auth", config_dir / "claude"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    if use_peppy:
        # A profile the user already prepared keeps its own permissions.
        peppy_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher_names = ["claude-codex", "claude-codex-proxy"] + (["claude-peppy"] if use_peppy else []) \
        + (["paseo-codex", WORKTREE_SETUP_COMMAND] if use_paseo else [])
    for name in launcher_names:
        path = bin_dir / name
        if (path.exists() or path.is_symlink()) and not launcher_is_owned(path):
            raise SetupError(f"Refusing to overwrite unrelated program {path}")
    # Validate before downloads, stopping services, or replacing configuration.
    step("Plane read-only connection")
    plane_key = prepare_plane(opts, previous, config_dir, data_dir, plane_environment_key)
    if plane_key is not None:
        say("Plane read-only connector for peppy: configured with this installation.", "ok")
    elif previous.get("plane_mcp_config"):
        say("Plane setup skipped; the existing connection and credentials are retained.")
    else:
        say("Plane setup skipped; no connection configured. Rerun interactively or use --plane-api-key-file.", "warn")
    peppy_token = prepare_peppy_token(opts, previous, peppy_environment_token) if use_peppy else None
    step("Installing Claude Code, CLIProxyAPI, and the launchers")
    claude_bin = resolve_cli("claude", opts.claude_bin, f"@anthropic-ai/claude-code@{CLAUDE_VERSION}", data_dir, previous.get("claude_bin"))
    assert_original_binary(claude_bin, bin_dir, ("claude-codex", "claude-peppy"), "--claude-bin", "Claude")
    paseo_bin = detected_paseo
    proxy_bin = executable(opts.proxy_binary, "CLIProxyAPI") if opts.proxy_binary else str(install_proxy(data_dir, opts.proxy_version))
    settings = {
        "config_dir": str(config_dir), "data_dir": str(data_dir), "state_dir": str(state_dir),
        "bin_dir": str(bin_dir), "port": port, "proxy_bin": proxy_bin, "claude_bin": claude_bin,
        "api_key": previous.get("api_key") or secrets.token_urlsafe(32),
        "reasoning": opts.reasoning or previous.get("reasoning", "high"),
        "node_dir": str(Path(shutil.which("node")).parent) if shutil.which("node") else None,
        "proxy_version": opts.proxy_version if not opts.proxy_binary else "external",
    }
    if paseo_bin:
        settings.update(paseo_bin=paseo_bin, paseo_config=str(paseo_config))
    elif previous.get("paseo_config"):
        settings.update(paseo_config=previous["paseo_config"], paseo_bin=previous.get("paseo_bin"))
    if use_peppy:
        settings["peppy_config_dir"] = str(peppy_dir)
        if peppy_token:
            settings["peppy_oauth_token"] = peppy_token
    elif previous.get("peppy_config_dir"):
        # Keep an already-installed launcher working across a --skip-peppy run.
        settings["peppy_config_dir"] = previous["peppy_config_dir"]
        if previous.get("peppy_oauth_token"):
            settings["peppy_oauth_token"] = previous["peppy_oauth_token"]
    if use_paseo:
        warn_legacy_patch(paseo_bin)
    # Stop only the previously managed process, using its old binary and port.
    if previous:
        Runtime(previous).stop()
    runtime_file = data_dir / "claude_codex.py"
    atomic_write(runtime_file, Path(__file__).with_name("claude_codex.py").read_text())
    if plane_key is not None:
        settings["plane_mcp_config"] = configure_plane(config_dir, data_dir, plane_key)
    elif previous.get("plane_mcp_config"):
        settings["plane_mcp_config"] = previous["plane_mcp_config"]
    write_json(settings_file, settings)
    write_json(config_dir / "proxy.yaml", proxy_config(settings))
    # Keep the native claude profile untouched. Project CLAUDE.md and settings
    # still load through Claude Code's normal project settings mechanism.
    claude_settings = config_dir / "claude" / "settings.json"
    if not claude_settings.exists():
        write_json(claude_settings, {"permissions": {"deny": ["WebSearch"]}})
    python_bin = str(Path(sys.executable).absolute())
    for name, mode in (("claude-codex", "launch"), ("claude-codex-proxy", "proxy")):
        write_launcher(bin_dir / name, [python_bin, runtime_file, mode, settings_file])
    if use_peppy:
        write_launcher(bin_dir / "claude-peppy", [python_bin, runtime_file, "peppy", settings_file])
    if paseo_bin:
        install_plugin_files(data_dir, settings.get("plane_mcp_config"))
        merge_paseo(paseo_config, settings, previous, use_peppy, pull_requests=not opts.skip_pull_requests)
        paseo_env = {"PASEO_HOME": str(paseo_home)}
        write_launcher(bin_dir / "paseo-codex", [paseo_bin], paseo_env, path_prepend=settings["node_dir"])
        # Run by Paseo from a repository's paseo.json (worktree.setup) right
        # after it creates a worktree; `init` registers it in a repository.
        write_launcher(bin_dir / WORKTREE_SETUP_COMMAND, [python_bin, runtime_file, "worktree-setup", settings_file])
    if not opts.no_path:
        add_path(bin_dir)
    runtime = Runtime(settings)
    codex_login_skipped = False
    if not opts.skip_login:
        step("Claude Codex: ChatGPT sign-in and verification")
        # An existing ChatGPT login is verified without asking; a first sign-in
        # can be declined, leaving Claude Codex staged and the other steps running.
        if runtime.has_login():
            codex_login = True
        elif opts.skip_codex_login:
            codex_login = False
        else:
            codex_login = not sys.stdin.isatty() or wants_codex_login()
        if codex_login:
            if not runtime.has_login():
                runtime.login(opts.device_login, opts.no_browser)
            runtime.doctor(smoke=not opts.skip_smoke_test)
        else:
            codex_login_skipped = True
            say(CODEX_LOGIN_SKIPPED, "warn")
        if use_paseo and use_peppy and not peppy_token and sys.stdin.isatty():
            step("Second Claude account: token for Paseo sessions")
            peppy_token = acquire_peppy_token(settings)
            if peppy_token:
                settings["peppy_oauth_token"] = peppy_token
                write_json(settings_file, settings)
    if paseo_bin:
        env = dict(os.environ)
        env["PASEO_HOME"] = str(paseo_home)
        # Explicitly target this machine; ignore inherited remote daemon selectors.
        env.pop("PASEO_HOST", None)
        # The daemon inherits this environment and runs paseo.json worktree
        # commands with it, so the launchers must be on its PATH even when the
        # shell that ran the installer does not have the bin directory yet.
        env["PATH"] = daemon_path(bin_dir, settings.get("node_dir"), env.get("PATH"))
        step("Paseo daemon: network access and restart")
        configure_paseo_network(paseo_config, opts)
        if opts.skip_paseo_start:
            say("Paseo daemon not restarted (--skip-paseo-start); run paseo-codex daemon restart to apply the "
                "configuration.", "warn")
        else:
            say(f"Restarting the local Paseo daemon with {paseo_bin}...")
            subprocess.run([paseo_bin, "daemon", "restart"], env=env, check=True)
            reload_paseo(paseo_bin, env)
    step("Done")
    say(f"Installed: {bin_dir / 'claude-codex'}", "ok")
    if paseo_bin:
        say(f"Paseo worktrees: run {bin_dir / WORKTREE_SETUP_COMMAND} init <repo> and commit paseo.json so new "
            "worktrees start from the latest default branch on origin.")
    say(f"Run: {shlex.quote(str(bin_dir / 'claude-codex'))} --reasoning high")
    if use_peppy:
        say(f"Second account: {bin_dir / 'claude-peppy'} (profile {peppy_dir}); run it once to sign in.")
        if paseo_bin and not peppy_token:
            say("No token is saved for the second account's Paseo sessions; the Claude Peppy provider refuses "
                "to start sessions until one is.", "warn")
            say(SETUP_TOKEN_FALLBACK, "warn")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        say(f'For this terminal: export PATH={shlex.quote(str(bin_dir))}:"$PATH" (or open a new terminal)')
    if paseo_bin:
        say("In Paseo, select 'Claude Codex · GPT-6 Astra', then select a reasoning variant in the model picker.")
        if use_peppy:
            say("In Paseo, select 'Claude Peppy' to use the second Claude account.")
        say("Paseo plugin 'claude-codex' installed; check it with: paseo-codex plugin ls")
    if codex_login_skipped:
        say(CODEX_LOGIN_SKIPPED, "warn")
    if opts.skip_login:
        say("Installation staged; login and live verification were skipped. Run claude-codex-proxy login when ready.", "warn")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        install(parser().parse_args())
    except (SetupError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        say(f"Install failed: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        say("Interrupted. Rerun install.sh to continue.")
        sys.exit(130)

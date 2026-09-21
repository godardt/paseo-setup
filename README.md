# Claude Codex

Run **Claude Code with GPT-6 Astra** on a ChatGPT subscription, through a local [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) Codex OAuth proxy. Optionally adds Astra and a second Claude account as providers to [Paseo](https://paseo.sh).

```text
Terminal: claude-codex ──────────┐
                               ├─ Claude Code → local CLIProxyAPI → Codex OAuth → GPT-6 Astra
Paseo: Claude Codex provider ────┘
```

## Quickstart

```bash
git clone https://github.com/godardt/paseo-setup.git && cd paseo-setup
bash install.sh          # Sign in with ChatGPT when prompted
claude-codex             # Claude Code on GPT-6 Astra
```

Paseo users: pick **Claude Codex · GPT-6 Astra** when creating an agent.

The installer is a Python standard-library script. It never modifies your `claude` binary, `~/.claude`, or Paseo's own files. It installs:

- **`claude-codex`** — Claude Code mapped to Astra at a chosen reasoning level, in an isolated profile. Starts the proxy on demand.
- **`claude-codex-proxy`** — proxy start/stop, ChatGPT login, diagnostics, default reasoning.
- **`claude-peppy`** — a second, regular Claude Code account in `~/.claude-peppy`. No proxy.
- **`paseo-codex`** (Paseo only) — your Paseo executable with the configured `PASEO_HOME`.
- **`paseo-codex-worktree-setup`** (Paseo only) — worktree setup command for `paseo.json`: new worktrees start from the latest default branch on `origin`.
- **Paseo plugin** `claude-codex` (Paseo 0.8+) — adds the Plane connector to every Claude Code and Codex agent and keeps Claude Codex agents out of auto mode via the `agent.create` hook.

## Requirements

- Linux (incl. WSL2) or macOS, x86_64 or ARM64.
- Python 3.9+, `curl`.
- Claude Code, or Node.js 22+ to install it privately.
- A ChatGPT subscription with **`gpt-6-astra` access**. No OpenAI API key needed.
- Optional: Paseo 0.8+ on PATH (or `--paseo-bin`). If missing, the installer offers to install it.

## Install

```bash
bash install.sh
```

Prompts, in order (Enter to accept, `skip` to skip):

1. Install Paseo if none is found.
2. Plane personal API token for read-only access to [peppy](https://app.plane.so/peppy/).
3. ChatGPT sign-in for Claude Codex (later: `claude-codex-proxy login`).
4. Second-account sign-in for Paseo sessions (later: rerun `install.sh`).

The installer downloads CLIProxyAPI 7.2.155 (checksum verified), installs the launchers into `~/.local/bin`, adds it to your shell PATH, sends one small live request to verify Astra access, and, when Paseo is present, merges the providers, plugin, and pull request policy into `$PASEO_HOME/config.json` (backup kept), opens the daemon to the LAN, and restarts it with the launchers on its PATH. Finish active Paseo sessions first.

Rerun `install.sh` after pulling updates. Tokens, OAuth credentials, and settings are preserved. Upgrading Paseo needs no rerun.

### Options

```bash
bash install.sh --reasoning max                 # Initial default reasoning
bash install.sh --device-login                  # Headless host
bash install.sh --no-browser                    # Print the OAuth URL
bash install.sh --port 18317 --paseo-home ~/.paseo-work
bash install.sh --claude-bin /path --paseo-bin /path
bash install.sh --install-paseo                 # Install Paseo without asking
bash install.sh --skip-paseo                    # Terminal only
bash install.sh --skip-peppy                    # No second account
bash install.sh --peppy-config-dir ~/.claude-work
bash install.sh --peppy-oauth-token-file FILE   # or CLAUDE_PEPPY_OAUTH_TOKEN=...
bash install.sh --plane-api-key-file FILE       # or PLANE_API_KEY=...
bash install.sh --skip-plane                    # Keeps an existing connection
bash install.sh --paseo-listen HOST[:PORT]      # Daemon address; `keep` leaves it alone
bash install.sh --skip-pull-requests            # Leave daemon.appendSystemPrompt alone
bash install.sh --skip-codex-login --skip-smoke-test --skip-paseo-start --no-path
bash install.sh --proxy-version X.Y.Z | --proxy-binary /path
bash install.sh --bin-dir --config-dir --data-dir --state-dir
bash install.sh --help
```

Reuse the same directory options on later runs. Set `NO_COLOR=1` to disable colored output.

## Terminal

```bash
claude-codex                          # Default: high
claude-codex --reasoning low|medium|high|xhigh|max
claude-codex --reasoning ultracode    # xhigh + Claude workflow orchestration
claude-codex -p 'Explain this project'
claude-codex-proxy reasoning xhigh    # Persistent default
CLAUDE_CODEX_REASONING=medium claude-codex
```

All Claude Code arguments are forwarded. `--model 'gpt-6-astra(max)'` also works; conflicting suffix and `--reasoning` are rejected. Use `--` before prompt text that looks like an option. All model mappings (Opus, Sonnet, Haiku, subagents) target Astra at the launch effort. The context window is set to Astra's 1,050,000 tokens.

**Auto mode is disabled** (`permissions.disableAutoMode`): its classifier cannot run on this gateway, so Astra would judge its own tool calls and deny ordinary ones. Use `acceptEdits` or `bypassPermissions` for fewer prompts. `CLAUDE_CODEX_AUTO_MODE=1` re-enables it.

The proxy starts on demand and keeps running after sessions exit. No systemd/launchd service.

### Second account

```bash
claude-peppy            # First run: sign in
claude-peppy --resume
```

Runs in `~/.claude-peppy` with ambient routing variables (`ANTHROPIC_*`, model overrides, Bedrock/Vertex switches) scrubbed. Configure it via `~/.claude-peppy/settings.json`. Receives the same Plane connector as `claude-codex`.

## Paseo

Providers added to `config.json`:

- **Claude Codex · GPT-6 Astra** — model picker lists the five reasoning levels plus **GPT-6 Astra · Ultra Code** (xhigh + workflow mode).
- **Claude Peppy** — Paseo's regular Claude provider, signed in with the second account via a long-lived `claude setup-token` token (~1 year, Pro/Max/Team/Enterprise). Stored in the installer's `settings.json`, injected as `CLAUDE_CODE_OAUTH_TOKEN` at launch. When it expires, rerun `install.sh` after removing `peppy_oauth_token` from `settings.json`, or pass `--peppy-oauth-token-file`.

Both run in the **daemon's Claude profile** (its `CLAUDE_CONFIG_DIR` or `~/.claude`), because Paseo reloads transcripts from there. Conversations survive restarts; the daemon profile's settings, hooks, and plugins apply.

The plugin adds the Plane connector (below) to every agent of the `claude`, `claude-codex`, `claude-peppy`, and `codex` providers, and rewrites an explicit Auto-mode request for the Claude Codex provider to Always Ask. To keep auto mode, add `"CLAUDE_CODEX_AUTO_MODE": "1"` to the provider's `env` and run `paseo-codex plugin disable claude-codex`.

### Worktrees and pull requests

Paseo cuts a new worktree from your **local** default branch, which is only as current as your last pull. The installer provides a [`paseo.json`](https://paseo.sh/docs/worktrees) worktree setup command that fetches `origin` right after the worktree is created and moves the fresh branch onto `origin`'s copy of its base branch (the default branch unless another base was chosen). Register it once per repository and commit the file on the default branch:

```bash
paseo-codex-worktree-setup init /path/to/repo   # writes {"worktree": {"setup": "paseo-codex-worktree-setup"}}
```

A checked-out existing branch is fast-forwarded to its `origin` counterpart; a branch with its own commits, a diverged branch, or a pull request checkout is left alone. A failed fetch fails the setup, so the worktree is never silently stale. The daemon must have `~/.local/bin` on its PATH; the installer restarts it that way.

The installer also appends a **pull request policy** to `daemon.appendSystemPrompt` in Paseo's `config.json`, between `[claude-codex pull-request policy]` markers so reruns update it and your own text around it stays. Agents on a worktree or non-default branch finish a completed task by committing, pushing, and opening a pull request against the default branch with `gh pr create`, reporting its URL, unless one already exists or you asked otherwise. Requires an authenticated `gh` on the daemon host. `--skip-pull-requests` leaves the prompt alone.

**Network:** a loopback listener is switched to `0.0.0.0:<port>` so phones and other machines can connect. No password is set; use `paseo-codex daemon set-password` on untrusted networks. Unix-socket or non-loopback listeners are left alone.

**Known limits** (Paseo's built-in Claude provider, not fixable by plugin): context usage updates only at turn end, and `context: fork` skills show as running until daemon restart.

```bash
paseo-codex provider diagnostic claude-codex
paseo-codex run --provider claude-codex 'Explain this project'
paseo-codex plugin ls
paseo-codex plugin logs claude-codex
```

Install on each daemon host. If an earlier version of this installer patched Paseo's `agent.js`, the installer reports it; reinstall Paseo at the same version.

## Plane (read-only)

Create a token at [Plane's API tokens page](https://app.plane.so/settings/profile/api-tokens). The installer stores it in a `0600` file and registers a GET-only MCP server, `plane-peppy-readonly`, exposing `list_projects`, `list_work_items`, and `get_work_item` for the `peppy` workspace. Mutations, redirects, and arbitrary paths are rejected in code. The token itself is not read-only; use a least-privilege account.

Its name is the key the launchers and the plugin merge into a session's MCP servers, so a server you configure yourself under that name takes precedence and the connector is left out.

Terminal `claude-codex` and `claude-peppy` sessions receive it through `--mcp-config`. Paseo agents receive it from the plugin, which adds the server to every new agent of the `claude`, `claude-codex`, `claude-peppy`, and `codex` providers (Paseo's built-in Claude and Codex providers never run the launchers). Agents created before the plugin was installed keep their old server list; start a new agent.

The installer checks the token with one read-only call before writing anything. A token it supplies (`--plane-api-key-file`, `PLANE_API_KEY`) that Plane rejects fails the install; a saved token that Plane rejects is reported and replaced by prompt, so rerunning `install.sh` is how you rotate a revoked or expired one. When Plane is unreachable the token is kept as provided, and `--skip-login` stages the install without contacting Plane at all.

Test it: ask an agent to "list the projects in peppy". Pass `--strict-mcp-config` to suppress injection in the terminal.

## Diagnostics

```bash
claude-codex-proxy status
claude-codex-proxy doctor [--smoke-test]
claude-codex-proxy login [--device]
claude-codex-proxy restart | stop | paths
```

| Path | Contents |
| --- | --- |
| `~/.config/claude-codex/settings.json` | Launcher settings, local API token, second-account token |
| `~/.config/claude-codex/proxy.yaml` | CLIProxyAPI config |
| `~/.config/claude-codex/plane-credentials.json` | Plane token |
| `~/.config/claude-codex/auth/` | ChatGPT OAuth credentials |
| `~/.config/claude-codex/claude/` | Isolated Claude profile for terminal `claude-codex` |
| `~/.claude-peppy/` | Second account profile for terminal `claude-peppy` |
| `~/.local/share/claude-codex/` | Runtime, proxy binary, Paseo plugin, private npm CLIs |
| `~/.local/state/claude-codex/` | PID, lock, logs |
| `~/.paseo/config.json` | Merged providers and plugin, backup beside it |

Troubleshooting:

- **Port occupied:** `--port 18317`.
- **401 / missing login:** `claude-codex-proxy login`. Headless: `--device`. Over SSH, forward port 1455.
- **403 / quota / Astra unavailable:** check the account's Astra access; `doctor --smoke-test`.
- **Installer waits:** another run holds `install.lock`. Don't delete it.
- **Executable rejected as recursive:** point `--claude-bin`/`--paseo-bin` at the original, not a wrapper.
- **Paseo provider absent:** `paseo-codex reload`, then the diagnostic above.
- **Plugin `failed`:** `paseo-codex plugin logs claude-codex`. Needs Paseo 0.8+.
- **Tool "denied by the auto mode classifier":** old session or `CLAUDE_CODEX_AUTO_MODE=1`. Start a new agent or change mode.
- **Empty conversation in Paseo:** transcript is in another profile. Send one message; the launcher moves it and history returns on the next reload.
- **Claude Peppy agent fails mentioning `setup-token`:** no or expired token. See "Second account" under Paseo.
- **Plane reads rejected (HTTP 401/403):** the token was revoked, expired, or belongs to another account. Create a new one and rerun `install.sh`; it detects the rejected token and prompts for a replacement.
- **Proxy startup failure:** `~/.local/state/claude-codex/proxy-startup.log`.

Not supported through the gateway: Claude's server-side `WebSearch` and auto mode.

## Remove

1. `claude-codex-proxy stop`; `claude-codex-proxy paths` to list locations.
2. Paseo: `paseo-codex plugin remove claude-codex`, delete `agents.providers.claude-codex` and `claude-peppy` and the `[claude-codex pull-request policy]` block of `daemon.appendSystemPrompt` from `config.json`, reload. Remove `paseo-codex-worktree-setup` from any `paseo.json` you registered it in.
3. Delete the five launchers from `~/.local/bin` and the installer-marked PATH block in your shell files.
4. Delete the config, data, and state directories, and `~/.claude-peppy` if unneeded.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Optional integration tests against real binaries (fake upstream, temporary Paseo daemon, no real quota used):

```bash
CLIPROXYAPI_TEST_BINARY=/path/to/cli-proxy-api \
CLAUDE_TEST_BINARY=/path/to/claude \
PASEO_TEST_BINARY=/path/to/paseo \
  python3 -m unittest discover -s tests -v
```

## References

- [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) · [ChatGPT subscription auth](https://learn.chatgpt.com/docs/auth)
- [CLIProxyAPI 7.2.155](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.155) · [Codex OAuth](https://help.router-for.me/configuration/provider/codex) · [reasoning suffixes](https://help.router-for.me/configuration/thinking)
- [Claude Code gateway config](https://code.claude.com/docs/en/llm-gateway) · [model config](https://code.claude.com/docs/en/model-config) · [`setup-token`](https://code.claude.com/docs/en/iam#authentication-precedence)
- [Paseo custom providers](https://github.com/getpaseo/paseo/blob/main/docs/custom-providers.md) · [plugin reference](https://paseo.sh/docs/plugins/v0.8/reference)

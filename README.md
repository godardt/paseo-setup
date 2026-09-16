# Claude Codex

Run the **Claude Code harness with GPT-6 Astra**, using your ChatGPT subscription through CLIProxyAPI's Codex OAuth provider. A separate `claude-codex` command configures the proxy connection and reasoning level. The original `claude` executable and its user configuration stay in place. The same installer also sets up a **second, regular Claude Code account** — `claude-peppy`, profile `~/.claude-peppy` — for terminal use and as a distinguishable Paseo provider.

Optionally adds a separate **Claude Codex · GPT-6 Astra** provider to an existing Paseo installation, together with a small **Paseo plugin** built on Paseo's public plugin API (0.8+). Paseo's own files are not modified, so Paseo upgrades do not break the integration. Paseo is not required for terminal use; the installer uses whichever Paseo executable you have.

```text
Terminal: claude-codex ──────────┐
                               ├─ Claude Code → local CLIProxyAPI → Codex OAuth → GPT-6 Astra
Paseo: Claude Codex provider ────┘
```

## What this project provides

This repository contains a **Python standard-library installer and launcher**, not a fork of Claude Code, Paseo, or CLIProxyAPI. Claude Code supplies the terminal/agent harness; CLIProxyAPI translates its Anthropic-format requests into Codex requests authenticated with ChatGPT OAuth.

- **`claude-codex`** launches Claude Code with Astra model mappings and the selected reasoning level, using an isolated user profile for terminal sessions. It starts the managed local proxy when needed.
- **`claude-codex-proxy`** manages proxy startup/shutdown, login, diagnostics, and the persistent terminal reasoning default.
- **`claude-peppy`** runs a **second, regular Claude Code account** in its own profile (`~/.claude-peppy`), separate from the primary `~/.claude` account, with ambient routing variables scrubbed so it always uses its own Anthropic login. Sign in once interactively; no proxy is involved.
- **`paseo-codex`**, installed only when Paseo is detected, invokes your existing Paseo executable with the configured home. The generated provider uses `claude-codex` and a stream-JSON usage adapter for Paseo's context meter.
- **The `claude-codex` Paseo plugin** (`scripts/paseo_plugin`), installed as a directory plugin from a private copy, keeps Claude Codex agents out of Claude's auto mode through Paseo's documented `agent.create` hook. It has no build step and no runtime dependencies.

The integration does not grant model access, bypass subscription limits, or make every native Claude feature available through the translated API. See the compatibility notes below.

## Install with one command

From a checkout of this repository, run this on the computer hosting Claude Code and, if used, the Paseo daemon:

```bash
bash install.sh
```

When prompted, enter a Plane personal API token to enable **read-only access to [peppy](https://app.plane.so/peppy/)**, or press Enter to skip. Complete the ChatGPT sign-in when prompted. The script configures the proxy and, when detected, Paseo, including a small live request to verify Astra access. Plane authentication is separate and is checked on the first Plane read, not by that Astra request. If your bin directory was added to `PATH`, open a new terminal after installation or use the absolute command printed by the installer.

Requirements:

- Linux (including WSL2) or macOS, on x86_64 or ARM64.
- Python 3.9+, plus `curl` to download CLIProxyAPI (`curl` is not required with `--proxy-binary`).
- Existing Claude Code, or Node.js **22+** and npm to install it privately.
- Paseo is optional: setup runs only when an executable is found on PATH or supplied with `--paseo-bin`. The installer reuses that exact executable and does not manage Paseo releases. Paseo **0.8.0 or later** is required for the plugin (`requirements.paseo` in its manifest); on an older daemon Paseo reports the plugin as incompatible and the providers still work without it. The desktop app's bundled `paseo` command is accepted. Only the optional legacy `--paseo-patch` needs Node.js and a user-writable npm-installed package whose Claude provider module it recognizes; the desktop bundle packs that module inside `app.asar` and cannot take the patch.
- A ChatGPT account with subscription-based Codex access **and access to `gpt-6-astra`**.

Model access and usage limits come from the signed-in ChatGPT account, not a proxy setting. A subscription's plan label alone does not establish Astra entitlement. By default, the installer checks an actual request and reports access/quota failures. This integration does not require an OpenAI API key or configure pay-as-you-go API billing.

### What the installer does

1. Reuses your Claude binary. If missing, installs `@anthropic-ai/claude-code@2.1.246` into a private directory.
2. Selects the executable supplied with `--paseo-bin`, or the existing `paseo` on PATH. When PATH resolves only to the desktop app bundle, or to nothing, the executable selected by the previous install is reused if it still exists. **If none is available, skips all Paseo configuration and daemon operations.** No Paseo package is downloaded or version enforced; cached private copies from older installer runs are not selected automatically. `paseo-codex` invokes the detected executable.
3. Downloads **CLIProxyAPI 7.2.155** and verifies a pinned SHA-256 checksum. Linux uses the static build without plugin support.
4. Creates an isolated configuration, OAuth directory, random local API token, and Claude profile with private file permissions.
5. Installs `claude-codex`, `claude-codex-proxy`, `claude-peppy`, and (when Paseo is detected) `paseo-codex` into `~/.local/bin`, then adds that directory to your shell's startup configuration.
6. When Paseo is detected, copies the Paseo plugin into the data directory, then backs up and merges into `$PASEO_HOME/config.json` (default `~/.paseo/config.json`): the two provider entries, the plugin entry under `plugins`, and `pluginsEnabled: true`. Other providers, plugins, and settings are preserved. If Paseo's Claude provider module still carries the source patch from an earlier version of this installer, it is restored to its upstream source (backup kept beside it) unless `--paseo-patch` is given.
7. Offers a masked Plane token prompt, or reuses a saved/supplied token, and installs a private GET-only MCP connector for the `peppy` workspace. No additional packages are needed. Missing credentials in noninteractive/staged installs skip new Plane setup without blocking; existing connections are retained. Likewise offers a masked prompt for the second account's long-lived token (see "The second account in Paseo").
8. Runs CLIProxyAPI's own ChatGPT OAuth login if needed, starts the local proxy, and sends a small Anthropic Messages request to Astra.
9. When Paseo is detected, restarts the local Paseo daemon using that existing executable and reloads its configuration. Finish active Paseo sessions first, or pass `--skip-paseo-start` to activate it later.

After updating the repository, run `./install.sh` again to copy the updated runtime and plugin and regenerate the installed launchers; editing this checkout alone does not update an existing installation. Reuse any custom directory options. The installer preserves the local API token, existing OAuth credentials, and the saved second-account token. Rerunning stops the managed proxy and restarts Paseo when detected, so finish active sessions first. Existing unrelated programs called `claude-codex`, `claude-codex-proxy`, `claude-peppy`, or `paseo-codex` are never overwritten. Explicit executable paths must point to the original programs, not these generated wrappers; self-referencing Claude and Paseo selections are rejected, including symlink aliases to the destination wrappers.

**Upgrading Paseo** does not require rerunning the installer: nothing of Paseo's own is modified, the provider entries are ordinary configuration, and the plugin is recompiled by the daemon from its directory. Rerun it after upgrading only if you use `--paseo-patch`.

Installers targeting the same configuration directory are serialized with `install.lock`. A second run waits until the first finishes or is interrupted, including any login and daemon-restart steps, so concurrent runs cannot mix their saved settings and proxy configuration. The legacy patch also has a package-local lock shared by installations using the same Paseo package. These locks are not crash rollback or general coordination between separate installations sharing other output directories. Do not delete a lock file to bypass a running installer.

For Bash, PATH setup updates `.bashrc` and the first existing login file in this order: `.bash_profile`, `.bash_login`, `.profile` (creating `.profile` if none exists). Zsh uses `.zshrc` and `.zprofile`; Fish uses a `conf.d` snippet. Existing Bash/Zsh dotfile symlinks are preserved, and repeated installation does not duplicate an unchanged PATH block.

## Use from the terminal

```bash
claude-codex                         # Default: high reasoning
claude-codex --reasoning low
claude-codex --reasoning medium
claude-codex --reasoning high
claude-codex --reasoning xhigh
claude-codex --reasoning max
claude-codex --reasoning ultracode    # xhigh + Claude workflow orchestration
claude-codex --reasoning high -p 'Explain this project'
claude-codex --reasoning max --resume
```

Both the terminal launcher and Paseo use Astra's full **1,050,000-token context window** at every reasoning level. The launcher sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1050000`, overriding an inherited 200K value, and the generated Paseo provider declares the same window. Claude Code therefore budgets the session against Astra's documented window instead of its 200K fallback for unknown gateway model IDs. Automatic compaction remains enabled; usable input space also reserves room for output. See [Astra's model specification](https://developers.openai.com/api/docs/models/gpt-6-astra) and [Claude's gateway context configuration](https://code.claude.com/docs/en/model-config#correct-the-window-for-a-gateway-or-custom-model-id).

After updating an existing installation, restart terminal sessions or create a new Paseo agent to pick up the new environment. Setting the client window does not change the upstream account's model access or limits.

Set the persistent terminal default:

```bash
claude-codex-proxy reasoning xhigh
```

Or override it for one invocation:

```bash
CLAUDE_CODEX_REASONING=medium claude-codex
```

Claude Code arguments are forwarded, including print mode, session resume, permissions, hooks, and the Agent SDK's stream-JSON options. `--reasoning` is handled by the wrapper. You can also use `--model 'gpt-6-astra(max)'`. Conflicting model-suffix and `--reasoning` values are rejected.

The wrapper respects the operand boundaries of supported native options: a value passed to `--append-system-prompt`, for example, remains literal even if it looks like `--model=...` or `--reasoning=...`. Bare `--resume`, boolean `-p`, empty values, and variadic SDK options remain supported. Use a standalone `--` before prompt text that should not be interpreted as options:

```bash
claude-codex -p -- '--reasoning is literal prompt text here'
```

Unknown SDK flags are forwarded, but their operand counts cannot be inferred automatically. Newly introduced native options with separate values may require updating the option table in `scripts/claude_codex.py`; `--option=value` avoids that ambiguity when supported by the native option.

The wrapper sends model names such as `gpt-6-astra(xhigh)`. CLIProxyAPI strips the suffix and sets OpenAI's `reasoning.effort`. Valid Astra levels are **low, medium, high, xhigh, max**. The proxy does not accept `none`, `minimal`, or `ultra` as Astra API reasoning values. Claude Code's separate Ultra Code workflow mode is available with `xhigh`, as described below.

Use the wrapper option or the model suffix to choose reasoning; Claude's `/effort` and thinking-budget controls do not override the suffix. Claude's Opus, Sonnet, Haiku, Fable, small/fast, and default subagent mappings all target Astra at the launch effort. A custom agent definition that explicitly hardcodes another model can still override its own model selection.

### Permission modes

Claude Code's **auto mode** judges every tool call with Anthropic's safety classifier models. CLIProxyAPI cannot serve those from a Codex subscription (it answers Claude model names with HTTP 400), and Claude Code then runs the classifier prompt on the session model instead: Astra, at the session's reasoning effort. Each tool call gains a full-transcript classifier request, the verdicts come from a model the prompt was not written for, and in auto mode a denial is final, with no approval prompt to fall back to. Observed results included ordinary `git commit` and `gh gist create` calls being refused.

The launcher therefore passes Claude's own `permissions.disableAutoMode` setting on every launch, merged into any `--settings` operand the caller already supplies. Claude starts in its normal prompting mode (`default`), the mode carousel does not offer auto, and no classifier request reaches the gateway. Choose `acceptEdits` or `bypassPermissions` when you want fewer prompts. To keep auto mode anyway, accepting that Astra judges its own actions, set `CLAUDE_CODEX_AUTO_MODE=1` in the environment; the launcher then leaves the settings untouched.

The proxy starts automatically on each launch if necessary, including after reboot. It remains running after a session exits. This is an on-demand background process, without a systemd/launchd service. If it crashes, the next launcher invocation starts it again; an already-running session may need a retry.

## Second Claude account: `claude-peppy`

`claude-peppy` runs a **second, regular Claude Code account** (a normal Anthropic login, not the Codex gateway) in its own profile, so it stays distinguishable from the primary account in `~/.claude`:

```bash
claude-peppy            # First run: sign in with the second account
claude-peppy -p 'Explain this project'
claude-peppy --resume
```

- The profile directory defaults to `~/.claude-peppy` (`--peppy-config-dir` to choose another; reruns keep a previously chosen directory). Its login, settings, skills, and terminal session history live entirely there, and the installer never requires or performs a login for it. Run `claude-peppy` once interactively to sign in.
- Claude Code arguments are forwarded untouched; there is no reasoning option, model remapping, or proxy. The primary account, the Codex gateway, and their settings are not read or modified.
- **Environment isolation:** routing variables inherited from the surrounding shell or daemon (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` and the model-override variables, `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, the Bedrock/Vertex/Foundry switches, and this integration's markers) are removed before launch, so ambient configuration cannot silently redirect the account or leak another provider's model selection. Proxy variables such as `HTTPS_PROXY`/`NO_PROXY` are kept, since the account talks to real Anthropic endpoints. Configure the account itself through `~/.claude-peppy/settings.json`.
- When a Plane connection is configured, `claude-peppy` sessions receive the same private read-only **peppy** Plane MCP connector as `claude-codex` sessions, with the same `--strict-mcp-config` opt-out. The managed config is appended after your arguments (never before a prompt), and an existing `--mcp-config` group is extended rather than replaced. Claude's management subcommands (`setup-token`, `auth`, `mcp`, `plugin`, `doctor`, `update`, and the others listed by `claude --help`) accept no session options, so both launchers forward them untouched; `claude-codex` also skips the proxy for them.

### The second account in Paseo

When Paseo is detected, the installer also merges a **Claude Peppy** provider (`agents.providers.claude-peppy`) into `$PASEO_HOME/config.json`. It extends Paseo's regular Claude provider — the native model list, auto mode, WebSearch, and normal usage reporting all work — and differs only in the account it signs in with. Select **Claude Peppy** when creating an agent to use the second account; the provider name identifies the account in the picker and on each agent.

Paseo's daemon reloads an agent's transcript from the profile *it* resolves (`CLAUDE_CONFIG_DIR` or `~/.claude`), ignoring the environment of the provider it spawns, and Paseo's plugin API offers no way to change that. Rather than pinning the peppy profile and patching Paseo to read it, the provider runs its Paseo sessions **in the daemon's profile**, exactly like Claude Codex and the regular Claude provider, and selects the account with a **long-lived token** from Claude Code's documented `claude setup-token` flow: the launcher exports it as `CLAUDE_CODE_OAUTH_TOKEN`, which Claude Code ranks above the profile's stored login. Conversations survive daemon restarts and app reloads with no changes to Paseo. Generate the token once and give it to the installer:

```bash
claude-peppy setup-token        # Sign in with the second account in the browser; copy the printed token
./install.sh                    # Paste it at the masked prompt (Enter to skip)
./install.sh --peppy-oauth-token-file /secure/path/peppy-token   # Noninteractive, or rotation
CLAUDE_PEPPY_OAUTH_TOKEN=... ./install.sh                         # From a secret manager
```

Selection order is **explicit file → environment → saved token → masked prompt**, like the Plane token. The token is stored only in the installer's private `settings.json` (mode `0600`); Paseo's configuration carries just the marker `CLAUDE_PEPPY_PASEO=1`, and the launcher injects the token at launch. The token lasts about a year and requires a Pro, Max, Team, or Enterprise subscription; when it expires, generate a new one and rerun the installer with it. Without a saved token, the provider is still configured, but a Paseo session fails at startup with instructions instead of silently using the daemon's primary login. The terminal `claude-peppy` command is unchanged and never uses the token: it keeps running in its own profile with its own browser login.

Consequences of running in the daemon's profile: Paseo sessions of the second account use that profile's user-level settings, hooks, and plugins (as Claude Codex sessions already do), their transcripts and auto-memory live there, and they appear in the regular Claude provider's session-import list. A Claude Peppy agent created by an earlier version of this installer recorded its transcript in the peppy profile; when Paseo resumes it, the launcher moves that transcript into the daemon's profile, and the history shows after the next reload. `--skip-peppy` skips the launcher and provider while preserving existing ones and the saved token.

## Use with Paseo

After installation, select the **Claude Codex · GPT-6 Astra** provider when creating an agent. The **model picker** lists the five Astra reasoning levels plus **GPT-6 Astra · Ultra Code**. The provider uses `extends: "claude"` and the absolute path to `claude-codex`, so the daemon does not depend on your interactive shell's PATH.

### Permission modes in Paseo

The `claude-codex` **Paseo plugin** registers a `before("agent.create")` hook. When an agent is created for the Claude Codex provider with **Auto mode** requested — Paseo's default for Claude providers, and what an older agent profile may still name — the hook rewrites the request to **Always Ask**, matching what Claude actually runs (see "Permission modes" above). Permission prompts appear in the app as usual; **Accept File Edits** and **Bypass** remain available, and a request without a mode is left alone so a child agent keeps inheriting its parent's mode. The regular Claude and Claude Peppy providers are untouched. Auto mode still appears in the mode carousel, because a plugin cannot edit a built-in provider's catalog; selecting it makes Claude run in `default` and report that. To keep auto mode for this provider, add `"CLAUDE_CODEX_AUTO_MODE": "1"` to the provider's `env` in `config.json`, run `paseo-codex plugin disable claude-codex`, and reload Paseo.

The plugin is installed as a directory plugin from `~/.local/share/claude-codex/paseo-plugin` (the data directory), and `pluginsEnabled` is set to `true` in `config.json` if it was not already. Paseo compiles it at daemon start and on `paseo plugin reload`; it consists of one TypeScript entry with a type-only SDK import, so no `npm install` or build runs on your machine. Inspect it with `paseo-codex plugin ls` and `paseo-codex plugin logs claude-codex`. Plugin server code runs unsandboxed as the daemon user, like any Paseo plugin; this one registers one hook and nothing else.

### Context usage and forked skills

The launcher removes Claude's provisional input estimates from the stream so Paseo's context meter uses measured counts, and declares the **1,050,000-token** window. With the default installation the context circle updates when a turn completes, from that turn's final measured usage. Because CLIProxyAPI reports Codex usage only at the end of a response, Paseo's unmodified Claude provider cannot show usage **between tool calls during a running turn**; that live update, along with nesting and finishing of subagents launched by a `context: fork` skill, previously came from a source patch to Paseo's Claude provider module and is available again with `--paseo-patch` (below). Both are upstream Paseo behaviors of the built-in Claude provider that no plugin hook can reach.

### Legacy source patch (`--paseo-patch`)

Earlier versions of this installer edited the selected Paseo package's Claude provider module (`agent.js`) by exact string replacement. That patch still exists and can be applied with `--paseo-patch`, but it is tied to the built output of the Paseo release it was written against and can stop applying on any Paseo upgrade; the installer then fails clearly without changing the module. It provides, for the Claude Codex provider only: live context usage between tool calls, forked-skill subagent tracking on live sessions and on replay, and removal of auto mode from the advertised catalog. The original module is backed up beside it as `agent.js.claude-codex-backup-*`, file permissions are preserved, the sibling task protocol module is read to confirm the contract, and unchanged reinstallations do not duplicate the patch or backup. It requires Node.js and a user-writable npm-installed Paseo package; the desktop app's bundle cannot take it.

A default `./install.sh` run (without the flag) recognizes every layout this installer ever wrote, restores the upstream source from it with a new backup, and leaves an unpatched or unfamiliar module untouched. A module with the patch marker but an unrecognized (locally edited) layout is reported; restore its backup or reinstall Paseo, then rerun. Reinstalling or updating Paseo replaces the module and thereby removes the patch; rerun with `--paseo-patch` afterward if you rely on it. With `--skip-paseo-start`, run `paseo-codex daemon restart` and `paseo-codex reload` yourself after any of these changes.

### Conversation history in Paseo

Paseo reloads an agent's transcript from the Claude profile **its daemon** resolves: the daemon's own `CLAUDE_CONFIG_DIR`, or `~/.claude`. It does not consult the environment of the provider it spawns. Paseo sessions of both providers therefore run in that profile rather than in a launcher-specific profile, so the conversation survives daemon restarts, reloads, and reopening the app. The isolated Codex profile and the peppy profile remain in use for terminal launches only.

Earlier versions recorded Paseo sessions in the isolated profile, which made a conversation appear empty whenever Paseo reloaded it from disk. After updating, rerun the installer so the provider entry no longer pins the isolated profile, then reload Paseo. When Paseo resumes an agent created before the update, the launcher moves that session's transcript into the daemon's profile; Claude continues the conversation immediately, and Paseo shows the earlier history after its next reload or daemon restart. The user-level settings, hooks, and plugins of the daemon's profile apply to Paseo sessions, the same as for Paseo's regular Claude provider.

The five API reasoning levels are encoded in model IDs. For consistent helper/subagent effort, choose the variant when creating the session; helpers retain their launch-time environment if you change the main model during a session.

### Ultra Code

Select **GPT-6 Astra · Ultra Code** directly in Paseo's **model picker**. This enables Claude Code's native workflow mode automatically, even if Paseo does not expose a separate thinking control. The proxy alias `gpt-6-astra-ultracode` routes to Astra with `xhigh` reasoning and the full 1,050,000-token context window. It is a launcher profile for the same Astra model.

The earlier **GPT-6 Astra · xhigh → Ultra Code** thinking option is retained for existing agents; **Standard** on that entry keeps ordinary xhigh reasoning.

Ultra Code is workflow orchestration at xhigh per-message reasoning. It is not an API effort above `max`, and it is different from Codex's own Ultra mode. Dynamic workflows must be available and enabled in your Claude Code installation; Claude's normal workflow permissions still apply. See [Claude Code's Ultra Code behavior](https://code.claude.com/docs/en/model-config#adjust-effort-level) and [Codex Max and Ultra](https://learn.chatgpt.com/docs/models#know-when-to-use-max-or-ultra).

The terminal equivalent uses Claude's native flag (requires Claude Code 2.1.203+):

```bash
claude-codex --reasoning ultracode
```

After updating an existing installation, reload Paseo and select **GPT-6 Astra · Ultra Code** in the model picker. If the app shows a cached list, reconnect to the host or reopen the app.

`paseo-codex` is a convenience command that invokes your selected Paseo executable with the configured `PASEO_HOME`. When Node was detected during installation, the wrapper prepends that Node directory to the caller's **current PATH**; it does not freeze the entire installation-time PATH. Newly added tool directories and activated environments remain available, though the saved Node directory takes precedence. An already-running daemon keeps its existing environment until restarted. The wrapper helps when using an explicit executable path or a nondefault home:

```bash
paseo-codex provider diagnostic claude-codex
paseo-codex run --provider claude-codex 'Explain this project'
```

The regular Claude provider remains available. Install on each remote **daemon host**, not just on the phone or desktop UI; `127.0.0.1` refers to that host. The installed version must support custom providers that extend Claude and the documented daemon commands. Version management for the daemon and desktop/mobile UI remains yours.

## Installer options

```bash
# Choose the initial default.
bash install.sh --reasoning max

# Headless host: device-code login.
bash install.sh --device-login

# Print the OAuth URL without opening a browser.
bash install.sh --no-browser

# Different local port and Paseo home.
bash install.sh --port 18317 --paseo-home ~/.paseo-work

# A different profile for the second Claude account, its Paseo token, or none of it.
bash install.sh --peppy-config-dir ~/.claude-work
bash install.sh --peppy-oauth-token-file /secure/path/peppy-token
bash install.sh --skip-peppy

# Also apply the legacy, Paseo-version-specific source patch (see above).
bash install.sh --paseo-patch

# Choose existing executables explicitly.
bash install.sh --claude-bin /path/to/claude --paseo-bin /path/to/paseo

# Terminal-only installation.
bash install.sh --skip-paseo

# Stage files without login, live model requests, Paseo startup, or shell edits.
bash install.sh --skip-login --skip-paseo-start --no-path

bash install.sh --help
```

`--skip-smoke-test` skips the subscription-consuming verification request. `--skip-paseo-start` stages the provider and plugin configuration but leaves daemon activation to you; subsequently run `paseo-codex daemon restart` and `paseo-codex reload`.

`--proxy-version X.Y.Z` selects another release and verifies its published checksum. `--proxy-binary /path/to/cli-proxy-api` uses an existing trusted binary without downloading or verifying it; use 7.2.155 or a compatible version with Astra and `max` support.

You can override `--bin-dir`, `--config-dir`, `--data-dir`, and `--state-dir`. The last three default to XDG directories when the corresponding XDG variables are set. Reuse the same directory options on later installer runs.

## Read-only Plane connection

A plain `./install.sh` offers to connect the managed **Claude Codex** provider to **https://app.plane.so/peppy/**. Open **[Plane's personal access tokens page](https://app.plane.so/settings/profile/api-tokens)** and choose **Add personal access token**, then paste it into the installer's prompt. The installer prints that direct URL and displays `*` for each entered character instead of revealing the token; Backspace deletes a character and Ctrl+U clears the field. Do not paste credentials into an agent conversation or commit them. Press Enter to skip new setup; later installer runs reuse a saved token without prompting.

For noninteractive setup or token rotation, supply a private UTF-8 file containing only the token:

```bash
./install.sh --plane-api-key-file /secure/path/plane-token
```

Alternatively, provide `PLANE_API_KEY` through your secret manager/environment. Avoid entering literal secrets into shell history. Selection order is **explicit file → environment → saved credential → masked prompt**. Invalid explicit input fails rather than silently using an older token. The installer stores the selected token in a mode-`0600` file under its private config directory and removes the input environment variable before starting subprocesses. The credential is not placed in Paseo's provider JSON, launchers, MCP configuration, or agent environment.

- `--skip-plane` skips setup and prompting, **preserving any existing Plane connection and token**; it is not an uninstall switch.
- `--skip-login` suppresses the new-token prompt. A supplied or saved token can still be staged, without contacting Plane.
- Noninteractive installs without a supplied or saved token skip new Plane setup and print that no connection was configured.
- To replace an expired token, rerun with `--plane-api-key-file` or `PLANE_API_KEY`. A malformed saved credential file is reported rather than silently overwritten; inspect it before replacing it.

### Available reads and enforcement

The installed MCP server, `claude-codex-plane-peppy-readonly`, exposes only:

- `list_projects` — list accessible projects in `peppy`.
- `list_work_items` — list a project's work items using its UUID.
- `get_work_item` — read a work item's details using project and work-item UUIDs.

List tools return pagination metadata and accept `cursor` and `per_page` (at most 100). Access is limited by the token owner's Plane permissions. The connector hardcodes the Plane cloud API origin, `peppy` workspace, approved routes, and HTTP **GET**; it rejects redirects, mutation tools, arbitrary URLs/paths, and unrecognized parameters. It cannot create, edit, comment on, or delete Plane data. This is enforced by code, not by a prompt or a tool's `readOnlyHint` annotation. The current connector does not expose other Plane resources such as pages or cycles.

**Security boundary:** this connector is read-only; your PAT/account and the entire agent are not necessarily read-only. A process with shell access to the credential file could use the token independently. Use a least-privilege Plane account and an appropriate expiration for stronger account-level protection. Plane's [official MCP server](https://developers.plane.so/dev-tools/mcp-server) requests read/write access and documents no separate read-only endpoint, so this integration deliberately does not register it.

### Use in Paseo

After running the installer, start a new **Claude Codex · GPT-6 Astra** agent and ask: “Use the Plane connection to list the projects in peppy.” The first read verifies the token and workspace access. A configured connection is not a claim that authentication has already succeeded. Authentication/authorization errors generally require checking the token, its expiry, and membership/project access in `peppy`; rate-limit errors require waiting before retrying.

With `--skip-paseo-start`, finish active sessions and run `paseo-codex daemon restart` and `paseo-codex reload` before creating the agent. The same connection is available in terminal `claude-codex` sessions. Native Claude/Codex providers and their profiles are unchanged. Existing MCP configuration operands are retained; an explicit `--strict-mcp-config` intentionally suppresses automatic Plane injection, including when supplied by an SDK client.

## Authentication and diagnostics

```bash
claude-codex-proxy status
claude-codex-proxy doctor               # Local catalog/auth-file checks
claude-codex-proxy doctor --smoke-test  # Small live Astra request
claude-codex-proxy login                # Login/reauthenticate with ChatGPT
claude-codex-proxy login --device
claude-codex-proxy restart
claude-codex-proxy stop
claude-codex-proxy paths
```

The local API token authenticates Claude to CLIProxyAPI. The separate OAuth credentials authenticate CLIProxyAPI to ChatGPT. An existing `codex login` is not imported: CLIProxyAPI owns its own refresh-token lifecycle.

Explicit `login` stops the managed proxy, runs CLIProxyAPI's browser or device login, and requires a newly created or updated valid Codex credential file. An unchanged old credential no longer makes a failed login look successful when CLIProxyAPI exits with status zero. The wrapper checks file contents and identity/modification time, so successful rewrites of identical credentials are accepted; it does not delete old credentials before retrying. This checks that credentials were saved, not which account will be selected among multiple credentials. A successful explicit login is followed by a live smoke-test request using subscription quota; ordinary `doctor` checks credentials and the local model catalog, not account entitlement.

The local listener binds to `127.0.0.1`, requires the generated API token, and disables the management API/UI. The Paseo config contains a placeholder gateway token; the launcher replaces it with the private token before executing Claude. Proxy startup messages go to a log file and wrapper errors go to stderr, preserving stdout for Paseo's protocol.

Default files:

| Path | Contents |
| --- | --- |
| `~/.config/claude-codex/settings.json` | Launcher settings, local API token, and the second account's saved Paseo token |
| `~/.config/claude-codex/proxy.yaml` | Generated CLIProxyAPI configuration (JSON syntax, valid YAML) |
| `~/.config/claude-codex/plane-credentials.json` | Optional private Plane token and fixed workspace |
| `~/.config/claude-codex/plane-mcp.json` | Optional MCP launch configuration; contains paths, not the Plane token |
| `~/.config/claude-codex/install.lock` | Persistent lock file used to serialize installers for this configuration |
| `~/.config/claude-codex/auth/` | ChatGPT OAuth credentials |
| `~/.config/claude-codex/claude/` | Isolated Claude user profile and sessions for terminal launches |
| `~/.claude-peppy/` | Second Claude Code account profile used by terminal `claude-peppy` launches |
| `~/.local/share/claude-codex/` | Runtime, versioned proxy binary, the Paseo plugin directory, optional private npm CLIs |
| `~/.local/state/claude-codex/` | PID, lock, startup log, rotating CLIProxyAPI logs |
| `~/.paseo/config.json` | Merged provider and plugin entries; timestamped backup beside it |

Keep the config/auth directory private. It is outside this repository and should not be committed.

Troubleshooting:

- **Port occupied:** rerun with `--port 18317` or another unused port. The installer does not kill unrelated listeners.
- **Login error on a headless machine:** try `--device-login`. Device login must be allowed by your ChatGPT account/workspace. For browser OAuth over SSH, forward the callback port shown by CLIProxyAPI (normally 1455).
- **401 / missing login:** run `claude-codex-proxy login`.
- **No new or updated OAuth credentials:** the login attempt did not save a valid new or changed Codex credential. Check CLIProxyAPI's login output and retry; existing credentials alone do not establish success.
- **Installer appears to wait:** another installer may hold the same configuration's `install.lock`, including while waiting for OAuth. Finish or interrupt that run rather than deleting the lock file.
- **Executable rejected as recursive:** point `--claude-bin` or `--paseo-bin` at the original executable, not an installed integration wrapper or a symlink to it.
- **Astra unavailable / 403 / quota error:** check access on the signed-in account and run `doctor --smoke-test`. The proxy catalog alone does not prove entitlement, and changing the model name cannot add subscription access.
- **Paseo provider absent:** use `paseo-codex reload`, confirm app/daemon version and `PASEO_HOME`, then run the provider diagnostic above. If Paseo reports that a restart is required, restart its daemon after finishing active sessions.
- **Paseo lists a forked-skill subagent as working long after the agent went idle:** this is Paseo's built-in Claude provider behavior for `context: fork` skills; the legacy `--paseo-patch` corrects it for the Claude Codex provider on Paseo versions it recognizes.
- **The plugin shows `failed` in `paseo-codex plugin ls`:** read `paseo-codex plugin logs claude-codex`. A daemon older than 0.8.0 reports the version requirement; rerun `./install.sh` after updating this repository if the entry file changed.
- **A tool call was "denied by the Claude Code auto mode classifier":** the session predates this launcher's auto-mode setting or runs with `CLAUDE_CODEX_AUTO_MODE=1`. Start a new agent, or switch the existing one to another permission mode; see "Permission modes".
- **Paseo shows an empty conversation for an existing agent:** the transcript is not in the profile the daemon reads. Rerun the installer and reload Paseo; see "Conversation history in Paseo" above. Sending one message to the agent moves an older transcript across (from the isolated Codex profile or the peppy profile), and the history returns after the next reload.
- **A Claude Peppy agent fails at startup mentioning `claude-peppy setup-token`:** no token is saved for the second account's Paseo sessions, or it expired. Generate one and rerun the installer with it; see "The second account in Paseo".
- **Proxy startup failure:** inspect `~/.local/state/claude-codex/proxy-startup.log` and the `logs/` directory there. Avoid sharing credentials from config files.
- **Unexpected routing:** inspect project `.claude/settings.json`, `.claude/settings.local.json`, and managed settings for conflicting model/provider environment variables.

This is a third-party compatibility bridge. Claude's server-side `WebSearch` is disabled because it is not supported by this integration; use a separately configured search tool if needed. Auto mode is disabled because its classifier cannot run on this gateway (see "Permission modes"). Existing user-level Claude plugins, credentials, and settings are not copied into the terminal launcher's isolated profile. Project `CLAUDE.md` and project settings load normally. Native Claude/Codex features are not all interchangeable through an API translator.

## Remove

1. Finish active proxied sessions and installer runs, then run `claude-codex-proxy paths` to inspect this installation's locations and `claude-codex-proxy stop` to stop its proxy.
2. If Paseo was configured, run `paseo-codex plugin remove claude-codex`, then remove only `agents.providers.claude-codex` and `agents.providers.claude-peppy` from its config and reload Paseo. Do not restore an old whole-file backup over newer unrelated edits. If you used `--paseo-patch`, a plain `./install.sh` run beforehand restores Paseo's module, or reinstall Paseo using your usual package manager; restart its daemon afterward.
3. Remove the generated `claude-codex`, `claude-codex-proxy`, and `claude-peppy` launchers, plus `paseo-codex` if installed. **Do not delete the bin directory itself**, your original Claude/Paseo executables, or the whole Paseo home.
4. After reviewing the reported paths, remove only this integration's `config_dir`, `data_dir`, and `state_dir` when you no longer need the credentials or terminal session history they contain. This also removes any private Claude installation inside the integration's data directory. Paseo session transcripts live in the daemon's Claude profile and are not affected. Remove `~/.claude-peppy` only if you no longer need the second account's login and history; it is not part of the integration's directories.
5. Remove installer-marked PATH entries from shell startup files if desired, or the generated Fish `conf.d/claude-codex.fish` snippet. Keep shared PATH entries that you still need for other programs.

## Development and verification

```bash
python3 -m unittest discover -s tests -v
```

Without integration environment variables, the real-binary tests are skipped. Node.js is used for offline JavaScript compatibility and installer checks; those tests skip when Node is unavailable. Offline coverage includes:

- Model/reasoning normalization and native argument-value boundaries.
- The launcher's auto-mode setting: appended, merged into a caller's `--settings` JSON or file without replacing it, kept out of the literal prompt tail, and skipped with `CLAUDE_CODEX_AUTO_MODE=1`.
- Paseo launches keeping the daemon's Claude profile, and moving a resumed session's transcript out of the isolated profile (or, for the second account, its own profile) exactly once.
- The second account's Paseo mode: the saved token injected and ambient credentials scrubbed, pinned profiles from older provider entries dropped, a missing token failing before Claude starts, and token selection precedence, validation, private storage, retention, and exclusion from the daemon's environment.
- The Paseo plugin: manifest and entry rules Paseo's loader enforces, no runtime dependencies, the installed copy matching the repository, and the hook rewriting only an explicit auto-mode request for the Claude Codex provider (run with Node's TypeScript type stripping when available).
- Login success/failure using temporary fake credentials, including stale credentials and same-content rewrites.
- Generated shell launchers, runtime PATH, Bash startup-file precedence, and recursion guards.
- Plane setup, masked terminal input and terminal restoration after Ctrl+C, credential precedence/retention/rotation, private file permissions, secret-free subprocess environments, MCP argument preservation/strict isolation, and the connector's fixed-workspace GET-only request and JSON-RPC handling.
- Coordinated installer subprocesses verifying serialization, retained settings, and lock release on failure.
- Download/checksum and archive-handling fixtures, provider configuration merging, and the stream-JSON usage adapter.
- Live-usage cache accounting, request/compaction resets, malformed data, model switches, and unchanged native-provider behavior.
- Forked-skill subagents against verbatim copies of Paseo's subagent modules: declaration from the Skill call, nesting of background children, no duplicate task card, completion and failure from the Skill result, nested forks, backgrounded-at-start children, replay facts linking the fork's transcript, and unchanged events for unmarked sessions.
- The mode catalog and a running session's advertised modes with and without the provider marker and the override.
- Provider-scoped profile resolution and model-label prefixes of the legacy patch, which are inert with the current provider entries.
- The second-account launcher: native arguments forwarded in the scrubbed profile environment, appended Plane MCP injection around prompts, separators, strict mode, and probes, per-key provider merging, profile-directory round-trips, and wrapper recursion guards.
- Legacy patch source guards for every edited anchor, the read-only task protocol contract check, syntax validation, atomic backups, package locking, in-place upgrade of the earlier patch layouts, application only with `--paseo-patch`, and removal by a default run from every layout with a backup, while unfamiliar modules are left untouched.

Tests use temporary directories, mocks, and local subprocesses; they do not read your OAuth credentials or change your existing Claude/Paseo settings. To include real CLIProxyAPI translation tests against a **local fake upstream**:

```bash
CLIPROXYAPI_TEST_BINARY=/absolute/path/to/cli-proxy-api \
  python3 -m unittest discover -s tests -v
```

To also exercise the real Claude harness and an isolated Paseo daemon:

```bash
CLIPROXYAPI_TEST_BINARY=/absolute/path/to/cli-proxy-api \
CLAUDE_TEST_BINARY=/absolute/path/to/claude \
PASEO_TEST_BINARY=/absolute/path/to/paseo \
  python3 -m unittest discover -s tests -v
```

The optional tests verify Anthropic streaming, reasoning translation, option-like system-prompt values reaching the upstream unchanged, tool execution, proxy lifecycle behavior, and complete Paseo sessions including Ultra Code and transcript persistence. A gated **Read → Read → text** regression inspects the actual daemon's context data while the first turn is still running, before any final result; it also checks that subsequent usage replaces earlier counts. Usage arrives only at response completion in the fake upstream. A real-Claude test shows the auto-mode classifier reaching the gateway as Astra requests without the launcher setting, and a session starting in `default` mode with no classifier traffic with it. The live-usage, forked-skill, and catalog checks install with `--paseo-patch`; a separate daemon test installs the default way and checks that the plugin loads and that an agent requesting auto mode is created and runs in `default`. The second-account test installs with a token file and checks that the daemon's profile receives the transcript, that the token and marker reach the fake Claude, that the peppy profile stays empty, and that the conversation replays after a daemon restart with no patch applied.

The Paseo tests copy the selected CLI/server package into temporary directories, install twice into that private copy, and start/stop their own daemon using temporary configuration and a temporary Claude profile. Client connections explicitly target their loopback test endpoint, with no fallback to your normal daemon. The existing installed package, daemon, and `~/.claude` are unaffected. Allow temporary disk space for a copy of your Paseo package and dependencies. No real Codex backend or subscription quota is used by these tests.

All **118 tests passed**, with no skips, in development validation on Linux with CLIProxyAPI 7.2.155, Claude Code 2.1.266, and a private copy of the locally installed Paseo; the daemon tests upgraded that copy's earlier usage-only patch in place. macOS and live ChatGPT OAuth were not exercised by that validation; login failures are simulated in offline tests. Model entitlement remains account-dependent, and the installer's live smoke test checks that path after you sign in.

The Plane changes were verified on macOS with Python 3.14.7 and 3.9.6, and the second-account changes with Python 3.14.7: **205 tests passed, including all 16 optional real-binary integration tests** (Claude Code 2.1.270, CLIProxyAPI 7.2.155, and a private copy of the locally installed Paseo daemon). Offline runs skip those 16. The Paseo test drives the **Claude Peppy** provider end to end with a fake Claude binary that honors `CLAUDE_CONFIG_DIR`: the session records its transcript in the peppy profile, nothing lands in the daemon's primary profile, and the conversation replays after a daemon restart. Temporary package fixtures resolve macOS's `/var` symlink before comparing filesystem paths.

The model-label prefix changes were verified on macOS with Python 3.14.7: **208 tests** — the full offline suite passes, and the real-binary run passes 15 of the 16 optional integration tests, upgrading a private copy of the locally installed (prefixless-layout) Paseo package in place; the failing forked-skill test also fails identically on the previous commit and is unrelated (Claude Code 2.1.270 now emits an extra `task_notification` call the expectation predates). Two test-arrangement fixes were included: a user-prepared profile's mode is now set explicitly so a `0o077` shell umask cannot mask it, matching the test's intent that the installer preserve existing permissions.

The plugin migration was verified on Linux (aarch64) with Python 3.12, Node 24, Claude Code 2.1.273, CLIProxyAPI 7.2.155, and a private copy of the locally installed Paseo 0.8.0: the full offline suite of **222 tests** passes, and 8 of the 9 gated Paseo daemon tests pass against that private copy (the plugin loading in a real daemon and rewriting an explicit auto-mode request, the token-selected second account replaying after a daemon restart, the default install restoring the previously patched module, turn-end context usage without the patch, live usage with `--paseo-patch`, and the Ultra Code selections). The forked-skill daemon test was not run; it already failed independently of this change on Claude Code 2.1.270+ as noted above. The proxy-only integration tests were not rerun; the proxy and terminal launcher are unchanged.

## Upstream references

- [GPT-6 Astra model and supported reasoning levels](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [OpenAI subscription authentication](https://learn.chatgpt.com/docs/auth)
- [CLIProxyAPI 7.2.155 release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.155)
- [CLIProxyAPI Codex OAuth login](https://help.router-for.me/configuration/provider/codex)
- [CLIProxyAPI reasoning suffixes](https://help.router-for.me/configuration/thinking) and [pinned Astra model registry](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.155/internal/registry/models/models.json)
- [Claude gateway configuration](https://code.claude.com/docs/en/llm-gateway) and [model configuration](https://code.claude.com/docs/en/model-config)
- [Paseo custom provider schema](https://github.com/getpaseo/paseo/blob/main/docs/custom-providers.md) and [Claude adapter](https://github.com/getpaseo/paseo/blob/main/packages/server/src/server/agent/providers/claude/agent.ts)
- [Paseo plugin reference (0.8)](https://paseo.sh/docs/plugins/v0.8/reference) and [`@getpaseo/plugin`](https://www.npmjs.com/package/@getpaseo/plugin)
- [Claude Code authentication precedence and `claude setup-token`](https://code.claude.com/docs/en/iam#authentication-precedence)

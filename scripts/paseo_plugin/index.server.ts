// Paseo plugin for the Claude Codex provider (installed by claude-codex).
//
// This uses only Paseo's public plugin API, so it keeps working across Paseo
// upgrades. The daemon compiles it itself; there is no build step and no
// runtime dependency (the SDK import below is type-only; the connector module
// is generated next to this file by the installer).
import type { PluginServerContext } from "@getpaseo/plugin/server";
import { PLANE_MCP_SERVERS } from "./server/plane-connector.ts";

// Providers written by the installer whose Claude Code launcher disables auto
// mode: its permission classifier cannot run on the Codex gateway, so the
// launcher starts Claude in its ordinary prompting mode. Without this hook a
// new agent still requests Paseo's default for Claude, "auto", and the mode
// it displays disagrees with the one Claude actually runs until Claude
// reports back.
export const GATEWAY_PROVIDERS: readonly string[] = ["claude-codex"];
export const UNAVAILABLE_MODE = "auto";
export const FALLBACK_MODE = "default";

// Every Paseo provider that receives the read-only Plane connector: the three
// that run Claude Code (Paseo's built-in provider and the two written by the
// installer) and Paseo's built-in Codex provider. Paseo starts the built-in
// providers directly, never through the claude-codex or claude-peppy
// launchers, so the connector those launchers add with --mcp-config would
// otherwise be missing from their agents; Paseo hands a Codex agent's MCP
// servers to Codex as its mcp_servers configuration. Paseo persists an
// agent's MCP servers with its configuration, so the connector follows the
// agent across resumes and daemon restarts; Claude Code treats a launcher copy
// of the same server definition as one server.
export const PLANE_PROVIDERS: readonly string[] = ["claude", "claude-codex", "claude-peppy", "codex"];

export interface AgentCreateRequest {
  config: {
    provider: string;
    modeId?: string;
    mcpServers?: Record<string, unknown>;
    [key: string]: unknown;
  };
  env?: Record<string, string>;
}

// Only an explicit request for the unavailable mode is rewritten. A request
// without a mode is left alone so a child agent keeps inheriting its parent's
// mode exactly as Paseo resolves it.
export function resolveMode(config: AgentCreateRequest["config"]): string | undefined {
  if (!GATEWAY_PROVIDERS.includes(config.provider) || config.modeId !== UNAVAILABLE_MODE) {
    return undefined;
  }
  return FALLBACK_MODE;
}

// The connector servers missing from a Claude Code or Codex agent's request. A
// caller's own server of the same name wins, so an explicit request is never
// replaced.
export function missingPlaneServers(config: AgentCreateRequest["config"]): Record<string, unknown> | undefined {
  if (!PLANE_PROVIDERS.includes(config.provider)) {
    return undefined;
  }
  const existing = config.mcpServers ?? {};
  const missing: Record<string, unknown> = {};
  for (const [name, server] of Object.entries(PLANE_MCP_SERVERS)) {
    if (!(name in existing)) {
      missing[name] = server;
    }
  }
  return Object.keys(missing).length > 0 ? missing : undefined;
}

export function resolveAgentCreate(request: AgentCreateRequest): AgentCreateRequest | undefined {
  const modeId = resolveMode(request.config);
  const servers = missingPlaneServers(request.config);
  if (modeId === undefined && servers === undefined) {
    return undefined;
  }
  const config = { ...request.config };
  if (modeId !== undefined) {
    config.modeId = modeId;
  }
  if (servers !== undefined) {
    config.mcpServers = { ...(request.config.mcpServers ?? {}), ...servers };
  }
  return { ...request, config };
}

export default function contribute(server: PluginServerContext) {
  const unsubscribe = server.before("agent.create", ({ request }) => resolveAgentCreate(request));
  return () => {
    unsubscribe();
  };
}

// Paseo plugin for the Claude Codex provider (installed by claude-codex).
//
// This uses only Paseo's public plugin API, so it keeps working across Paseo
// upgrades. The daemon compiles it itself; there is no build step and no
// runtime dependency (the SDK import below is type-only).
import type { PluginServerContext } from "@getpaseo/plugin/server";

// Providers written by the installer whose Claude Code launcher disables auto
// mode: its permission classifier cannot run on the Codex gateway, so the
// launcher starts Claude in its ordinary prompting mode. Without this hook a
// new agent still requests Paseo's default for Claude, "auto", and the mode
// it displays disagrees with the one Claude actually runs until Claude
// reports back.
export const GATEWAY_PROVIDERS: readonly string[] = ["claude-codex"];
export const UNAVAILABLE_MODE = "auto";
export const FALLBACK_MODE = "default";

export interface AgentCreateRequest {
  config: { provider: string; modeId?: string; [key: string]: unknown };
  env?: Record<string, string>;
}

// Only an explicit request for the unavailable mode is rewritten. A request
// without a mode is left alone so a child agent keeps inheriting its parent's
// mode exactly as Paseo resolves it.
export function resolveAgentCreate(request: AgentCreateRequest): AgentCreateRequest | undefined {
  if (!GATEWAY_PROVIDERS.includes(request.config.provider) || request.config.modeId !== UNAVAILABLE_MODE) {
    return undefined;
  }
  return { ...request, config: { ...request.config, modeId: FALLBACK_MODE } };
}

export default function contribute(server: PluginServerContext) {
  const unsubscribe = server.before("agent.create", ({ request }) => resolveAgentCreate(request));
  return () => {
    unsubscribe();
  };
}

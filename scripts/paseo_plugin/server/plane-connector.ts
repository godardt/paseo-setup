// Plane connector definition for Paseo agents. The installer regenerates this
// module from the installed plane-mcp.json; the repository copy is the empty
// default used when no Plane connection is configured. Do not edit by hand.
export const PLANE_MCP_SERVERS: Readonly<Record<string, PlaneMcpServer>> = {};

export interface PlaneMcpServer {
  type: "stdio";
  command: string;
  args: string[];
}

import type { TuiPlugin } from "@opencode-ai/plugin/tui"
import { readTrustState, writeTrustState } from "../trust-state"

function currentSessionID(api: Parameters<TuiPlugin>[0]): string | undefined {
  const route = api.route.current
  if (route?.name !== "session") return undefined
  const sessionID: unknown = route.params?.sessionID
  return typeof sessionID === "string" ? sessionID : undefined
}

async function toggle(api: Parameters<TuiPlugin>[0]): Promise<void> {
  const sessionID = currentSessionID(api)
  if (!sessionID) {
    api.ui.toast({ title: "Permission gate", message: "Open a session first", variant: "warning" })
    return
  }
  const trusted = !readTrustState(sessionID).trusted
  await writeTrustState(sessionID, { trusted })
  api.ui.toast({
    title: "Permission gate",
    message: trusted
      ? "Trust mode enabled: permission prompts are auto-allowed for this session"
      : "Permission prompts are enabled for this session",
    variant: trusted ? "warning" : "info",
  })
}

export const PermissionGate: TuiPlugin = async (api) => {
  api.keymap.registerLayer({
    commands: [
      {
        name: "permission-gate.toggle",
        title: "Permission gate: toggle",
        namespace: "palette",
        slashName: "permission-gate",
        run: () => toggle(api),
      },
    ],
    bindings: [{ key: "<leader>p", cmd: "permission-gate.toggle" }],
  })
}

export default { id: "permission-gate", tui: PermissionGate }

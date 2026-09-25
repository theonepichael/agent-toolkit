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
    api.ui.toast({ title: "Trust session", message: "Open a session first", variant: "warning" })
    return
  }
  const trusted = !readTrustState(sessionID).trusted
  await writeTrustState(sessionID, { trusted })
  api.ui.toast({
    title: "Trust session",
    message: trusted
      ? "Trust mode enabled: guard-rails and permission prompts are disabled for this session"
      : "Guard-rails and permission prompts are enabled for this session",
    variant: trusted ? "warning" : "info",
  })
}

export const TrustSession: TuiPlugin = async (api) => {
  api.keymap.registerLayer({
    commands: [
      {
        name: "trust-session.toggle",
        title: "Trust session: toggle",
        namespace: "palette",
        slashName: "trust-session",
        run: () => toggle(api),
      },
    ],
    bindings: [{ key: "<leader>y", cmd: "trust-session.toggle" }],
  })
}

export default { id: "trust-session", tui: TrustSession }

import type { Plugin } from "@opencode-ai/plugin"
import { readTrustState } from "../trust-state"

type PermissionAskedEvent = {
  type: "permission.asked"
  properties: { id: string; sessionID: string }
}

type PermissionClient = {
  postSessionIdPermissionsPermissionId: (input: {
    path: { id: string; permissionID: string }
    body: { response: "once" }
  }) => Promise<unknown>
}

export const TrustSession: Plugin = async ({ client }) => ({
  event: async ({ event }) => {
    const permissionEvent = event as unknown as PermissionAskedEvent
    if (permissionEvent.type !== "permission.asked") return
    const properties = permissionEvent.properties
    if (!readTrustState(properties.sessionID).trusted) return
    const permissionClient = client as unknown as PermissionClient
    await permissionClient.postSessionIdPermissionsPermissionId({
      path: { id: properties.sessionID, permissionID: properties.id },
      body: { response: "once" },
    })
  },
})

export default TrustSession

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { setGuardRailsEnabled } from "./guard-rails";
import { setPermissionGateEnabled } from "./permission-gate";

// guard-rails.ts and permission-gate.ts are independent gates with their own
// enable/disable commands -- disabling one leaves the other still asking for
// anything it doesn't recognize (confirmed: disabling guard-rails alone
// still lets permission-gate intercept sudo/rm -rf with its own dialog).
// This is the single "go for it, I trust you" switch that flips both.
export default function (pi: ExtensionAPI) {
  pi.registerCommand("trust-session", {
    description: "Disable guard-rails and permission-gate for this session",
    handler: async (_args, ctx) => {
      setGuardRailsEnabled(false);
      setPermissionGateEnabled(false);
      ctx.ui.notify(
        "Trust mode: guard-rails and permission-gate disabled for this session",
        "warning",
      );
    },
  });

  pi.registerCommand("trust-session-off", {
    description: "Re-enable guard-rails and permission-gate",
    handler: async (_args, ctx) => {
      setGuardRailsEnabled(true);
      setPermissionGateEnabled(true);
      ctx.ui.notify("Guard-rails and permission-gate re-enabled", "info");
    },
  });
}

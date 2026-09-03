import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// guard-rails.ts and permission-gate.ts are independent gates with their own
// enable/disable commands -- disabling one leaves the other still asking for
// anything it doesn't recognize (confirmed: disabling guard-rails alone
// still lets permission-gate intercept sudo/rm -rf with its own dialog).
// This is the single "go for it, I trust you" switch that flips both.
//
// The toggling travels over pi's shared extension event bus, NOT through
// imported setters: pi evaluates every extension file in its own jiti
// registry (moduleCache disabled), so an imported module is a private copy
// and module state never crosses extension boundaries. The first version of
// this command imported both gates' setters and flipped copies nothing read
// -- its notify reported success while neither loaded gate observed anything
// (proven live 2026-09-02). The gates subscribe to the channel below and map
// `trusted` onto their own module state.
//
// The channel name is a string literal here and in guard-rails.ts and
// permission-gate.ts -- grep "session-trust-changed" to find all three. A
// shared constants module cannot fix that drift: pi auto-discovers every .ts
// file in an extensions dir as an extension (a non-factory sibling fails to
// load on every start), and a relative import from one of these symlinked
// extensions resolves against the symlink dir, not this repo -- the same
// path split that hid the original bug. test/toggle-check.test.ts's
// isolated-instances block fails if any copy drifts.
const TRUST_CHANNEL = "session-trust-changed";

export default function (pi: ExtensionAPI) {
  pi.registerCommand("trust-session", {
    description: "Disable guard-rails and permission-gate for this session",
    handler: async (_args, ctx) => {
      pi.events.emit(TRUST_CHANNEL, { trusted: true });
      ctx.ui.notify(
        "Trust mode: guard-rails and permission-gate disabled for this session",
        "warning",
      );
    },
  });

  pi.registerCommand("trust-session-off", {
    description: "Re-enable guard-rails and permission-gate",
    handler: async (_args, ctx) => {
      pi.events.emit(TRUST_CHANNEL, { trusted: false });
      ctx.ui.notify("Guard-rails and permission-gate re-enabled", "info");
    },
  });
}

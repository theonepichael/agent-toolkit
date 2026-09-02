import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Reports every blocking UI prompt to herdr, so an agent waiting on a human
// is distinguishable from one still working.
//
// herdr's pi integration (herdr-agent-state.ts -- shipped by herdr, and
// deliberately ignored by links.toml so herdr can update it) learns about a
// blocked agent from exactly one signal: the custom `herdr:blocked` event.
// It does no screen detection once its lifecycle hook is active. Until this
// bridge existed, question-tool.ts was the only thing in the tree that
// emitted that event, so a question picker reported `blocked` and every
// other prompt reported nothing at all.
//
// That gap had a real cost. permission-gate.ts raises `ctx.ui.confirm` for
// any bash command outside its allowlist; during a live swarm run on
// 2026-09-02 that stranded workers indefinitely, invisibly -- agent_status
// stayed "working", so swarm_poll read them as making progress, the
// orchestrator relayed nothing, and the human had to go hunting through
// panes to find out why a run had stopped moving.
//
// Pi already publishes what's needed: `ui_prompt_start` fires whenever it
// begins waiting on a blocking extension UI prompt, `ui_prompt_end` when it
// stops, both carrying the prompt's kind and title. Verified live against a
// real pi in a herdr pane -- permission-gate's confirm produced
//   {"type":"ui_prompt_start","reason":"ui_prompt","kind":"confirm",
//    "title":"Run bash command?"}
// while herdr still reported "working" for 40 consecutive polls, purely
// because nothing connected the two. This file is that wire.
//
// Being driven by pi's own events rather than by each extension remembering
// to emit is the point: every present and future `ctx.ui.*` prompt is
// covered for free, and there is no per-call-site step to forget.
//
// Note for anyone debugging a missing `blocked` state: this only works when
// herdr-agent-state.ts is loaded, which happens through extension
// DISCOVERY. A pi started with `-ne`/`--no-extensions` has no listener for
// `herdr:blocked` at all, so no prompt of any kind will report blocked --
// that is inherent to disabling discovery, not something this bridge can
// repair from inside.
export default function (pi: ExtensionAPI) {
  pi.on("ui_prompt_start", (event) => {
    // herdr-agent-state.ts stores `label` as the blocked message, so send
    // something meaningful rather than nothing when a prompt carries no
    // title. Where it ends up is only partly confirmed: `herdr agent get`
    // on a blocked agent returns no message field (checked live 2026-09-02,
    // the payload carries agent_status but no label), so treat this as
    // feeding herdr's own UI rather than as something a socket client can
    // read back. A swarm orchestrator wanting the actual prompt text still
    // has to `agent read` the pane, which is what swarm-tool.ts does.
    pi.events.emit("herdr:blocked", {
      active: true,
      label: event.title ?? `${event.kind} prompt`,
    });
    return undefined;
  });

  // herdr counts these, so an unmatched start would pin the agent at blocked.
  pi.on("ui_prompt_end", () => {
    pi.events.emit("herdr:blocked", { active: false });
    return undefined;
  });
}

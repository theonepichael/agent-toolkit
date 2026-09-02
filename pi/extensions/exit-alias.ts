import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// /exit as an alias for /quit.
//
// This cannot be a prompt template (a pi/prompts/exit.md whose body is
// "/quit"). A template's body is expanded into the user message text and
// sent to the model; it is not re-dispatched as a command. Confirmed in the
// runtime's prompt() path: extension commands are resolved and executed
// first (_tryExecuteExtensionCommand), then skill and template expansion
// runs (expandPromptTemplate), and the expanded string becomes the prompt
// text. So a template alias would hand the model the literal text "/quit"
// and leave the session running.
//
// ctx.shutdown() is the programmatic equivalent of /quit: it requests a
// graceful shutdown, emitting session_shutdown to every extension first.
export default function (pi: ExtensionAPI) {
  pi.registerCommand("exit", {
    description: "Quit pi (alias for /quit)",
    handler: async (_args, ctx) => {
      ctx.shutdown();
    },
  });
}

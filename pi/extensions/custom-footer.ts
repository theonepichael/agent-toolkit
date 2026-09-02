import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

const DEV_STATUS_PATH = join(homedir(), ".claude", "scripts", "dev_status.py");

interface PendingItem {
  id: string;
  status: string;
}

export default function (pi: ExtensionAPI) {
  let enabled = false;
  let pendingCount = 0;

  async function refreshPendingCount(tui: { requestRender(): void }): Promise<void> {
    try {
      const result = await pi.exec("python3", [DEV_STATUS_PATH, "pending", "list"], {
        timeout: 5000,
      });
      if (result.code !== 0) {
        return;
      }
      const lines = result.stdout.trim().split("\n").filter(Boolean);
      let count = 0;
      for (const line of lines) {
        try {
          const item = JSON.parse(line) as PendingItem;
          if (item.status !== "resolved") {
            count++;
          }
        } catch {
          // ignore malformed lines
        }
      }
      if (count !== pendingCount) {
        pendingCount = count;
        tui.requestRender();
      }
    } catch {
      // ignore exec failures
    }
  }

  pi.registerCommand("custom-footer", {
    description: "Toggle custom footer (git branch, model, pending count)",
    handler: async (_args, ctx) => {
      enabled = !enabled;

      if (enabled) {
        ctx.ui.setFooter((tui, theme, footerData) => {
          // Initial fetch and periodic refresh every 30 s
          void refreshPendingCount(tui);
          const intervalId = setInterval(() => {
            void refreshPendingCount(tui);
          }, 30000);

          const unsubBranch = footerData.onBranchChange(() => tui.requestRender());

          return {
            dispose: () => {
              unsubBranch();
              clearInterval(intervalId);
            },
            invalidate() {},
            render(width: number): string[] {
              const branch = footerData.getGitBranch();
              const model = ctx.model?.id || "no-model";
              const branchStr = branch ? ` ${branch}` : "";
              const pendingStr = `P:${pendingCount}`;

              const left = theme.fg("dim", pendingStr);
              const right = theme.fg("dim", `${model}${branchStr}`);

              const pad = " ".repeat(Math.max(1, width - visibleWidth(left) - visibleWidth(right)));
              return [truncateToWidth(left + pad + right, width)];
            },
          };
        });
        ctx.ui.notify("Custom footer enabled", "info");
      } else {
        ctx.ui.setFooter(undefined);
        ctx.ui.notify("Default footer restored", "info");
      }
    },
  });
}

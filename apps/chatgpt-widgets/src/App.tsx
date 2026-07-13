import { useEffect, useState } from "react";
import { grafanaPanelViewSchema } from "./contracts/grafana-panel-view";
import { GrafanaPanel } from "./GrafanaPanel";
import { PanelEmptyState } from "./grafana-panel/PanelEmptyState";
import {
  getPanelPayload,
  getToolOutputFromGlobals,
  isRecord,
  looksLikePanelView,
  selectPanelPayload,
  type OpenAiGlobals
} from "./grafana-panel/payload";
import { useOpenAiGlobal } from "./grafana-panel/useOpenAiGlobal";

/**
 * VS Code Copilot Chat does not inject window.openai — it passes tool output
 * via postMessage from the parent frame. This hook listens for those messages
 * and also collects diagnostic info about what messages arrive.
 */
function usePostMessagePayload(): { payload: unknown; messages: string[] } {
  const [payload, setPayload] = useState<unknown>(undefined);
  const [messages, setMessages] = useState<string[]>([]);

  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const msg = event.data;

      // Record diagnostic info (first 5 messages)
      setMessages((prev) => {
        if (prev.length >= 5) return prev;
        try {
          const summary =
            typeof msg === "string"
              ? msg.slice(0, 120)
              : JSON.stringify(msg).slice(0, 120);
          return [...prev, summary];
        } catch {
          return [...prev, String(msg).slice(0, 120)];
        }
      });

      if (!isRecord(msg)) return;

      const candidates = [
        msg["toolOutput"],
        msg["output"],
        msg["result"],
        msg["structuredContent"],
        msg["mcpToolResult"],
        msg["payload"],
        msg["data"],
        isRecord(msg["body"]) ? msg["body"] : undefined,
        isRecord(msg["content"]) ? msg["content"] : undefined,
      ];

      for (const candidate of candidates) {
        const panelPayload = getPanelPayload(candidate);
        if (looksLikePanelView(panelPayload)) {
          setPayload(panelPayload);
          return;
        }
      }

      if ("panel" in msg && "series" in msg) {
        setPayload(msg);
      }
    };

    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, []);

  return { payload, messages };
}

export function App() {
  const openAiGlobals = useOpenAiGlobal<OpenAiGlobals>("__openai__");
  const toolOutputGlobal = useOpenAiGlobal<unknown>("toolOutput");
  const outputGlobal = useOpenAiGlobal<unknown>("output");
  const resultGlobal = useOpenAiGlobal<unknown>("result");
  const toolResultGlobal = useOpenAiGlobal<unknown>("toolResult");
  const input = useOpenAiGlobal<unknown>("input");
  const globalFallback = getToolOutputFromGlobals(openAiGlobals);
  const postMessageResult = usePostMessagePayload();
  const postMessagePayload = postMessageResult.payload;
  const postMessages = postMessageResult.messages;

  const { panelPayload, rawToolOutput: toolOutput } = selectPanelPayload([
    toolOutputGlobal,
    outputGlobal,
    resultGlobal,
    toolResultGlobal,
    globalFallback,
    postMessagePayload,
    openAiGlobals
  ]);
  const parsed = grafanaPanelViewSchema.safeParse(panelPayload);

  useEffect(() => {
    if (parsed.success) return;
    console.error("Grafana panel payload validation failed", {
      issues: parsed.error.issues,
      payload: panelPayload,
      rawToolOutput: toolOutput,
      input,
    });
  }, [input, panelPayload, parsed, toolOutput]);

  if (!toolOutput) {
    const keys = openAiGlobals ? Object.keys(openAiGlobals).slice(0, 10).join(", ") : "none";
    const msgLog = postMessages.length > 0 ? postMessages.join(" | ") : "none";
    return (
      <PanelEmptyState
        title="Loading Grafana panel..."
        message={`openai keys: ${keys} | postMessages: ${msgLog}`}
      />
    );
  }

  if (!parsed.success) {
    const firstIssue = parsed.error.issues[0];
    const issuePath = firstIssue?.path?.length ? firstIssue.path.join(".") : "payload";
    const issueMessage = firstIssue
      ? `${issuePath}: ${firstIssue.message}`
      : "This panel cannot be rendered yet.";
    return <PanelEmptyState title="Panel data is invalid" message={issueMessage} />;
  }

  return <GrafanaPanel panelView={parsed.data} />;
}

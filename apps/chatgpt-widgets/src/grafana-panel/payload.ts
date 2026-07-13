type ToolOutputEnvelope = {
  structuredContent?: unknown;
  result?: unknown;
  data?: unknown;
};

export type OpenAiGlobals = Record<string, unknown>;

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

export function looksLikePanelView(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    isRecord(value["panel"]) &&
    Array.isArray(value["series"]) &&
    Array.isArray(value["data"]) &&
    isRecord(value["visualization"]) &&
    isRecord(value["timeRange"])
  );
}

function parseJsonIfString(value: unknown): unknown {
  if (typeof value !== "string") {
    return value;
  }
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}

export function getPanelPayload(toolOutput: unknown): unknown {
  const normalized = parseJsonIfString(toolOutput);

  if (looksLikePanelView(normalized)) {
    return normalized;
  }

  if (!isRecord(normalized)) {
    return normalized;
  }

  const directKeys = ["structuredContent", "toolOutput", "output", "result", "toolResult"];
  for (const key of directKeys) {
    if (key in normalized) {
      const candidate = getPanelPayload(normalized[key]);
      if (looksLikePanelView(candidate)) {
        return candidate;
      }
    }
  }

  const queue: unknown[] = Object.values(normalized);
  const seen = new Set<unknown>();
  while (queue.length > 0) {
    const current = parseJsonIfString(queue.shift());
    if (seen.has(current)) {
      continue;
    }
    seen.add(current);
    if (looksLikePanelView(current)) {
      return current;
    }
    if (Array.isArray(current)) {
      queue.push(...current);
    } else if (isRecord(current)) {
      queue.push(...Object.values(current));
    }
  }

  return normalized;
}

export function getToolOutputFromGlobals(globals: unknown): unknown {
  if (!isRecord(globals)) return undefined;

  const priorityKeys = [
    "toolOutput",
    "output",
    "result",
    "toolResult",
    "mcpToolResult",
    "structuredContent",
    "payload",
    "data"
  ];
  for (const key of priorityKeys) {
    if (key in globals) return globals[key];
  }

  const queue: unknown[] = Object.values(globals);
  while (queue.length > 0) {
    const current = queue.shift();
    if (!isRecord(current)) continue;
    if (
      "structuredContent" in current ||
      "toolOutput" in current ||
      "output" in current ||
      "result" in current ||
      "mcpToolResult" in current ||
      "data" in current
    ) {
      return current;
    }
    queue.push(...Object.values(current));
  }

  return undefined;
}

export function selectPanelPayload(candidates: unknown[]): {
  panelPayload: unknown;
  rawToolOutput: unknown;
} {
  for (const candidate of candidates) {
    const panelPayload = getPanelPayload(candidate);
    if (looksLikePanelView(panelPayload)) {
      return { panelPayload, rawToolOutput: candidate };
    }
  }

  const rawToolOutput = candidates.find((candidate) => candidate !== undefined);
  return {
    panelPayload: getPanelPayload(rawToolOutput),
    rawToolOutput
  };
}

export function getEnvelopeStructuredContent(envelope: ToolOutputEnvelope): unknown {
  return envelope.structuredContent ?? envelope.result ?? envelope.data;
}

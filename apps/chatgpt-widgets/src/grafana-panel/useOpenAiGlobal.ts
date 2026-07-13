import { useEffect, useState } from "react";
import type { WindowWithOpenAi } from "./types";

const OPENAI_GLOBAL_EVENTS = [
  "openai:set_globals",
  "openai:update_globals",
  "openai:globals_changed",
  "openai:tool_output",
] as const;

function readGlobal<T>(key: string): T | undefined {
  const openai = (window as WindowWithOpenAi).openai;
  if (key === "__openai__") {
    return openai as T | undefined;
  }
  return openai?.[key as keyof typeof openai] as T | undefined;
}

export function useOpenAiGlobal<T>(key: string): T | undefined {
  const [value, setValue] = useState<T | undefined>(() => readGlobal<T>(key));

  useEffect(() => {
    const update = () => {
      setValue((previous) => {
        const next = readGlobal<T>(key);
        return next === undefined ? previous : next;
      });
    };

    for (const eventName of OPENAI_GLOBAL_EVENTS) {
      window.addEventListener(eventName, update);
    }

    // Some hosts update window.openai without dispatching an event.
    const pollId = window.setInterval(update, 250);
    const stopPollingId = window.setTimeout(() => window.clearInterval(pollId), 10_000);

    update();

    return () => {
      for (const eventName of OPENAI_GLOBAL_EVENTS) {
        window.removeEventListener(eventName, update);
      }
      window.clearInterval(pollId);
      window.clearTimeout(stopPollingId);
    };
  }, [key]);

  return value;
}

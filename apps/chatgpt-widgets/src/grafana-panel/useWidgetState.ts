import { useCallback, useEffect, useState } from "react";
import { useOpenAiGlobal } from "./useOpenAiGlobal";
import type { WidgetState, WindowWithOpenAi } from "./types";

export function useWidgetState() {
  const hostState = useOpenAiGlobal<WidgetState>("widgetState");
  const [state, setLocalState] = useState<WidgetState>({});

  useEffect(() => {
    if (hostState && typeof hostState === "object") {
      setLocalState(hostState);
    }
  }, [hostState]);

  const setState = useCallback((next: WidgetState) => {
    setLocalState(next);
    void (window as WindowWithOpenAi).openai?.setWidgetState?.(next);
  }, []);

  return [state, setState] as const;
}

import { useEffect, useState } from "react";
import type { WindowWithOpenAi } from "./types";

function readGlobal<T>(key: string): T | undefined {
  const openai = (window as WindowWithOpenAi).openai;
  return openai?.[key as keyof typeof openai] as T | undefined;
}

export function useOpenAiGlobal<T>(key: string): T | undefined {
  const [value, setValue] = useState<T | undefined>(() => readGlobal<T>(key));

  useEffect(() => {
    const update = () => setValue(readGlobal<T>(key));
    window.addEventListener("openai:set_globals", update);
    update();
    return () => window.removeEventListener("openai:set_globals", update);
  }, [key]);

  return value;
}

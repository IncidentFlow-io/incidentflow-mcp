import type { GrafanaPanelView } from "../contracts/grafana-panel-view";

export type OpenAiGlobal = {
  toolOutput?: unknown;
  widgetState?: unknown;
  setWidgetState?: (state: unknown) => Promise<void> | void;
  sendFollowUpMessage?: (message: string) => Promise<void> | void;
};

export type WindowWithOpenAi = Window & {
  openai?: OpenAiGlobal;
};

export type SelectedInterval = {
  from: number;
  to: number;
  annotationId?: string;
};

export type WidgetState = {
  selectedInterval?: SelectedInterval;
};

export type PanelViewProps = {
  panelView: GrafanaPanelView;
};

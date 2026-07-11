import { z } from "zod";

export const grafanaPanelVariableValueSchema = z.union([z.string(), z.array(z.string())]);

export const grafanaPanelViewSchema = z.object({
  version: z.literal("1"),
  panel: z.object({
    id: z.number().int(),
    title: z.string(),
    description: z.string().optional(),
    type: z.literal("timeseries"),
    unit: z.string().optional()
  }),
  dashboard: z.object({
    uid: z.string(),
    title: z.string()
  }),
  source: z.object({
    type: z.literal("grafana"),
    datasourceUid: z.string()
  }),
  visualization: z.object({
    type: z.union([z.literal("line"), z.literal("area")]),
    stacked: z.boolean(),
    showLegend: z.boolean(),
    showTooltip: z.boolean()
  }),
  timeRange: z.object({
    from: z.number().int(),
    to: z.number().int()
  }),
  variables: z.record(z.string(), grafanaPanelVariableValueSchema),
  series: z.array(
    z.object({
      key: z.string(),
      name: z.string(),
      unit: z.string().optional(),
      color: z.string().optional()
    })
  ),
  data: z.array(z.object({ timestamp: z.number().int() }).catchall(z.number().nullable())),
  annotations: z.array(
    z.object({
      id: z.string(),
      type: z.union([z.literal("spike"), z.literal("deployment"), z.literal("note")]),
      timestamp: z.number().int().optional(),
      from: z.number().int().optional(),
      to: z.number().int().optional(),
      label: z.string(),
      value: z.number().optional()
    })
  ),
  links: z.object({
    grafana: z.string().url()
  }),
  warnings: z.array(z.string())
});

export type GrafanaPanelView = z.infer<typeof grafanaPanelViewSchema>;
export type GrafanaPanelDataPoint = GrafanaPanelView["data"][number];

export const grafanaPanelViewInputSchema = z.object({
  dashboardUid: z.string().min(1),
  panelId: z.number().int().positive(),
  from: z.string().optional(),
  to: z.string().optional(),
  variables: z.record(z.string(), grafanaPanelVariableValueSchema).optional(),
  maxPoints: z.number().int().positive().max(500).optional()
});

export type GrafanaPanelViewInput = z.infer<typeof grafanaPanelViewInputSchema>;

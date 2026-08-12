import { z } from "zod";

/** Canonical IncidentFlow MCP success/error envelope. */
export const toolResponseSchema = z.object({
  api_version: z.string(),
  schema_version: z.string(),
  schema_id: z.string(),
  status: z.enum(["success", "error"]),
  request_id: z.string(),
  data: z.unknown().nullable(),
  error: z.object({ message: z.string() }).passthrough().nullable(),
  meta: z.unknown()
});

export type ToolResponse = z.infer<typeof toolResponseSchema>;

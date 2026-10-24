// TypeScript view of schema/incident.schema.json (generated from src/pc_diag_mcp/models.py).

export const EVIDENCE_KINDS = ["event", "sensor", "process", "driver", "frametime"] as const;
export type EvidenceKind = (typeof EVIDENCE_KINDS)[number];

export type Severity = "info" | "warning" | "error" | "critical";

export type RootCauseCategory =
  | "gpu_driver_timeout"
  | "gpu_thermal"
  | "power_loss"
  | "hardware_error"
  | "kernel_driver_fault"
  | "memory_pressure"
  | "storage_io"
  | "application_hang"
  | "insufficient_evidence";

export interface GpuInfo {
  name: string;
  vendor: "amd" | "nvidia" | "intel" | "other";
  driver_version: string;
  driver_date?: string | null;
}

export interface SystemInfo {
  os_caption: string;
  os_build: string;
  gpus: GpuInfo[];
  utc_offset_minutes: number;
}

export interface RankedCause {
  category: RootCauseCategory;
  confidence: number;
  rationale: string;
  evidence_refs?: string[];
}

export interface BugReport {
  title: string;
  summary: string;
  os_build: string;
  gpu: string;
  driver_version: string;
  occurred_at: string;
  steps_to_reproduce: string[];
  expected: string;
  actual: string;
  evidence: string[];
  severity: "critical" | "high" | "medium" | "low";
}

export interface Diagnosis {
  window: { start: string; end: string };
  causes: RankedCause[];
  next_steps: string[];
  bug_report: BugReport;
}

export interface EvidenceItem {
  id: string;
  kind: EvidenceKind;
  time: string;
  label: string;
  detail?: string;
  severity?: Severity;
  value?: number | null;
  unit?: string | null;
}

export interface RunStats {
  config: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens?: number;
  cost_usd?: number | null;
  latency_ms: number;
}

export interface Incident {
  id: string;
  question: string;
  created_at: string;
  system: SystemInfo;
  diagnosis: Diagnosis;
  evidence: EvidenceItem[];
  run?: RunStats | null;
}

/** Row returned by GET /api/incidents. */
export interface IncidentSummary {
  id: string;
  created_at: string;
  question: string;
  primary_category: RootCauseCategory;
  confidence: number;
  os_build: string;
  gpu: string | null;
  evidence_count: number;
}

export interface IncidentPage {
  items: IncidentSummary[];
  total: number;
  limit: number;
  offset: number;
}

/** One row of evals/results/summary.json. */
export interface EvalConfigResult {
  config: string;
  model: string;
  split: string;
  cases: number;
  top1_accuracy: number;
  top3_accuracy: number;
  valid_rate: number;
  report_complete_rate: number;
  mean_input_tokens: number;
  mean_output_tokens: number;
  mean_cost_usd: number | null;
  mean_latency_ms: number;
  p95_latency_ms: number;
}

export interface EvalSummary {
  generated_at: string;
  configs: EvalConfigResult[];
}

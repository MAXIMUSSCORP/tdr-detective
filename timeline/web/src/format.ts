import type { RootCauseCategory } from "./types";

const CATEGORY_LABELS: Record<RootCauseCategory, string> = {
  gpu_driver_timeout: "GPU driver timeout (TDR)",
  gpu_thermal: "GPU thermal",
  power_loss: "Power loss",
  hardware_error: "Hardware error",
  kernel_driver_fault: "Kernel driver fault",
  memory_pressure: "Memory pressure",
  storage_io: "Storage I/O",
  application_hang: "Application hang",
  insufficient_evidence: "Insufficient evidence",
};

export function categoryLabel(category: RootCauseCategory): string {
  return CATEGORY_LABELS[category] ?? category;
}

function pad(n: number, width = 2): string {
  return String(n).padStart(width, "0");
}

/** Shift a UTC instant into the incident machine's local wall-clock time. */
function shifted(time: string | number, offsetMinutes: number): Date {
  const ms = typeof time === "number" ? time : Date.parse(time);
  return new Date(ms + offsetMinutes * 60_000);
}

/** "09:42:07" in the machine's local time. */
export function formatLocalTime(time: string | number, offsetMinutes: number, seconds = true): string {
  const d = shifted(time, offsetMinutes);
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  return seconds ? `${hm}:${pad(d.getUTCSeconds())}` : hm;
}

/** "2026-10-20 09:42:07" in the machine's local time. */
export function formatLocalDateTime(time: string | number, offsetMinutes: number): string {
  const d = shifted(time, offsetMinutes);
  const date = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  return `${date} ${formatLocalTime(time, offsetMinutes)}`;
}

/** "UTC−04:00" */
export function formatUtcOffset(offsetMinutes: number): string {
  const sign = offsetMinutes < 0 ? "−" : "+";
  const abs = Math.abs(offsetMinutes);
  return `UTC${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

export function formatPercent(ratio: number, digits = 0): string {
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function formatInteger(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

export function formatUsd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "n/a";
  return n < 1 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

export function formatDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function formatValue(value: number, unit: string | null | undefined): string {
  const rounded = Number.isInteger(value) ? String(value) : value.toFixed(1);
  if (!unit) return rounded;
  return unit.startsWith("°") || unit === "%" ? `${rounded}${unit}` : `${rounded} ${unit}`;
}

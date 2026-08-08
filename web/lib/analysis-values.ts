import type { AnalysisSource } from "./types";

function displayValue(value: unknown): string | null {
  if (typeof value === "string") {
    return value.trim() ? value : null;
  }
  if (typeof value === "number" || typeof value === "boolean" || value === null) {
    return String(value);
  }

  try {
    return JSON.stringify(value) ?? null;
  } catch {
    return null;
  }
}

export function analysisKeyPoints(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.flatMap((item) => {
    const display = displayValue(item);
    return display === null ? [] : [display];
  });
}

export function analysisSources(value: unknown): AnalysisSource[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.flatMap((item) => {
    if (
      typeof item !== "object" ||
      item === null ||
      typeof (item as { title?: unknown }).title !== "string" ||
      typeof (item as { url?: unknown }).url !== "string"
    ) {
      return [];
    }

    const note = (item as { note?: unknown }).note;
    return [
      {
        title: (item as { title: string }).title,
        url: (item as { url: string }).url,
        note: typeof note === "string" ? note : "",
      },
    ];
  });
}

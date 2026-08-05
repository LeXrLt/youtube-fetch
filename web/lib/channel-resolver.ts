import "server-only";

import { execFile } from "node:child_process";
import path from "node:path";

import { parseChannelInspectionPayload } from "./channel-input";
import type { ResolvedChannel } from "./types";

const PROJECT_ROOT = path.resolve(process.cwd(), "..");
const DEFAULT_PIPELINE_PYTHON = path.join(PROJECT_ROOT, "pipeline", ".venv", "bin", "python");
const PIPELINE_MAIN = path.join(PROJECT_ROOT, "pipeline", "main.py");
const INSPECTION_TIMEOUT_MS = 60_000;
const MAX_OUTPUT_BYTES = 1024 * 1024;

type CommandError = Error & {
  code?: number | string;
  stdout?: string;
  stderr?: string;
};

export class ChannelNotFoundError extends Error {}

export class ChannelInspectionError extends Error {}

export async function resolveYoutubeChannel(reference: string): Promise<ResolvedChannel> {
  const python = process.env.PIPELINE_PYTHON?.trim() || DEFAULT_PIPELINE_PYTHON;
  try {
    const stdout = await inspectCommand(python, reference);
    const payload = JSON.parse(stdout) as unknown;
    const channel = parseChannelInspectionPayload(payload);
    if (!channel) {
      throw new ChannelInspectionError("Pipeline returned invalid channel metadata");
    }
    return channel;
  } catch (error) {
    if (error instanceof ChannelInspectionError) {
      throw error;
    }

    const commandError = error as CommandError;
    if (commandError.code === 2 || isNotFoundPayload(commandError.stdout)) {
      throw new ChannelNotFoundError("YouTube channel could not be verified", {
        cause: error,
      });
    }
    throw new ChannelInspectionError("Pipeline channel inspection failed", {
      cause: error,
    });
  }
}

function inspectCommand(python: string, reference: string): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      python,
      [PIPELINE_MAIN, "--log-level", "ERROR", "channel-inspect", reference],
      {
        cwd: PROJECT_ROOT,
        encoding: "utf8",
        maxBuffer: MAX_OUTPUT_BYTES,
        timeout: INSPECTION_TIMEOUT_MS,
        windowsHide: true,
      },
      (error, stdout, stderr) => {
        if (error) {
          const commandError = error as CommandError;
          commandError.stdout = stdout;
          commandError.stderr = stderr;
          reject(commandError);
          return;
        }
        resolve(stdout.trim());
      },
    );
  });
}

function isNotFoundPayload(value: string | undefined): boolean {
  if (!value) {
    return false;
  }
  try {
    const payload = JSON.parse(value) as { error?: unknown };
    return payload.error === "channel_not_found";
  } catch {
    return false;
  }
}

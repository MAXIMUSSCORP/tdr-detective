// @ts-check
"use strict";

const path = require("node:path");

/** Absolute path of the `timeline/` directory. */
const TIMELINE_DIR = path.resolve(__dirname, "..", "..");

/** Absolute path of the repository root (one level above `timeline/`). */
const REPO_DIR = path.resolve(TIMELINE_DIR, "..");

const DEFAULT_PORT = 4173;
const DEFAULT_HOST = "127.0.0.1";

/**
 * Resolve runtime configuration from the environment.
 * Relative paths in env vars resolve against the current working directory;
 * defaults resolve against `timeline/` so they work from any cwd.
 *
 * @param {NodeJS.ProcessEnv} [env]
 */
function loadConfig(env = process.env) {
  const port = env.PORT === undefined || env.PORT === "" ? DEFAULT_PORT : Number(env.PORT);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error(`PORT must be an integer between 0 and 65535, got "${env.PORT}"`);
  }
  return {
    host: env.HOST || DEFAULT_HOST,
    port,
    dbPath: env.TDR_DB ? path.resolve(env.TDR_DB) : path.join(TIMELINE_DIR, "data", "tdr.db"),
    evalSummaryPath: env.TDR_EVAL_SUMMARY
      ? path.resolve(env.TDR_EVAL_SUMMARY)
      : path.resolve(TIMELINE_DIR, "..", "evals", "results", "summary.json"),
    schemaPath: path.join(REPO_DIR, "schema", "incident.schema.json"),
    webDistPath: path.join(TIMELINE_DIR, "web", "dist"),
  };
}

module.exports = { loadConfig, TIMELINE_DIR, REPO_DIR };

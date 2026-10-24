// @ts-check
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const express = require("express");
const { createRepository } = require("./db");
const { INCIDENT_ID_PATTERN } = require("./schema");

const DEFAULT_LIMIT = 20;
const MAX_LIMIT = 100;
const MAX_OFFSET = 1_000_000;

/** @type {Record<string, string>} */
const CLIENT_ERROR_MESSAGES = {
  "entity.parse.failed": "request body is not valid JSON",
  "entity.too.large": "request body exceeds 1mb",
  "encoding.unsupported": "unsupported request encoding",
  "charset.unsupported": "unsupported request charset",
};

/**
 * @typedef {import("express").Request} Request
 * @typedef {import("express").Response} Response
 * @typedef {import("express").NextFunction} NextFunction
 *
 * @typedef {object} AppOptions
 * @property {import("better-sqlite3").Database} db
 * @property {(body: unknown) => import("./schema").ValidationResult} validateIncident
 * @property {readonly string[]} evidenceKinds
 * @property {string} evalSummaryPath
 * @property {string | null} [webDistPath] directory of the built web app to serve, if any
 */

/** An error whose message and status are safe to send to the client. */
class HttpError extends Error {
  /**
   * @param {number} status
   * @param {string} message
   * @param {unknown} [details]
   */
  constructor(status, message, details) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

/**
 * Parse an optional non-negative integer query parameter within [min, max].
 *
 * @param {unknown} raw
 * @param {string} name
 * @param {number} fallback
 * @param {number} min
 * @param {number} max
 */
function intParam(raw, name, fallback, min, max) {
  if (raw === undefined) return fallback;
  if (typeof raw !== "string" || !/^\d{1,7}$/.test(raw)) {
    throw new HttpError(400, `${name} must be an integer between ${min} and ${max}`);
  }
  const value = Number(raw);
  if (value < min || value > max) {
    throw new HttpError(400, `${name} must be an integer between ${min} and ${max}`);
  }
  return value;
}

/** @param {Request} req */
function incidentId(req) {
  const id = req.params.id;
  if (typeof id !== "string" || !INCIDENT_ID_PATTERN.test(id)) {
    throw new HttpError(400, "invalid incident id");
  }
  return id;
}

/** Minimal hardening headers; the app is same-origin only, so no CORS headers are sent. */
function securityHeaders(/** @type {Request} */ _req, /** @type {Response} */ res, /** @type {NextFunction} */ next) {
  res.set({
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy":
      "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
  });
  next();
}

/**
 * Read and minimally shape-check the eval summary file.
 *
 * @param {string} file
 */
async function readEvalSummary(file) {
  let text;
  try {
    text = await fs.promises.readFile(file, "utf8");
  } catch (err) {
    if (/** @type {NodeJS.ErrnoException} */ (err).code === "ENOENT") {
      throw new HttpError(404, "no eval results yet");
    }
    throw err;
  }
  let summary;
  try {
    summary = JSON.parse(text);
  } catch {
    throw new HttpError(500, "eval summary is not valid JSON");
  }
  if (!summary || typeof summary !== "object" || !Array.isArray(summary.configs)) {
    throw new HttpError(500, "eval summary is malformed");
  }
  return summary;
}

/**
 * Build the Express application. Pure with respect to the environment:
 * everything it needs is passed in, which keeps tests hermetic.
 *
 * @param {AppOptions} options
 */
function createApp({ db, validateIncident, evidenceKinds, evalSummaryPath, webDistPath = null }) {
  const repo = createRepository(db);
  const app = express();
  app.disable("x-powered-by");
  app.set("query parser", "simple");
  app.use(securityHeaders);

  const api = express.Router();
  api.use(express.json({ limit: "1mb" }));

  api.get("/health", (_req, res) => {
    res.json({ status: "ok" });
  });

  api.post("/incidents", (req, res) => {
    const result = validateIncident(req.body);
    if (!result.ok) {
      throw new HttpError(400, "incident failed validation", result.errors);
    }
    const incident = /** @type {import("./db").Incident} */ (req.body);
    const { created } = repo.saveIncident(incident);
    res
      .status(201)
      .location(`/api/incidents/${encodeURIComponent(incident.id)}`)
      .json({ id: incident.id, created });
  });

  api.get("/incidents", (req, res) => {
    const limit = intParam(req.query.limit, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT);
    const offset = intParam(req.query.offset, "offset", 0, 0, MAX_OFFSET);
    const { items, total } = repo.listIncidents(limit, offset);
    res.json({ items, total, limit, offset });
  });

  api.get("/incidents/:id", (req, res) => {
    const incident = repo.getIncident(incidentId(req));
    if (!incident) throw new HttpError(404, "incident not found");
    res.json(incident);
  });

  api.get("/incidents/:id/evidence", (req, res) => {
    const id = incidentId(req);
    const { kind } = req.query;
    if (kind !== undefined && (typeof kind !== "string" || !evidenceKinds.includes(kind))) {
      throw new HttpError(400, `kind must be one of: ${evidenceKinds.join(", ")}`);
    }
    const items = repo.listEvidence(id, kind);
    if (!items) throw new HttpError(404, "incident not found");
    res.json({ items });
  });

  api.get("/evals", async (_req, res) => {
    res.json(await readEvalSummary(evalSummaryPath));
  });

  api.use((_req, _res) => {
    throw new HttpError(404, "not found");
  });

  app.use("/api", api);

  if (webDistPath && fs.existsSync(path.join(webDistPath, "index.html"))) {
    app.use(express.static(webDistPath, { index: "index.html", maxAge: "1h" }));
  }

  app.use((/** @type {Request} */ _req, /** @type {Response} */ res) => {
    res.status(404).json({ error: "not found" });
  });

  // Express recognizes error handlers by arity, so all four parameters must stay.
  app.use(
    (
      /** @type {any} */ err,
      /** @type {Request} */ _req,
      /** @type {Response} */ res,
      /** @type {NextFunction} */ _next,
    ) => {
      if (err instanceof HttpError) {
        const body = err.details === undefined ? { error: err.message } : { error: err.message, details: err.details };
        res.status(err.status).json(body);
        return;
      }
      // body-parser errors carry a client-safe 4xx status (malformed JSON, body too large, ...).
      const status = typeof err?.status === "number" && err.status >= 400 && err.status < 500 ? err.status : 500;
      if (status === 500) {
        console.error(err);
      }
      res.status(status).json({ error: CLIENT_ERROR_MESSAGES[err?.type] ?? (status === 500 ? "internal server error" : "bad request") });
    },
  );

  return app;
}

module.exports = { createApp, HttpError, DEFAULT_LIMIT, MAX_LIMIT };

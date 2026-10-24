// @ts-check
"use strict";

const fs = require("node:fs");
const Ajv2020 = require("ajv/dist/2020").default;
const addFormats = require("ajv-formats").default;

/**
 * Incident ids end up in URLs, so they are restricted to a URL-safe alphabet.
 * The Python model does not constrain them; this is the API's own rule.
 */
const INCIDENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

/** Upper bound on validation issues echoed to the client, so error bodies stay small. */
const MAX_REPORTED_ERRORS = 20;

/**
 * @typedef {{ instancePath: string, message: string, params?: Record<string, unknown> }} ValidationIssue
 * @typedef {{ ok: true } | { ok: false, errors: ValidationIssue[] }} ValidationResult
 */

/**
 * Load `schema/incident.schema.json` and compile a validator for it.
 * The file is the contract shared with the Python side and is used verbatim.
 *
 * @param {string} schemaPath
 */
function loadIncidentSchema(schemaPath) {
  /** @type {any} */
  const schema = JSON.parse(fs.readFileSync(schemaPath, "utf8"));
  // allErrors stays off: stopping at the first failure bounds both CPU time and response size.
  const ajv = new Ajv2020({ allErrors: false, strict: true });
  addFormats(ajv, ["date-time"]);
  const validateSchema = ajv.compile(schema);

  /** @type {readonly string[]} */
  const evidenceKinds = Object.freeze([...schema.$defs.EvidenceKind.enum]);

  /**
   * Validate a parsed request body as an Incident.
   * Schema errors come first; semantic checks run only on schema-valid input.
   *
   * @param {unknown} body
   * @returns {ValidationResult}
   */
  function validateIncident(body) {
    if (!validateSchema(body)) {
      const errors = (validateSchema.errors ?? []).slice(0, MAX_REPORTED_ERRORS).map((e) => ({
        instancePath: e.instancePath,
        message: e.message ?? "is invalid",
        params: e.params,
      }));
      return { ok: false, errors };
    }
    /** @type {any} */
    const incident = body;
    /** @type {ValidationIssue[]} */
    const errors = [];
    if (!INCIDENT_ID_PATTERN.test(incident.id)) {
      errors.push({ instancePath: "/id", message: `must match ${INCIDENT_ID_PATTERN.source}` });
    }
    // RFC 3339 allows leap seconds (":60") but JavaScript dates cannot represent them.
    /** @type {[string, string][]} */
    const timestamps = [
      ["/created_at", incident.created_at],
      ["/diagnosis/window/start", incident.diagnosis.window.start],
      ["/diagnosis/window/end", incident.diagnosis.window.end],
      ["/diagnosis/bug_report/occurred_at", incident.diagnosis.bug_report.occurred_at],
      ...incident.evidence.map((/** @type {{ time: string }} */ e, /** @type {number} */ i) => [`/evidence/${i}/time`, e.time]),
    ];
    for (const [instancePath, value] of timestamps) {
      if (Number.isNaN(Date.parse(value))) {
        errors.push({ instancePath, message: "must be a representable date-time" });
      }
    }
    const seen = new Set();
    incident.evidence.forEach((/** @type {{ id: string }} */ item, /** @type {number} */ index) => {
      if (seen.has(item.id)) {
        errors.push({ instancePath: `/evidence/${index}/id`, message: `duplicate evidence id "${item.id}"` });
      }
      seen.add(item.id);
    });
    return errors.length ? { ok: false, errors: errors.slice(0, MAX_REPORTED_ERRORS) } : { ok: true };
  }

  return { validateIncident, evidenceKinds };
}

module.exports = { loadIncidentSchema, INCIDENT_ID_PATTERN, MAX_REPORTED_ERRORS };

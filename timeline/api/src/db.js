// @ts-check
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const Database = require("better-sqlite3");

/**
 * Ordered schema migrations. `PRAGMA user_version` records how many have run;
 * append new entries, never edit applied ones.
 */
const MIGRATIONS = [
  `
  CREATE TABLE incidents (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    question         TEXT NOT NULL,
    primary_category TEXT NOT NULL,
    confidence       REAL NOT NULL,
    os_build         TEXT NOT NULL,
    gpu              TEXT,
    json             TEXT NOT NULL
  ) STRICT;
  CREATE INDEX idx_incidents_created_at ON incidents (created_at DESC, id);

  CREATE TABLE evidence (
    incident_id TEXT NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    time        TEXT NOT NULL,
    label       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    severity    TEXT NOT NULL,
    value       REAL,
    unit        TEXT,
    PRIMARY KEY (incident_id, evidence_id)
  ) STRICT;
  CREATE INDEX idx_evidence_time ON evidence (incident_id, time);
  CREATE INDEX idx_evidence_kind ON evidence (incident_id, kind, time);
  `,
];

/**
 * @typedef {import("better-sqlite3").Database} SqliteDatabase
 *
 * @typedef {object} EvidenceItem
 * @property {string} id
 * @property {string} kind
 * @property {string} time
 * @property {string} label
 * @property {string} [detail]
 * @property {string} [severity]
 * @property {number | null} [value]
 * @property {string | null} [unit]
 *
 * @typedef {object} Incident
 * @property {string} id
 * @property {string} question
 * @property {string} created_at
 * @property {{ os_build: string, gpus: { name: string, driver_version: string }[] }} system
 * @property {{ causes: { category: string, confidence: number }[] }} diagnosis
 * @property {EvidenceItem[]} evidence
 *
 * @typedef {object} IncidentSummary
 * @property {string} id
 * @property {string} created_at
 * @property {string} question
 * @property {string} primary_category
 * @property {number} confidence
 * @property {string} os_build
 * @property {string | null} gpu
 * @property {number} evidence_count
 */

/**
 * Open (creating if needed) the SQLite database and bring its schema up to date.
 *
 * @param {string} dbPath file path, or ":memory:"
 * @returns {SqliteDatabase}
 */
function openDatabase(dbPath) {
  if (dbPath !== ":memory:") {
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  }
  const db = new Database(dbPath);
  db.pragma("journal_mode = WAL");
  db.pragma("foreign_keys = ON");
  migrate(db);
  return db;
}

/** @param {SqliteDatabase} db */
function migrate(db) {
  const current = Number(db.pragma("user_version", { simple: true }));
  for (let version = current; version < MIGRATIONS.length; version += 1) {
    db.transaction(() => {
      db.exec(MIGRATIONS[version]);
      // PRAGMA cannot take bound parameters; the value is a loop counter, not input.
      db.pragma(`user_version = ${version + 1}`);
    })();
  }
}

/** Normalize an RFC 3339 timestamp to UTC ISO-8601 so string order is time order. */
function toUtcIso(/** @type {string} */ value) {
  return new Date(value).toISOString();
}

/**
 * Prepared statements and queries over an open database.
 *
 * @param {SqliteDatabase} db
 */
function createRepository(db) {
  const upsertIncident = db.prepare(`
    INSERT INTO incidents (id, created_at, question, primary_category, confidence, os_build, gpu, json)
    VALUES (@id, @created_at, @question, @primary_category, @confidence, @os_build, @gpu, @json)
    ON CONFLICT (id) DO UPDATE SET
      created_at = excluded.created_at,
      question = excluded.question,
      primary_category = excluded.primary_category,
      confidence = excluded.confidence,
      os_build = excluded.os_build,
      gpu = excluded.gpu,
      json = excluded.json
  `);
  const incidentExists = db.prepare("SELECT 1 FROM incidents WHERE id = ?").pluck();
  const deleteEvidence = db.prepare("DELETE FROM evidence WHERE incident_id = ?");
  const insertEvidence = db.prepare(`
    INSERT INTO evidence (incident_id, evidence_id, kind, time, label, detail, severity, value, unit)
    VALUES (@incident_id, @evidence_id, @kind, @time, @label, @detail, @severity, @value, @unit)
  `);
  const countIncidents = db.prepare("SELECT COUNT(*) FROM incidents").pluck();
  const listIncidents = db.prepare(`
    SELECT i.id, i.created_at, i.question, i.primary_category, i.confidence, i.os_build, i.gpu,
           (SELECT COUNT(*) FROM evidence e WHERE e.incident_id = i.id) AS evidence_count
    FROM incidents i
    ORDER BY i.created_at DESC, i.id
    LIMIT ? OFFSET ?
  `);
  const getIncidentJson = db.prepare("SELECT json FROM incidents WHERE id = ?").pluck();
  const evidenceColumns = `
    SELECT evidence_id AS id, kind, time, label, detail, severity, value, unit
    FROM evidence WHERE incident_id = ?`;
  const listEvidence = db.prepare(`${evidenceColumns} ORDER BY time, evidence_id`);
  const listEvidenceByKind = db.prepare(`${evidenceColumns} AND kind = ? ORDER BY time, evidence_id`);

  const saveTransaction = db.transaction((/** @type {Incident} */ incident) => {
    const existed = Boolean(incidentExists.get(incident.id));
    const primary = incident.diagnosis.causes[0];
    const gpu = incident.system.gpus[0];
    upsertIncident.run({
      id: incident.id,
      created_at: toUtcIso(incident.created_at),
      question: incident.question,
      primary_category: primary.category,
      confidence: primary.confidence,
      os_build: incident.system.os_build,
      gpu: gpu ? gpu.name : null,
      json: JSON.stringify(incident),
    });
    deleteEvidence.run(incident.id);
    for (const item of incident.evidence) {
      insertEvidence.run({
        incident_id: incident.id,
        evidence_id: item.id,
        kind: item.kind,
        time: toUtcIso(item.time),
        label: item.label,
        detail: item.detail ?? "",
        severity: item.severity ?? "info",
        value: item.value ?? null,
        unit: item.unit ?? null,
      });
    }
    return { created: !existed };
  });

  return {
    /**
     * Insert or replace an incident and its evidence rows atomically.
     * @param {Incident} incident schema-valid incident
     * @returns {{ created: boolean }}
     */
    saveIncident(incident) {
      return saveTransaction(incident);
    },

    /**
     * @param {number} limit
     * @param {number} offset
     * @returns {{ items: IncidentSummary[], total: number }}
     */
    listIncidents(limit, offset) {
      const items = /** @type {IncidentSummary[]} */ (listIncidents.all(limit, offset));
      return { items, total: Number(countIncidents.get()) };
    },

    /**
     * @param {string} id
     * @returns {Incident | null}
     */
    getIncident(id) {
      const json = getIncidentJson.get(id);
      return typeof json === "string" ? JSON.parse(json) : null;
    },

    /**
     * @param {string} id
     * @param {string} [kind]
     * @returns {EvidenceItem[] | null} null when the incident does not exist
     */
    listEvidence(id, kind) {
      if (!incidentExists.get(id)) return null;
      const rows = kind ? listEvidenceByKind.all(id, kind) : listEvidence.all(id);
      return /** @type {EvidenceItem[]} */ (rows);
    },
  };
}

module.exports = { openDatabase, createRepository };

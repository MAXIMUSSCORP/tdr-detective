// @ts-check
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { loadConfig } = require("./config");
const { openDatabase, createRepository } = require("./db");
const { loadIncidentSchema } = require("./schema");

const SEED_FILE = path.join(__dirname, "..", "seed", "demo-incident.json");

function main() {
  const config = loadConfig();
  const { validateIncident } = loadIncidentSchema(config.schemaPath);
  const incident = JSON.parse(fs.readFileSync(SEED_FILE, "utf8"));

  const result = validateIncident(incident);
  if (!result.ok) {
    console.error(`Seed incident is invalid:\n${JSON.stringify(result.errors, null, 2)}`);
    process.exitCode = 1;
    return;
  }

  const db = openDatabase(config.dbPath);
  try {
    const { created } = createRepository(db).saveIncident(incident);
    console.log(`${created ? "Inserted" : "Replaced"} incident "${incident.id}" in ${config.dbPath}`);
  } finally {
    db.close();
  }
}

main();

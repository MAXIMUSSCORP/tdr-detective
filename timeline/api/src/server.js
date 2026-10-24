// @ts-check
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { loadConfig } = require("./config");
const { openDatabase } = require("./db");
const { loadIncidentSchema } = require("./schema");
const { createApp } = require("./app");

function main() {
  const config = loadConfig();
  const { validateIncident, evidenceKinds } = loadIncidentSchema(config.schemaPath);
  const db = openDatabase(config.dbPath);
  const app = createApp({
    db,
    validateIncident,
    evidenceKinds,
    evalSummaryPath: config.evalSummaryPath,
    webDistPath: config.webDistPath,
  });

  if (!fs.existsSync(path.join(config.webDistPath, "index.html"))) {
    console.warn(`No web build at ${config.webDistPath}; serving the API only. Run "npm run build" first.`);
  }

  const server = app.listen(config.port, config.host, () => {
    console.log(`TDR timeline listening on http://${config.host}:${config.port} (db: ${config.dbPath})`);
  });
  server.on("error", (err) => {
    console.error(`Failed to start server: ${err.message}`);
    db.close();
    process.exitCode = 1;
  });

  const shutdown = () => {
    server.close(() => {
      db.close();
    });
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

main();

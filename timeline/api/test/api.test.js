// @ts-check
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const request = require("supertest");
const { createApp } = require("../src/app");
const { loadConfig } = require("../src/config");
const { openDatabase } = require("../src/db");
const { loadIncidentSchema } = require("../src/schema");

const { schemaPath } = loadConfig({});
const { validateIncident, evidenceKinds } = loadIncidentSchema(schemaPath);
const demoIncident = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "seed", "demo-incident.json"), "utf8"));

/** A small incident a client could POST; built fresh so tests can mutate it. */
function exampleIncident(overrides = {}) {
  return {
    id: "inc-2026-10-20-0942",
    question: "Why did my screen go black at 9:42?",
    created_at: "2026-10-20T13:50:00Z",
    system: {
      os_caption: "Microsoft Windows 11 Pro",
      os_build: "26100.4652",
      gpus: [{ name: "AMD Radeon RX 7800 XT", vendor: "amd", driver_version: "32.0.21013.1000" }],
      utc_offset_minutes: -240,
    },
    diagnosis: {
      window: { start: "2026-10-20T13:35:00Z", end: "2026-10-20T13:45:00Z" },
      causes: [
        { category: "gpu_driver_timeout", confidence: 0.8, rationale: "4101 then LiveKernelEvent 141.", evidence_refs: ["e1", "e2"] },
        { category: "gpu_thermal", confidence: 0.15, rationale: "Hotspot near limit.", evidence_refs: ["s1"] },
      ],
      next_steps: ["Reinstall the display driver."],
      bug_report: {
        title: "TDR on amdkmdag",
        summary: "Display driver timeout during gameplay.",
        os_build: "26100.4652",
        gpu: "AMD Radeon RX 7800 XT",
        driver_version: "32.0.21013.1000",
        occurred_at: "2026-10-20T13:42:07Z",
        steps_to_reproduce: ["Play a GPU-bound game for ten minutes."],
        expected: "No driver reset.",
        actual: "Black screen and driver reset.",
        evidence: ["e1", "e2"],
        severity: "high",
      },
    },
    evidence: [
      { id: "e2", kind: "event", time: "2026-10-20T13:42:09Z", label: "LiveKernelEvent 141", severity: "error" },
      { id: "e1", kind: "event", time: "2026-10-20T13:42:07Z", label: "Display 4101", severity: "warning" },
      { id: "s1", kind: "sensor", time: "2026-10-20T13:42:04Z", label: "GPU hotspot", value: 106, unit: "°C" },
      { id: "d1", kind: "driver", time: "2026-10-20T13:35:10Z", label: "amdkmdag 32.0.21013.1000" },
    ],
    ...overrides,
  };
}

/** @type {string} */
let tmpDir;
/** @type {import("better-sqlite3").Database} */
let db;

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "tdr-timeline-"));
  db = openDatabase(":memory:");
});

afterEach(() => {
  db.close();
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

/** @param {{ evalSummaryPath?: string, webDistPath?: string | null }} [options] */
function makeApp(options = {}) {
  return createApp({
    db,
    validateIncident,
    evidenceKinds,
    evalSummaryPath: options.evalSummaryPath ?? path.join(tmpDir, "missing-summary.json"),
    webDistPath: options.webDistPath ?? null,
  });
}

describe("GET /api/health", () => {
  it("reports ok without CORS headers", async () => {
    const res = await request(makeApp()).get("/api/health").set("Origin", "http://evil.example");
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ status: "ok" });
    expect(res.headers["access-control-allow-origin"]).toBeUndefined();
    expect(res.headers["x-powered-by"]).toBeUndefined();
  });
});

describe("POST /api/incidents", () => {
  it("accepts the POST-able example and returns it unchanged", async () => {
    const app = makeApp();
    const incident = exampleIncident();
    const res = await request(app).post("/api/incidents").send(incident);
    expect(res.status).toBe(201);
    expect(res.body).toEqual({ id: incident.id, created: true });
    expect(res.headers.location).toBe(`/api/incidents/${incident.id}`);

    const fetched = await request(app).get(`/api/incidents/${incident.id}`);
    expect(fetched.status).toBe(200);
    expect(fetched.body).toEqual(incident);
  });

  it("accepts the seeded demo incident", async () => {
    const res = await request(makeApp()).post("/api/incidents").send(demoIncident);
    expect(res.status).toBe(201);
  });

  it.each([
    ["missing required field", () => { const i = /** @type {any} */ (exampleIncident()); delete i.diagnosis; return i; }, ""],
    ["unknown property", () => exampleIncident({ extra: true }), ""],
    ["timestamp without timezone", () => exampleIncident({ created_at: "2026-10-20T13:50:00" }), "/created_at"],
    ["evidence kind outside the enum", () => { const i = exampleIncident(); i.evidence[0].kind = "registry"; return i; }, "/evidence/0/kind"],
    ["confidence above 1", () => { const i = exampleIncident(); i.diagnosis.causes[0].confidence = 1.5; return i; }, "/diagnosis/causes/0/confidence"],
    ["empty causes", () => { const i = exampleIncident(); i.diagnosis.causes = []; return i; }, "/diagnosis/causes"],
  ])("rejects %s with ajv errors", async (_name, build, instancePath) => {
    const res = await request(makeApp()).post("/api/incidents").send(build());
    expect(res.status).toBe(400);
    expect(res.body.error).toBe("incident failed validation");
    expect(res.body.details.length).toBeGreaterThan(0);
    expect(res.body.details.map((/** @type {any} */ d) => d.instancePath)).toContain(instancePath);
  });

  it("rejects ids that are not URL-safe", async () => {
    const res = await request(makeApp()).post("/api/incidents").send(exampleIncident({ id: "../../etc/passwd" }));
    expect(res.status).toBe(400);
    expect(res.body.details[0].instancePath).toBe("/id");
  });

  it("rejects schema-valid timestamps that cannot be stored, such as leap seconds", async () => {
    const incident = exampleIncident({ created_at: "2026-12-31T23:59:60Z" });
    incident.evidence[2].time = "2026-12-31T23:59:60+00:00";
    const res = await request(makeApp()).post("/api/incidents").send(incident);
    expect(res.status).toBe(400);
    expect(res.body.details.map((/** @type {any} */ d) => d.instancePath)).toEqual(["/created_at", "/evidence/2/time"]);
  });

  it("rejects duplicate evidence ids", async () => {
    const incident = exampleIncident();
    incident.evidence[1].id = incident.evidence[0].id;
    const res = await request(makeApp()).post("/api/incidents").send(incident);
    expect(res.status).toBe(400);
    expect(res.body.details[0].message).toMatch(/duplicate evidence id/);
  });

  it("rejects malformed JSON without leaking internals", async () => {
    const res = await request(makeApp())
      .post("/api/incidents")
      .set("Content-Type", "application/json")
      .send('{"id": "x",');
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "request body is not valid JSON" });
    expect(res.text).not.toMatch(/at .*\.js/);
  });

  it("rejects bodies over 1mb", async () => {
    const incident = exampleIncident({ question: "x".repeat(1024 * 1024) });
    const res = await request(makeApp()).post("/api/incidents").send(incident);
    expect(res.status).toBe(413);
    expect(res.body).toEqual({ error: "request body exceeds 1mb" });
  });

  it("upserts: a second POST with the same id replaces the incident and its evidence", async () => {
    const app = makeApp();
    await request(app).post("/api/incidents").send(exampleIncident()).expect(201);

    const revised = exampleIncident({ question: "Revised question" });
    revised.evidence = revised.evidence.filter((e) => e.kind === "event");
    const res = await request(app).post("/api/incidents").send(revised);
    expect(res.status).toBe(201);
    expect(res.body.created).toBe(false);

    const list = await request(app).get("/api/incidents");
    expect(list.body.total).toBe(1);
    expect(list.body.items[0]).toMatchObject({ question: "Revised question", evidence_count: 2 });
    const evidence = await request(app).get(`/api/incidents/${revised.id}/evidence`);
    expect(evidence.body.items.map((/** @type {any} */ e) => e.id)).toEqual(["e1", "e2"]);
  });
});

describe("GET /api/incidents", () => {
  async function seedThree() {
    const app = makeApp();
    // Mixed offsets: 13:00Z, 12:30Z (written as +02:00) and 14:00Z.
    const times = { a: "2026-10-20T13:00:00Z", b: "2026-10-20T14:30:00+02:00", c: "2026-10-20T14:00:00Z" };
    for (const [id, created_at] of Object.entries(times)) {
      await request(app).post("/api/incidents").send(exampleIncident({ id, created_at })).expect(201);
    }
    return app;
  }

  it("lists summaries newest first", async () => {
    const res = await request(await seedThree()).get("/api/incidents");
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ total: 3, limit: 20, offset: 0 });
    expect(res.body.items.map((/** @type {any} */ i) => i.id)).toEqual(["c", "a", "b"]);
    expect(res.body.items[0]).toEqual({
      id: "c",
      created_at: "2026-10-20T14:00:00.000Z",
      question: "Why did my screen go black at 9:42?",
      primary_category: "gpu_driver_timeout",
      confidence: 0.8,
      os_build: "26100.4652",
      gpu: "AMD Radeon RX 7800 XT",
      evidence_count: 4,
    });
  });

  it("pages with limit and offset", async () => {
    const res = await request(await seedThree()).get("/api/incidents?limit=1&offset=1");
    expect(res.status).toBe(200);
    expect(res.body.items.map((/** @type {any} */ i) => i.id)).toEqual(["a"]);
    expect(res.body).toMatchObject({ total: 3, limit: 1, offset: 1 });
  });

  it.each(["limit=0", "limit=101", "limit=abc", "limit=1.5", "limit=-1", "offset=-1", "offset=1000001", "limit=1&limit=2"])(
    "rejects out-of-bounds paging (%s)",
    async (query) => {
      const res = await request(makeApp()).get(`/api/incidents?${query}`);
      expect(res.status).toBe(400);
      expect(res.body.error).toMatch(/must be an integer/);
    },
  );

  it("accepts the maximum limit", async () => {
    const res = await request(makeApp()).get("/api/incidents?limit=100");
    expect(res.status).toBe(200);
    expect(res.body.items).toEqual([]);
  });
});

describe("GET /api/incidents/:id", () => {
  it("returns 404 for an unknown incident", async () => {
    const res = await request(makeApp()).get("/api/incidents/does-not-exist");
    expect(res.status).toBe(404);
    expect(res.body).toEqual({ error: "incident not found" });
  });

  it.each([
    "1'%20OR%20'1'%3D'1",
    "x%3B%20DROP%20TABLE%20incidents%3B--",
    "%22%20UNION%20SELECT%20json%20FROM%20incidents--",
    "-leading-dash",
    "a".repeat(129),
  ])("rejects injection-shaped or malformed id %s", async (id) => {
    const app = makeApp();
    await request(app).post("/api/incidents").send(exampleIncident()).expect(201);

    const res = await request(app).get(`/api/incidents/${id}`);
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "invalid incident id" });
    const evidence = await request(app).get(`/api/incidents/${id}/evidence`);
    expect(evidence.status).toBe(400);

    const list = await request(app).get("/api/incidents");
    expect(list.body.total).toBe(1);
  });
});

describe("GET /api/incidents/:id/evidence", () => {
  it("returns all evidence ordered by time", async () => {
    const app = makeApp();
    await request(app).post("/api/incidents").send(exampleIncident()).expect(201);
    const res = await request(app).get("/api/incidents/inc-2026-10-20-0942/evidence");
    expect(res.status).toBe(200);
    expect(res.body.items.map((/** @type {any} */ e) => e.id)).toEqual(["d1", "s1", "e1", "e2"]);
    expect(res.body.items[1]).toEqual({
      id: "s1",
      kind: "sensor",
      time: "2026-10-20T13:42:04.000Z",
      label: "GPU hotspot",
      detail: "",
      severity: "info",
      value: 106,
      unit: "°C",
    });
  });

  it("filters by kind", async () => {
    const app = makeApp();
    await request(app).post("/api/incidents").send(demoIncident).expect(201);
    const res = await request(app).get(`/api/incidents/${demoIncident.id}/evidence?kind=sensor`);
    expect(res.status).toBe(200);
    expect(res.body.items).toHaveLength(8);
    expect(res.body.items.every((/** @type {any} */ e) => e.kind === "sensor")).toBe(true);
    const times = res.body.items.map((/** @type {any} */ e) => e.time);
    expect([...times].sort()).toEqual(times);
  });

  it.each(["kind=registry", "kind=", "kind=event&kind=sensor", "kind=event'--"])("rejects invalid kind (%s)", async (query) => {
    const app = makeApp();
    await request(app).post("/api/incidents").send(exampleIncident()).expect(201);
    const res = await request(app).get(`/api/incidents/inc-2026-10-20-0942/evidence?${query}`);
    expect(res.status).toBe(400);
    expect(res.body.error).toBe(`kind must be one of: ${evidenceKinds.join(", ")}`);
  });

  it("returns 404 for an unknown incident", async () => {
    const res = await request(makeApp()).get("/api/incidents/nope/evidence?kind=event");
    expect(res.status).toBe(404);
  });
});

describe("GET /api/evals", () => {
  const summary = {
    generated_at: "2026-10-21T08:00:00Z",
    configs: [
      { config: "skill_tools", model: "m", split: "test", cases: 40, top1_accuracy: 0.85, top3_accuracy: 0.95, valid_rate: 1, report_complete_rate: 0.9, mean_input_tokens: 18000, mean_output_tokens: 1300, mean_cost_usd: 0.08, mean_latency_ms: 21000, p95_latency_ms: 34000 },
    ],
  };

  it("returns the summary file when present", async () => {
    const file = path.join(tmpDir, "summary.json");
    fs.writeFileSync(file, JSON.stringify(summary));
    const res = await request(makeApp({ evalSummaryPath: file })).get("/api/evals");
    expect(res.status).toBe(200);
    expect(res.body).toEqual(summary);
  });

  it("returns 404 with an error message when absent", async () => {
    const res = await request(makeApp()).get("/api/evals");
    expect(res.status).toBe(404);
    expect(res.body).toEqual({ error: "no eval results yet" });
  });

  it("returns a generic 500 for a malformed file", async () => {
    const file = path.join(tmpDir, "summary.json");
    fs.writeFileSync(file, "{ not json");
    const res = await request(makeApp({ evalSummaryPath: file })).get("/api/evals");
    expect(res.status).toBe(500);
    expect(res.body).toEqual({ error: "eval summary is not valid JSON" });
  });
});

describe("routing", () => {
  it("returns JSON 404 for unknown API routes", async () => {
    const res = await request(makeApp()).get("/api/nope");
    expect(res.status).toBe(404);
    expect(res.body).toEqual({ error: "not found" });
  });

  it("serves the built web app when present", async () => {
    fs.writeFileSync(path.join(tmpDir, "index.html"), "<!doctype html><title>TDR timeline</title>");
    const res = await request(makeApp({ webDistPath: tmpDir })).get("/");
    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toMatch(/text\/html/);
    expect(res.headers["x-content-type-options"]).toBe("nosniff");
    expect(res.text).toContain("TDR timeline");
  });
});

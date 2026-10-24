import demo from "../../../api/seed/demo-incident.json";
import type { Incident } from "../types";

/** The seeded demo incident (validated against the schema by the API tests). */
export const demoIncident = demo as unknown as Incident;

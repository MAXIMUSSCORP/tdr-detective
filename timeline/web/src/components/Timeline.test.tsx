import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { demoIncident } from "../test/fixtures";
import { buildLanes, timeTicks } from "../timeline";
import { Timeline } from "./Timeline";

const offset = demoIncident.system.utc_offset_minutes;

function renderTimeline(highlighted: ReadonlySet<string> = new Set()) {
  return render(
    <Timeline
      evidence={demoIncident.evidence}
      offsetMinutes={offset}
      focusTime={Date.parse(demoIncident.diagnosis.bug_report.occurred_at)}
      highlighted={highlighted}
    />,
  );
}

describe("buildLanes", () => {
  it("groups evidence by kind in lane order, charting numeric sensor and frame-time values", () => {
    const lanes = buildLanes(demoIncident.evidence);
    expect(lanes.map((l) => l.kind)).toEqual(["driver", "process", "sensor", "frametime", "event"]);

    const sensor = lanes.find((l) => l.kind === "sensor")!;
    expect(sensor.markers).toEqual([]);
    expect(sensor.series).toHaveLength(1);
    expect(sensor.series[0]!.unit).toBe("°C");
    expect(sensor.series[0]!.points.map((p) => p.value)).toEqual([71, 79, 86, 93, 99, 103, 106, 74]);

    const events = lanes.find((l) => l.kind === "event")!;
    expect(events.series).toEqual([]);
    expect(events.markers.map((m) => m.id)).toEqual(["ev-display-4101", "ev-lke-141"]);
  });

  it("omits lanes for kinds with no evidence", () => {
    const lanes = buildLanes(demoIncident.evidence.filter((e) => e.kind === "event"));
    expect(lanes.map((l) => l.kind)).toEqual(["event"]);
  });

  it("aligns ticks to the machine's local clock", () => {
    const start = Date.parse("2026-10-20T13:41:07Z");
    const ticks = timeTicks([start, start + 60_000], 330);
    // UTC+05:30: 10s steps land on :10, :20 ... in local time.
    expect(ticks[0]).toBe(Date.parse("2026-10-20T13:41:10Z"));
    expect(ticks).toHaveLength(6);
  });
});

describe("<Timeline>", () => {
  it("renders one lane per evidence kind with markers for discrete evidence", () => {
    renderTimeline();
    for (const name of ["Driver", "Processes", "Sensors", "Frame time", "Events"]) {
      expect(screen.getByRole("group", { name })).toBeInTheDocument();
    }

    const events = screen.getByRole("group", { name: "Events" });
    const markers = within(events).getAllByRole("button");
    expect(markers).toHaveLength(2);
    expect(markers[0]).toHaveAccessibleName(/Display 4101.*warning, 09:42:07/);
    expect(markers[1]).toHaveAccessibleName(/LiveKernelEvent 141.*error, 09:42:09/);

    const sensors = screen.getByRole("group", { name: "Sensors" });
    expect(within(sensors).getByText("8 readings, 71°C to 106°C")).toBeInTheDocument();
    expect(within(sensors).queryAllByRole("button")).toHaveLength(0);
    expect(sensors.querySelectorAll("circle.dot")).toHaveLength(8);
    expect(screen.getByRole("group", { name: "Frame time" }).querySelectorAll("circle.dot")).toHaveLength(14);
  });

  it("shows label, detail and evidence id in a tooltip on hover", async () => {
    const user = userEvent.setup();
    renderTimeline();
    const marker = within(screen.getByRole("group", { name: "Events" })).getAllByRole("button")[1]!;

    await user.hover(marker);
    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("LiveKernelEvent 141 (VIDEO_ENGINE_TIMEOUT_DETECTED)");
    expect(tip).toHaveTextContent("Windows Error Reporting event 1001");
    expect(tip).toHaveTextContent("ev-lke-141");
    expect(marker).toHaveAttribute("aria-describedby", tip.id);

    await user.unhover(marker);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("opens the tooltip on keyboard focus or tap and dismisses it with Escape", async () => {
    const user = userEvent.setup();
    renderTimeline();
    const marker = within(screen.getByRole("group", { name: "Events" })).getAllByRole("button")[0]!;

    act(() => marker.focus());
    expect(screen.getByRole("tooltip")).toHaveTextContent("ev-display-4101");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    await user.click(marker);
    expect(screen.getByRole("tooltip")).toHaveTextContent("ev-display-4101");
  });

  it("drops markers outside the zoomed window", async () => {
    const user = userEvent.setup();
    renderTimeline();
    expect(within(screen.getByRole("group", { name: "Driver" })).getAllByRole("button")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "1 min" }));
    expect(screen.getByRole("button", { name: "1 min" })).toHaveAttribute("aria-pressed", "true");
    expect(within(screen.getByRole("group", { name: "Driver" })).queryAllByRole("button")).toHaveLength(0);
    expect(within(screen.getByRole("group", { name: "Events" })).getAllByRole("button")).toHaveLength(2);
  });
});

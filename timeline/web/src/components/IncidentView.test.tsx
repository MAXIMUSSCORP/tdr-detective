import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { demoIncident } from "../test/fixtures";
import { IncidentView } from "./IncidentView";

function highlightedIds(container: HTMLElement): string[] {
  return [...container.querySelectorAll<HTMLElement>('[data-highlighted="true"]')]
    .map((el) => el.dataset.evidenceId!)
    .sort();
}

describe("<IncidentView>", () => {
  it("shows the question, system and primary cause in the header", () => {
    render(<IncidentView incident={demoIncident} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(demoIncident.question);
    expect(screen.getByText(/Microsoft Windows 11 Pro, build 26100\.4652/)).toBeInTheDocument();
    expect(screen.getByText(/AMD Radeon RX 7800 XT, driver 32\.0\.21013\.1000/)).toBeInTheDocument();
    const header = screen.getByRole("banner");
    expect(within(header).getByText("GPU driver timeout (TDR)")).toBeInTheDocument();
    expect(within(header).getByText("71% confidence")).toBeInTheDocument();
  });

  it("highlights a cause's evidence on the timeline and toggles it off again", async () => {
    const user = userEvent.setup();
    const { container } = render(<IncidentView incident={demoIncident} />);
    const timeline = screen.getByRole("region", { name: "Timeline" });
    expect(highlightedIds(timeline)).toEqual([]);

    const causes = screen.getByRole("region", { name: "Ranked causes" });
    const [first, , third] = [...causes.querySelectorAll<HTMLElement>("li.cause")];
    await user.click(within(first!).getByRole("button", { name: "Highlight 5 on timeline" }));

    // Event markers and chart points alike carry the highlight.
    expect(highlightedIds(timeline)).toEqual(["drv-amdkmdag", "ev-display-4101", "ev-lke-141", "ft-12", "ft-13"]);
    const hitch = timeline.querySelector('circle[data-evidence-id="ft-13"]')!;
    expect(hitch).toHaveClass("is-highlighted");
    expect(hitch).toHaveAttribute("r", "6");
    expect(timeline.querySelector('circle[data-evidence-id="ft-01"]')).toHaveClass("is-dimmed");
    const game = container.querySelector('[data-evidence-id="pr-game"]')!;
    expect(game).toHaveAttribute("data-highlighted", "false");
    expect(game).toHaveClass("is-dimmed");
    expect(within(first!).getByRole("button", { name: "Clear highlight" })).toHaveAttribute("aria-pressed", "true");
    expect(within(timeline).getByRole("status")).toHaveTextContent("5 evidence items highlighted.");
    expect(within(timeline).getByRole("button", { name: /^Display 4101.*, highlighted$/ })).toBeInTheDocument();

    await user.click(within(timeline).getByRole("button", { name: "1 min" }));
    expect(within(timeline).getByRole("status")).toHaveTextContent("5 evidence items highlighted, 1 outside this zoom.");
    await user.click(within(timeline).getByRole("button", { name: "All" }));

    await user.click(within(third!).getByRole("button", { name: "pr-game" }));
    expect(highlightedIds(timeline)).toEqual(["pr-game"]);
    expect(within(first!).getByRole("button", { name: "Highlight 5 on timeline" })).toHaveAttribute("aria-pressed", "false");

    await user.click(within(third!).getByRole("button", { name: "pr-game" }));
    expect(highlightedIds(timeline)).toEqual([]);
  });
});

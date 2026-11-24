import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { integerTicks } from "./charts";
import { toggleRowProps } from "./ui";

describe("integerTicks", () => {
  it("uses a step of 1 for small ranges", () => {
    expect(integerTicks(-3, 3)).toEqual([-3, -2, -1, 0, 1, 2, 3]);
  });

  it("picks a nice step for wide ranges and stays inside the range", () => {
    const ticks = integerTicks(13, 60);
    expect(ticks[0]).toBe(15);
    expect(ticks.at(-1)).toBe(60);
    expect(ticks.length).toBeLessThanOrEqual(12);
    expect(ticks.every((t) => t % 5 === 0)).toBe(true);
  });

  it("returns no ticks for invalid input", () => {
    expect(integerTicks(5, 1)).toEqual([]);
    expect(integerTicks(Number.NaN, 3)).toEqual([]);
  });
});

function Row() {
  const [open, setOpen] = useState(false);
  return (
    <table>
      <tbody>
        <tr {...toggleRowProps(() => setOpen((v) => !v), open)} data-testid="row">
          <td>{open ? "open" : "closed"}</td>
        </tr>
      </tbody>
    </table>
  );
}

describe("toggleRowProps", () => {
  it("toggles on click, Enter and Space and is focusable", async () => {
    const user = userEvent.setup();
    render(<Row />);
    const row = screen.getByTestId("row");
    expect(row).toHaveAttribute("tabindex", "0");
    expect(row).toHaveAttribute("aria-expanded", "false");
    await user.click(row);
    expect(row).toHaveTextContent("open");
    row.focus();
    await user.keyboard("{Enter}");
    expect(row).toHaveTextContent("closed");
    await user.keyboard(" ");
    expect(row).toHaveTextContent("open");
    expect(row).toHaveAttribute("aria-expanded", "true");
  });

  it("adds nothing when there is nothing to toggle", () => {
    expect(toggleRowProps(undefined, false)).toEqual({});
  });
});

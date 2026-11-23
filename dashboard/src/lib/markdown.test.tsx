import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { parseBlocks, safeHref, SafeMarkdown } from "./markdown";

describe("parseBlocks", () => {
  it("splits paragraphs, headings and lists", () => {
    const blocks = parseBlocks("# Summary\nTeam 1 is\nstronger.\n\n- one\n- two\n\n1. first\n2. second\n> quoted");
    expect(blocks).toEqual([
      { kind: "heading", level: 1, text: "Summary" },
      { kind: "paragraph", text: "Team 1 is stronger." },
      { kind: "ul", items: ["one", "two"] },
      { kind: "ol", items: ["first", "second"] },
      { kind: "quote", text: "quoted" },
    ]);
  });

  it("handles CRLF and indented list continuations", () => {
    expect(parseBlocks("- a\r\n  continued\r\n- b")).toEqual([{ kind: "ul", items: ["a continued", "b"] }]);
  });
});

describe("SafeMarkdown", () => {
  it("renders inline formatting as elements", () => {
    const { container } = render(<SafeMarkdown text={"**bold** and *italic* and `code`"} />);
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.querySelector("em")?.textContent).toBe("italic");
    expect(container.querySelector("code")?.textContent).toBe("code");
  });

  it("never interprets HTML: tags are shown as text", () => {
    const { container } = render(<SafeMarkdown text={'<img src=x onerror="alert(1)"><script>alert(2)</script>'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
    expect(container.textContent).toContain("<script>alert(2)</script>");
  });

  it("only links http(s) URLs, with a safe rel", () => {
    const { container } = render(<SafeMarkdown text={"[news](https://example.com/a) and [bad](javascript:alert(1))"} />);
    const link = screen.getByRole("link", { name: "news" });
    expect(link).toHaveAttribute("href", "https://example.com/a");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(screen.queryByRole("link", { name: "bad" })).toBeNull();
    expect(container.textContent).toContain("[bad](javascript:alert(1))");
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });

  it("renders nothing for empty text", () => {
    const { container } = render(<SafeMarkdown text={"   "} />);
    expect(container.innerHTML).toBe("");
  });

  it("validates hrefs", () => {
    expect(safeHref("https://hltv.org/news/1")).toBe("https://hltv.org/news/1");
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,hi")).toBeNull();
    expect(safeHref("/relative")).toBeNull();
  });
});

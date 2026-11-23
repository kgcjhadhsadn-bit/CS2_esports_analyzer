/**
 * A deliberately small, SAFE Markdown renderer for Claude's rationale text.
 *
 * The text is untrusted (it is generated from scraped news). It is never handed to innerHTML:
 * the parser produces plain React elements, so any HTML in the input is shown as literal text.
 * Supported: paragraphs, `#`-`###` headings, `-` / `*` / `+` bullets, `1.` numbered lists,
 * `>` quotes, `**bold**`, `*italic*` / `_italic_`, `` `code` `` and `[text](https://...)` links
 * (http / https only; other schemes such as `javascript:` render as plain text).
 */
import type { ReactNode } from "react";

export type Block =
  | { kind: "heading"; level: 1 | 2 | 3; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "quote"; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] };

const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d{1,3}[.)]\s+(.*)$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const QUOTE = /^\s*>\s?(.*)$/;

/** Split Markdown text into blocks (pure; exported for tests). */
export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: { kind: "ul" | "ol"; items: string[] } | null = null;
  let quote: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
    paragraph = [];
  };
  const flushList = () => {
    if (list) blocks.push({ kind: list.kind, items: list.items });
    list = null;
  };
  const flushQuote = () => {
    if (quote.length) blocks.push({ kind: "quote", text: quote.join(" ") });
    quote = [];
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flushAll();
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      flushAll();
      const level = Math.min(3, heading[1]?.length ?? 1) as 1 | 2 | 3;
      blocks.push({ kind: "heading", level, text: heading[2] ?? "" });
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      flushParagraph();
      flushQuote();
      if (!list || list.kind !== kind) {
        flushList();
        list = { kind, items: [] };
      }
      list.items.push(((bullet ?? numbered)?.[1] ?? "").trim());
      continue;
    }
    const q = QUOTE.exec(line);
    if (q) {
      flushParagraph();
      flushList();
      quote.push((q[1] ?? "").trim());
      continue;
    }
    if (list && /^\s{2,}\S/.test(raw)) {
      // indented continuation of the previous list item
      const items: string[] = (list as { items: string[] }).items;
      items[items.length - 1] = `${items[items.length - 1] ?? ""} ${line.trim()}`;
      continue;
    }
    flushList();
    flushQuote();
    paragraph.push(line.trim());
  }
  flushAll();
  return blocks;
}

const INLINE = /(\*\*([^*]+?)\*\*|__([^_]+?)__|`([^`]+?)`|\[([^\]]+?)\]\(([^)\s]+?)\)|\*([^*\s][^*]*?)\*|(?<![A-Za-z0-9])_([^_\s][^_]*?)_(?![A-Za-z0-9]))/;

/** Only absolute http(s) URLs become links. */
export function safeHref(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

/** Inline Markdown to React nodes (text is always rendered as text nodes). */
export function renderInline(text: string, keyPrefix = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let i = 0;
  while (rest) {
    const m = INLINE.exec(rest);
    if (!m || m.index === undefined) {
      out.push(rest);
      break;
    }
    if (m.index > 0) out.push(rest.slice(0, m.index));
    const key = `${keyPrefix}-${i++}`;
    const [whole, , bold1, bold2, code, linkText, linkUrl, ital1, ital2] = m;
    if (bold1 !== undefined || bold2 !== undefined) {
      out.push(<strong key={key}>{renderInline(bold1 ?? bold2 ?? "", key)}</strong>);
    } else if (code !== undefined) {
      out.push(<code key={key}>{code}</code>);
    } else if (linkText !== undefined && linkUrl !== undefined) {
      const href = safeHref(linkUrl);
      out.push(
        href ? (
          <a key={key} href={href} target="_blank" rel="noopener noreferrer nofollow">
            {renderInline(linkText, key)}
          </a>
        ) : (
          <span key={key}>{whole}</span>
        ),
      );
    } else {
      out.push(<em key={key}>{renderInline(ital1 ?? ital2 ?? "", key)}</em>);
    }
    rest = rest.slice(m.index + whole.length);
  }
  return out;
}

export function SafeMarkdown({ text, className }: { text: string | null | undefined; className?: string }) {
  if (!text || !text.trim()) return null;
  const blocks = parseBlocks(text);
  return (
    <div className={className ?? "markdown"}>
      {blocks.map((block, idx) => {
        const key = `b${idx}`;
        switch (block.kind) {
          case "heading": {
            const Tag = (["h3", "h4", "h5"] as const)[block.level - 1] ?? "h5";
            return <Tag key={key}>{renderInline(block.text, key)}</Tag>;
          }
          case "ul":
            return (
              <ul key={key}>
                {block.items.map((item, j) => (
                  <li key={j}>{renderInline(item, `${key}-${j}`)}</li>
                ))}
              </ul>
            );
          case "ol":
            return (
              <ol key={key}>
                {block.items.map((item, j) => (
                  <li key={j}>{renderInline(item, `${key}-${j}`)}</li>
                ))}
              </ol>
            );
          case "quote":
            return <blockquote key={key}>{renderInline(block.text, key)}</blockquote>;
          default:
            return <p key={key}>{renderInline(block.text, key)}</p>;
        }
      })}
    </div>
  );
}

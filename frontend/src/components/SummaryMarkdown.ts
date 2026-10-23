import { createElement as h, type ReactNode } from "react";

// Deliberately limited to paragraphs, line breaks and emphasis. All content is
// React text; HTML, links, images and other Markdown constructs stay inert.
export function SummaryMarkdown({ text }: { text: string }) {
  function inline(value: string): ReactNode[] {
    const parts: ReactNode[] = [];
    let start = 0;
    for (const match of value.matchAll(/\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|\n/g)) {
      parts.push(value.slice(start, match.index));
      parts.push(match[0] === "\n" ? h("br", { key: match.index })
        : h(match[1] ? "strong" : "em", { key: match.index }, match[1] ?? match[2]));
      start = match.index! + match[0].length;
    }
    parts.push(value.slice(start));
    return parts;
  }
  return h("div", { className: "investigation-answer" },
    text.replace(/\r\n?/g, "\n").split(/\n[ \t]*\n+/).map((paragraph, index) =>
      h("p", { key: index }, inline(paragraph))));
}

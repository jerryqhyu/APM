import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo } from "react";

marked.setOptions({ gfm: true, breaks: false });

/** Renders markdown, sanitized: a node's body can be written by an agent. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text, { async: false })), [text]);
  return <div className={`markdown ${className ?? ""}`} dangerouslySetInnerHTML={{ __html: html }} />;
}

/** `text` without the headings that have nothing under them, such as a template's empty
 * "## Notes": on screen they read as content that isn't there. Code blocks are left alone.
 */
export function withoutEmptySections(text: string): string {
  const lines = text.split("\n");
  const level = (line: string) => /^(#{1,6})\s/.exec(line)?.[1]?.length ?? 0;
  const kept: string[] = [];
  let fenced = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    if (line.trimStart().startsWith("```")) fenced = !fenced;
    const here = fenced ? 0 : level(line);
    if (here) {
      let next = i + 1;
      while (next < lines.length && !lines[next]!.trim()) next++;
      const after = next < lines.length ? level(lines[next]!) : 1;
      if (after && after <= here) continue;
    }
    kept.push(line);
  }
  return kept.join("\n").trim();
}

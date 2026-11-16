import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo } from "react";

marked.setOptions({ gfm: true, breaks: false });

/** Renders markdown, sanitized: a node's body can be written by an agent. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text, { async: false })), [text]);
  return <div className={`markdown ${className ?? ""}`} dangerouslySetInnerHTML={{ __html: html }} />;
}

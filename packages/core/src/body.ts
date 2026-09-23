// nodes/<id>.md: prose only, plain markdown with no frontmatter (plans.md §3.3).

import { bodyPath, readFileIfExists, writeFileAtomic } from "./files.ts";
import type { NodeId } from "./model.ts";

export interface BodyInit {
  intent?: string;
  acceptance?: readonly string[];
  notes?: string;
}

/** The body for a new node: Intent / Acceptance criteria / Notes, filled from `init`. */
export function renderBody(init: BodyInit = {}): string {
  const section = (heading: string, content: string) =>
    content.trim() === "" ? `## ${heading}\n` : `## ${heading}\n\n${content.trim()}\n`;
  const acceptance = (init.acceptance ?? []).map((a) => `- [ ] ${a.trim()}`).join("\n");
  return [
    section("Intent", init.intent ?? ""),
    section("Acceptance criteria", acceptance),
    section("Notes", init.notes ?? ""),
  ].join("\n");
}

/** A node's body, or "" if it has no body file. */
export async function readBody(apmDir: string, id: NodeId): Promise<string> {
  return (await readFileIfExists(bodyPath(apmDir, id))) ?? "";
}

export async function writeBody(apmDir: string, id: NodeId, text: string): Promise<void> {
  await writeFileAtomic(bodyPath(apmDir, id), text);
}

// Paths inside the plan repo (plans.md §3.1) and atomic file I/O.

import { randomBytes } from "node:crypto";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { isNodeId } from "./ids.ts";
import type { Node, NodeId } from "./model.ts";
import { parseGraph, serializeGraph } from "./ndjson.ts";

export const GRAPH_FILE = "graph.ndjson";
export const NODES_DIR = "nodes";
export const CONFIG_FILE = "apm.yaml";

export function graphPath(apmDir: string): string {
  return join(apmDir, GRAPH_FILE);
}

export function configPath(apmDir: string): string {
  return join(apmDir, CONFIG_FILE);
}

/** Path of a node's body. Throws on anything but a valid ID, so input can't escape `nodes/`. */
export function bodyPath(apmDir: string, id: NodeId): string {
  if (!isNodeId(id)) throw new Error(`not a node id: ${JSON.stringify(id)}`);
  return join(apmDir, NODES_DIR, `${id}.md`);
}

export async function readGraphFile(apmDir: string): Promise<Node[]> {
  return parseGraph(await readFile(graphPath(apmDir), "utf8"), GRAPH_FILE);
}

export async function writeGraphFile(apmDir: string, nodes: Iterable<Node>): Promise<void> {
  await writeFileAtomic(graphPath(apmDir), serializeGraph(nodes));
}

/**
 * Writes via a temp file in the same directory and a rename, so readers in other processes see
 * either the old file or the new one, never a partial write. Creates parent directories.
 */
export async function writeFileAtomic(path: string, data: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.${randomBytes(4).toString("hex")}.tmp`;
  try {
    await writeFile(tmp, data, "utf8");
    await rename(tmp, path);
  } catch (err) {
    await rm(tmp, { force: true });
    throw err;
  }
}

/** Reads a file, or returns `undefined` if it doesn't exist. */
export async function readFileIfExists(path: string): Promise<string | undefined> {
  try {
    return await readFile(path, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw err;
  }
}

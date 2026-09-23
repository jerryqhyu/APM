// apm.yaml (plans.md §3.4). Unknown keys are errors, so typos surface instead of being ignored.

import { LineCounter, parseDocument } from "yaml";
import { z } from "zod";
import { CONFIG_FILE, configPath, readFileIfExists } from "./files.ts";

export interface RepoConfig {
  /** Relative to the plan repo. */
  path: string;
  remote: string | null;
  default: boolean;
}

export interface AgentConfig {
  model?: string;
  budget_usd?: number;
  allowed_tools?: string[];
}

export interface ApmConfig {
  name: string;
  code: { repos: Record<string, RepoConfig> };
  runner: {
    max_concurrent: number;
    default_budget_usd: number;
    agents: Record<string, AgentConfig>;
  };
}

export class ConfigError extends Error {
  override name = "ConfigError";
  readonly issues: readonly string[];
  readonly source: string;

  constructor(issues: readonly string[], source = CONFIG_FILE) {
    super(issues.map((i) => `${source}: ${i}`).join("\n"));
    this.issues = issues;
    this.source = source;
  }
}

const key = z.string().regex(/^[a-z0-9][a-z0-9_-]*$/, "must be lowercase letters, digits, - or _");

const RepoSchema = z.strictObject({
  path: z.string().min(1),
  remote: z.string().min(1).nullish(),
  default: z.boolean().optional(),
});

const AgentSchema = z.strictObject({
  model: z.string().min(1).optional(),
  budget_usd: z.number().positive().optional(),
  allowed_tools: z.array(z.string().min(1)).optional(),
});

const ConfigSchema = z.strictObject({
  name: z.string().min(1),
  code: z.strictObject({
    repos: z
      .record(key, RepoSchema)
      .refine((r) => Object.keys(r).length > 0, "at least one repo is required")
      .refine((r) => {
        const entries = Object.values(r);
        const defaults = entries.filter((e) => e.default === true).length;
        return entries.length === 1 ? entries[0]?.default !== false : defaults === 1;
      }, "exactly one repo must have default: true"),
  }),
  runner: z
    .strictObject({
      max_concurrent: z.int().positive().default(2),
      default_budget_usd: z.number().positive().default(2),
      agents: z.record(key, AgentSchema).default({}),
    })
    .prefault({}),
});

export function parseConfig(text: string, source = CONFIG_FILE): ApmConfig {
  const lineCounter = new LineCounter();
  const doc = parseDocument(text, { lineCounter, prettyErrors: false });
  if (doc.errors.length > 0) {
    throw new ConfigError(
      doc.errors.map((e) => {
        const { line, col } = lineCounter.linePos(e.pos[0]);
        return `${line}:${col}: ${e.message}`;
      }),
      source,
    );
  }
  const result = ConfigSchema.safeParse(dropNulls(doc.toJS() ?? {}));
  if (!result.success) {
    throw new ConfigError(
      result.error.issues.map((i) => {
        // Zod reports a bad record key as "Invalid key in record"; the reason is nested.
        const message =
          i.code === "invalid_key"
            ? `invalid key: ${i.issues.map((n) => n.message).join("; ")}`
            : i.message;
        return i.path.length > 0 ? `${i.path.map(String).join(".")}: ${message}` : message;
      }),
      source,
    );
  }
  const { name, code, runner } = result.data;
  const repos: Record<string, RepoConfig> = {};
  const single = Object.keys(code.repos).length === 1;
  for (const [id, r] of Object.entries(code.repos)) {
    repos[id] = { path: r.path, remote: r.remote ?? null, default: single || r.default === true };
  }
  const agents: Record<string, AgentConfig> = {};
  for (const [id, a] of Object.entries(runner.agents)) {
    const agent: AgentConfig = {};
    if (a.model !== undefined) agent.model = a.model;
    if (a.budget_usd !== undefined) agent.budget_usd = a.budget_usd;
    if (a.allowed_tools !== undefined) agent.allowed_tools = a.allowed_tools;
    agents[id] = agent;
  }
  return {
    name,
    code: { repos },
    runner: {
      max_concurrent: runner.max_concurrent,
      default_budget_usd: runner.default_budget_usd,
      agents,
    },
  };
}

export async function readConfig(apmDir: string): Promise<ApmConfig> {
  const text = await readFileIfExists(configPath(apmDir));
  if (text === undefined) throw new ConfigError(["file not found"]);
  return parseConfig(text);
}

/**
 * YAML reads an empty key (`runner:` with its body commented out) as null. Treat it as not set,
 * so defaults apply, by dropping null-valued keys from every mapping.
 */
function dropNulls(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(dropNulls);
  if (value === null || typeof value !== "object") return value;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    if (v !== null) out[k] = dropNulls(v);
  }
  return out;
}

/** The key of the repo marked default. */
export function defaultRepo(config: ApmConfig): string {
  const entry = Object.entries(config.code.repos).find(([, r]) => r.default);
  if (!entry) throw new Error("apm.yaml has no default repo");
  return entry[0];
}

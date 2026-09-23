import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { ConfigError, defaultRepo, parseConfig, readConfig } from "./config.ts";

function issuesFor(text: string): string[] {
  try {
    parseConfig(text);
  } catch (err) {
    expect(err).toBeInstanceOf(ConfigError);
    return [...(err as ConfigError).issues];
  }
  throw new Error("expected a ConfigError");
}

describe("parseConfig", () => {
  it("parses the example from plans.md §3.4", () => {
    const config = parseConfig(`
name: my-app
code:
  repos:
    app: { path: "..", remote: "git@github.com:jerry/app.git", default: true }
runner:
  max_concurrent: 2
  default_budget_usd: 2.00
  agents:
    apm-decomposer: { model: opus }
    apm-critic:     { model: sonnet }
    apm-implementer: { model: opus, budget_usd: 5.00, allowed_tools: ["Bash(git *)", "Bash(npm test*)"] }
`);
    expect(config).toEqual({
      name: "my-app",
      code: {
        repos: { app: { path: "..", remote: "git@github.com:jerry/app.git", default: true } },
      },
      runner: {
        max_concurrent: 2,
        default_budget_usd: 2,
        agents: {
          "apm-decomposer": { model: "opus" },
          "apm-critic": { model: "sonnet" },
          "apm-implementer": {
            model: "opus",
            budget_usd: 5,
            allowed_tools: ["Bash(git *)", "Bash(npm test*)"],
          },
        },
      },
    });
  });

  it("fills defaults, and a single repo is the default", () => {
    const config = parseConfig("name: x\ncode:\n  repos:\n    app: { path: '..' }\n");
    expect(config).toEqual({
      name: "x",
      code: { repos: { app: { path: "..", remote: null, default: true } } },
      runner: { max_concurrent: 2, default_budget_usd: 2, agents: {} },
    });
    expect(defaultRepo(config)).toBe("app");
  });

  it("fills defaults inside a partial runner section", () => {
    const config = parseConfig(
      "name: x\ncode: { repos: { app: { path: '..' } } }\nrunner: { max_concurrent: 4 }\n",
    );
    expect(config.runner).toEqual({ max_concurrent: 4, default_budget_usd: 2, agents: {} });
  });

  it.each([
    ["an empty runner", "runner:\n"],
    ["a runner with everything commented out", "runner:\n  # max_concurrent: 4\n"],
    ["empty agents", "runner:\n  agents:\n"],
    ["an empty setting", "runner:\n  max_concurrent:\n  default_budget_usd: 2\n"],
  ])("treats %s as not set, so defaults apply", (_name, runner) => {
    const config = parseConfig(`name: x\ncode: { repos: { app: { path: '..' } } }\n${runner}`);
    expect(config.runner).toEqual({ max_concurrent: 2, default_budget_usd: 2, agents: {} });
  });

  it("treats an empty agent entry and an empty remote as not set", () => {
    const config = parseConfig(
      "name: x\ncode:\n  repos:\n    app: { path: '..', remote: }\nrunner:\n  agents:\n    apm-critic:\n    apm-decomposer: { model: opus, budget_usd: }\n",
    );
    expect(config.code.repos.app?.remote).toBeNull();
    expect(config.runner.agents).toEqual({ "apm-decomposer": { model: "opus" } });
  });

  it("still requires required keys that are present but empty", () => {
    expect(issuesFor("name:\ncode: { repos: { app: { path: '..' } } }\n")[0]).toMatch(/^name: /);
    expect(issuesFor("name: x\ncode:\n")[0]).toMatch(/^code: /);
  });

  it("picks the marked default among several repos", () => {
    const config = parseConfig(
      "name: x\ncode:\n  repos:\n    api: { path: '../api' }\n    web: { path: '../web', default: true }\n",
    );
    expect(defaultRepo(config)).toBe("web");
    expect(config.code.repos.api?.default).toBe(false);
  });

  it.each([
    [
      "no default among several repos",
      "name: x\ncode: { repos: { a: { path: a }, b: { path: b } } }",
      /code\.repos: exactly one repo/,
    ],
    [
      "two defaults",
      "name: x\ncode: { repos: { a: { path: a, default: true }, b: { path: b, default: true } } }",
      /exactly one repo/,
    ],
    [
      "a single repo marked not default",
      "name: x\ncode: { repos: { a: { path: a, default: false } } }",
      /exactly one repo/,
    ],
    ["no repos", "name: x\ncode: { repos: {} }", /at least one repo/],
    ["an unknown key (typo)", "name: x\ncode: { repos: { a: { path: a } } }\nrunnr: {}", /runnr/],
    [
      "a negative budget",
      "name: x\ncode: { repos: { a: { path: a } } }\nrunner: { default_budget_usd: -1 }",
      /runner\.default_budget_usd: /,
    ],
    [
      "a fractional max_concurrent",
      "name: x\ncode: { repos: { a: { path: a } } }\nrunner: { max_concurrent: 1.5 }",
      /runner\.max_concurrent: /,
    ],
    [
      "a bad repo key",
      "name: x\ncode: { repos: { 'My Repo': { path: a } } }",
      /code\.repos\.My Repo: invalid key: must be lowercase/,
    ],
    ["a missing name", "code: { repos: { a: { path: a } } }", /^name: /],
    ["an empty file", "", /^name: /],
  ])("rejects %s", (_name, text, pattern) => {
    const issues = issuesFor(text);
    expect(
      issues.some((i) => pattern.test(i)),
      issues.join("\n"),
    ).toBe(true);
  });

  it("reports YAML syntax errors with line and column", () => {
    const [issue] = issuesFor("name: x\ncode: [\n");
    expect(issue).toMatch(/^\d+:\d+: /);
  });
});

describe("readConfig", () => {
  it("reads apm.yaml from the plan repo, or reports it missing", async () => {
    const dir = await mkdtemp(join(tmpdir(), "apm-config-"));
    try {
      await expect(readConfig(dir)).rejects.toThrow("apm.yaml: file not found");
      await writeFile(join(dir, "apm.yaml"), "name: x\ncode: { repos: { app: { path: '..' } } }\n");
      expect((await readConfig(dir)).name).toBe("x");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});

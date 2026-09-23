import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { run } from "./run.ts";

const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as {
  version: string;
};

function capture(argv: string[]) {
  let stdout = "";
  let stderr = "";
  const code = run(argv, {
    stdout: (t) => {
      stdout += t;
    },
    stderr: (t) => {
      stderr += t;
    },
  });
  return { code, stdout, stderr };
}

describe("apm", () => {
  it.each([["--version"], ["-v"]])("%s prints the package version", (flag) => {
    expect(capture([flag])).toEqual({ code: 0, stdout: `${pkg.version}\n`, stderr: "" });
  });

  it("prints usage with --help or no arguments", () => {
    for (const argv of [["--help"], []]) {
      const { code, stdout } = capture(argv);
      expect(code).toBe(0);
      expect(stdout).toMatch(/^Usage: apm/);
    }
  });

  it("rejects unknown options with exit code 2", () => {
    const { code, stdout, stderr } = capture(["--nope"]);
    expect(code).toBe(2);
    expect(stdout).toBe("");
    expect(stderr).toMatch(/--nope/);
  });

  it("runs as a binary straight from source", () => {
    const cli = fileURLToPath(new URL("./cli.ts", import.meta.url));
    const out = execFileSync(process.execPath, [cli, "--version"], { encoding: "utf8" });
    expect(out).toBe(`${pkg.version}\n`);
  });
});

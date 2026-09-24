// Throwaway git repos for tests. Not part of the published build.

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { add, commit, git } from "../git.ts";

export interface TempRepo {
  dir: string;
  /** Writes a file (relative to the repo), stages it and commits; returns the SHA. */
  commitFile(path: string, content: string, subject?: string): Promise<string>;
  cleanup(): Promise<void>;
}

/**
 * An empty repo on branch `main`, with a local identity and signing off, so tests don't depend
 * on the machine's git config.
 */
export async function tempRepo(): Promise<TempRepo> {
  const dir = await mkdtemp(join(tmpdir(), "apm-git-"));
  await git(dir, ["init", "--quiet", "-b", "main"]);
  await git(dir, ["config", "user.name", "APM Test"]);
  await git(dir, ["config", "user.email", "test@example.com"]);
  await git(dir, ["config", "commit.gpgsign", "false"]);
  return {
    dir,
    async commitFile(path, content, subject = `write ${path}`) {
      await writeFile(join(dir, path), content);
      await add(dir, [path]);
      return commit(dir, { subject });
    },
    cleanup: () => rm(dir, { recursive: true, force: true }),
  };
}

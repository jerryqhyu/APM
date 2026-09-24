// Child process for the multi-process lock test: N rounds of lock → read-modify-write → unlock.
// Usage: node lock-worker.ts <apmDir> <rounds>

import { appendFile, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { withWriteLock } from "../lock.ts";

const [apmDir = "", rounds = "1"] = process.argv.slice(2);
const counter = join(apmDir, "counter");
const trace = join(apmDir, "trace");

for (let i = 0; i < Number(rounds); i++) {
  await withWriteLock(
    apmDir,
    async () => {
      // "+" and "-" around each hold: if two holders ever overlap, the trace shows "++".
      await appendFile(trace, "+");
      const n = Number(await readFile(counter, "utf8").catch(() => "0"));
      await new Promise((r) => setTimeout(r, 2));
      await writeFile(counter, String(n + 1));
      await appendFile(trace, "-");
    },
    { timeoutMs: 30_000, retryMs: 5 },
  );
}

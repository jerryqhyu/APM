import { parseArgs } from "node:util";
import { VERSION } from "./version.ts";

export interface Io {
  stdout: (text: string) => void;
  stderr: (text: string) => void;
}

const USAGE = `Usage: apm [options]

Options:
  -v, --version  print the version
  -h, --help     print this help
`;

/** Runs the CLI with the given arguments and returns the process exit code. */
export function run(argv: readonly string[], io: Io): number {
  let values: { version?: boolean; help?: boolean };
  try {
    ({ values } = parseArgs({
      args: [...argv],
      options: {
        version: { type: "boolean", short: "v" },
        help: { type: "boolean", short: "h" },
      },
      strict: true,
    }));
  } catch (err) {
    io.stderr(`apm: ${(err as Error).message}\n\n${USAGE}`);
    return 2;
  }

  if (values.version) {
    io.stdout(`${VERSION}\n`);
    return 0;
  }
  io.stdout(USAGE);
  return 0;
}

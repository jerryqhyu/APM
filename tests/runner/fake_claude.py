"""A stand-in for the `claude` CLI: prints canned stream-json for the agent it was asked to be.

What it does is steered by FAKE_CLAUDE (a comma-separated set of words):

- `fail`: exit 1 with an error on stderr.
- `error-result`: finish with an error result.
- `hang`: emit the first events, then sleep until killed.
- `no-answer`: a schema was requested, but return no structured output.
- `bad-proposal`: the decomposer returns something that isn't a decomposition.
- `revise`: the critic asks for a revision.
- `critic-fails`: the critic exits 1.

Every call's arguments are appended to FAKE_CLAUDE_LOG as one JSON line.
"""

import json
import os
import sys
import time
from pathlib import Path

DECOMPOSITION = {
    "rationale": "Parse first, then write.",
    "children": [
        {
            "key": "a",
            "title": "Parse",
            "kind": "work",
            "intent": "Read it.",
            "acceptance": ["rejects bad lines"],
            "depends_on_keys": [],
            "depends_on_ids": [],
        },
        {
            "key": "b",
            "title": "Write",
            "kind": "work",
            "intent": "Write it.",
            "acceptance": [],
            "depends_on_keys": ["a"],
            "depends_on_ids": [],
        },
    ],
    "warnings": [],
}


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def main() -> int:
    argv = sys.argv[1:]
    modes = set(filter(None, os.environ.get("FAKE_CLAUDE", "").split(",")))

    def option(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv else None

    agent = (option("--agent") or "").removeprefix("apm:")
    session = option("--resume") or option("--session-id") or "no-session"
    resumed = "--resume" in argv
    if log := os.environ.get("FAKE_CLAUDE_LOG"):
        with Path(log).open("a") as f:
            f.write(json.dumps({"argv": argv, "cwd": str(Path.cwd()), "agent": agent}) + "\n")

    if "fail" in modes or ("critic-fails" in modes and agent == "apm-critic"):
        print("boom: something broke", file=sys.stderr)
        return 1

    emit({"type": "rate_limit_event"})  # noise the runner should drop
    emit({"type": "system", "subtype": "init", "cwd": str(Path.cwd()), "session_id": session})
    print("not json at all", flush=True)
    text = f"I am {agent}" + (" (resumed)" if resumed else "")
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    if "hang" in modes:
        time.sleep(600)

    answer = None
    if "--json-schema" in argv and "no-answer" not in modes:
        if agent == "apm-critic":
            verdict = "revise" if "revise" in modes else "accept"
            issue = {"severity": "major", "child_key": "a", "problem": "Too big.", "suggestion": ""}
            answer = {
                "verdict": verdict,
                "summary": "Looks fine." if verdict == "accept" else "Needs work.",
                "issues": [issue] if verdict == "revise" else [],
            }
        elif "bad-proposal" in modes:
            answer = {"rationale": "nothing to do", "children": [], "warnings": []}
        else:
            answer = json.loads(json.dumps(DECOMPOSITION))
            if resumed:
                answer["rationale"] = "Revised."
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": session,
        "total_cost_usd": 0.25,
    }
    if "error-result" in modes:
        result |= {"subtype": "error_max_budget_usd", "is_error": True, "result": "over budget"}
    elif answer is not None:
        result["structured_output"] = answer
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

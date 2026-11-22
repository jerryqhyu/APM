"""A stand-in for the `claude` CLI: reads the user's message from stdin as stream-json, and prints
canned stream-json for the agent it was asked to be.

What it does is steered by FAKE_CLAUDE (a comma-separated set of words):

- `fail`: exit 1 with an error on stderr.
- `error-result`: finish with an error result.
- `hang`: emit the first events, then sleep until killed.
- `no-answer`: a schema was requested, but return no structured output.
- `bad-proposal`: the decomposer returns something that isn't a decomposition.
- `revise`: the critic asks for a revision.
- `critic-fails`: the critic exits 1.
- `ask-permission`: ask the host whether it may run a command, and report the answer.
- `ask-question`: ask the host a question, and report the answer.
- `ask-multi`: ask a question that takes several options, and report the answer.
- `other-control`: send a control request that isn't a permission prompt.
- `review-gaps`: the feature reviewer finds the feature incomplete, with one follow-up task.
- `review-stacked-gaps`: the same, with a second follow-up that depends on the first.
- `too-big`, `unclear`, `bad-verdict`: the task reviewer's verdict (else `pass`); `bad-verdict` is
  `too_big` with no split.
- The feature reporter returns a report that names the detail level its prompt asked for (and
  says "rewritten" when resumed). `report-fails`: it exits 1; `report-hangs`: it hangs;
  `report-bad`: its markdown is empty.

Every call's arguments, working directory and prompt are appended to FAKE_CLAUDE_LOG as one JSON
line.
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

FEATURE_REVIEW = {
    "verdict": "complete",
    "summary": "Everything the description asks for is there.",
    "criteria": [{"criterion": "It works", "status": "met", "evidence": "app.py:1"}],
    "issues": [],
    "follow_ups": [],
}

GAPS = {
    "verdict": "incomplete",
    "summary": "Errors aren't handled.",
    "criteria": [
        {"criterion": "It works", "status": "met", "evidence": "app.py:1"},
        {"criterion": "It handles errors", "status": "missing", "evidence": "nothing catches it"},
    ],
    "issues": [
        {
            "severity": "major",
            "problem": "No error handling",
            "where": "app.py",
            "suggestion": "Catch it",
        }
    ],
    "follow_ups": [
        {
            "key": "e",
            "title": "Handle errors",
            "kind": "work",
            "intent": "Fail cleanly.",
            "acceptance": ["A bad input gives a clear message"],
            "depends_on_keys": [],
            "depends_on_ids": [],
        }
    ],
}

QUESTION = {
    "question": "Tabs or spaces?",
    "header": "Indent",
    "multiSelect": False,
    "options": [{"label": "Tabs", "description": ""}, {"label": "Spaces", "description": ""}],
}


MULTI = {
    "question": "Which checks?",
    "header": "Checks",
    "multiSelect": True,
    "options": [
        {"label": "Lint", "description": ""},
        {"label": "Types", "description": ""},
        {"label": "Tests", "description": ""},
    ],
}


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def ask_host(request_id: str, tool: str, tool_input: dict, description: str = "") -> dict:
    """Sends a permission prompt and waits for the host's decision."""
    request = {
        "subtype": "can_use_tool",
        "tool_name": tool,
        "input": tool_input,
        "description": description,
    }
    emit({"type": "control_request", "request_id": request_id, "request": request})
    reply = json.loads(sys.stdin.readline())
    assert reply["type"] == "control_response", reply
    assert reply["response"]["request_id"] == request_id, reply
    return reply["response"]["response"]


def main() -> int:
    argv = sys.argv[1:]
    modes = set(filter(None, os.environ.get("FAKE_CLAUDE", "").split(",")))

    def option(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv else None

    agent = (option("--agent") or "").removeprefix("apm:")
    session = option("--resume") or option("--session-id") or "no-session"
    resumed = "--resume" in argv
    first = sys.stdin.readline()
    prompt = json.loads(first)["message"]["content"] if first.strip() else ""
    if log := os.environ.get("FAKE_CLAUDE_LOG"):
        call = {"argv": argv, "cwd": str(Path.cwd()), "agent": agent, "prompt": prompt}
        with Path(log).open("a") as f:
            f.write(json.dumps(call) + "\n")

    reporter = agent == "apm-feature-reporter"
    if (
        "fail" in modes
        or ("critic-fails" in modes and agent == "apm-critic")
        or ("report-fails" in modes and reporter)
    ):
        print("boom: something broke", file=sys.stderr)
        return 1

    emit({"type": "rate_limit_event"})  # noise the runner should drop
    emit({"type": "system", "subtype": "init", "cwd": str(Path.cwd()), "session_id": session})
    print("not json at all", flush=True)
    text = f"I am {agent}" + (" (resumed)" if resumed else "")
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    if "hang" in modes or ("report-hangs" in modes and reporter):
        time.sleep(600)

    if "other-control" in modes:
        emit({"type": "control_request", "request_id": "c-0", "request": {"subtype": "hello"}})
        assert json.loads(sys.stdin.readline())["response"]["request_id"] == "c-0"
    if "ask-permission" in modes:
        decision = ask_host("p-1", "Bash", {"command": "make test"}, "Run the tests")
        if decision["behavior"] == "allow":
            text = f"allowed: {decision['updatedInput']['command']}"
        else:
            text = f"denied: {decision['message']}"
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    if "ask-multi" in modes:
        decision = ask_host("m-1", "AskUserQuestion", {"questions": [MULTI]})
        text = f"you chose: {decision['updatedInput']['answers'][MULTI['question']]}"
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    if "ask-question" in modes:
        decision = ask_host("q-1", "AskUserQuestion", {"questions": [QUESTION]})
        text = f"you chose: {decision['updatedInput']['answers'][QUESTION['question']]}"
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})

    answer = None
    if "--json-schema" in argv and "no-answer" not in modes:
        if agent == "apm-task-reviewer":
            answer = {
                "verdict": "pass",
                "summary": "About 20 lines.",
                "children": [],
                "missing": [],
            }
            if "too-big" in modes:
                children = DECOMPOSITION["children"]
                answer |= {
                    "verdict": "too_big",
                    "summary": "About 120 lines.",
                    "children": children,
                }
            if "unclear" in modes:
                answer |= {"verdict": "unclear", "summary": "Too vague.", "missing": ["the format"]}
            if "bad-verdict" in modes:
                answer |= {"verdict": "too_big"}
        elif agent == "apm-feature-reviewer":
            answer = json.loads(json.dumps(GAPS if "review-gaps" in modes else FEATURE_REVIEW))
            if "review-stacked-gaps" in modes:
                then = {"key": "f", "title": "Report errors", "depends_on_keys": ["e"]}
                answer = json.loads(json.dumps(GAPS))
                answer["follow_ups"].append(answer["follow_ups"][0] | then)
        elif reporter:
            detail = next(
                (d for d in ("brief", "standard", "detailed") if f"**{d}**" in prompt), ""
            )
            answer = {
                "summary": "Search for the app.",
                "markdown": f"# Search\n\nA {detail} report"
                + (", rewritten" if resumed else "")
                + ". No reason was recorded for the index.\n",
            }
            if "report-bad" in modes:
                answer = {"summary": "", "markdown": "  "}
        elif agent == "apm-critic":
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

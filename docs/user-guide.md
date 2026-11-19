# APM user guide

APM is a project planner you run on your own machine. You keep a plan as a graph of nodes, talk it through with Claude, and have Claude build the pieces. This guide covers everything you can do with it.

- [Install](#install)
- [Your first ten minutes](#your-first-ten-minutes)
- [The ideas](#the-ideas)
- [The screen](#the-screen)
- [Talking to Claude](#talking-to-claude)
- [Changing the graph yourself](#changing-the-graph-yourself)
- [Having Claude break a node down](#having-claude-break-a-node-down)
- [Having Claude build a node](#having-claude-build-a-node)
- [From the terminal](#from-the-terminal)
- [In your own Claude Code session](#in-your-own-claude-code-session)
- [Settings](#settings)
- [Where the plan lives](#where-the-plan-lives)
- [When something goes wrong](#when-something-goes-wrong)

## Install

You need [uv](https://docs.astral.sh/uv/), git, Node with pnpm (to build the web UI once), and the [Claude Code](https://claude.com/claude-code) CLI, signed in.

```bash
git clone https://github.com/jerryqhyu/APM.git
```

```bash
cd APM/ui && pnpm install && pnpm build && cd ..
```

```bash
uv tool install .
```

This puts an `apm` command on your PATH. To update later, run `git pull`, build the UI again, then `uv tool install --reinstall .`.

## Your first ten minutes

1. Go to the repository you want to plan and create its plan:

   ```bash
   apm init
   ```

2. Start the web UI:

   ```bash
   apm serve
   ```

   Your browser opens. The address ends in a secret token; bookmark the whole address, because the page only works when opened through it.

3. In the **Chat** tab on the right, tell Claude what you want to build, for example: *"I want to add search and tags to this app. Plan it with me."* Claude reads your code, adds nodes to the plan, and tells you what it did. The graph in the middle updates as it goes.

4. Double-click a node to go inside it. Click a node to select it; the right column then shows its actions, a conversation about it, and its details.

5. Pick a node with nothing inside it that says **Ready**, and press **Implement**. Claude builds it on its own branch and reports back in the conversation.

That is the whole loop: talk, shape the plan, build a piece, repeat.

## The ideas

**Node.** One piece of the plan. It has a title, a description, a status, and a kind: *work* (something to build) or *decision* (a choice to make).

**Nesting.** A node can contain other nodes. You start with a big idea and break it down until each piece is small enough to build in one go. A node with nothing inside it is a **leaf**; leaves are what actually get built.

**Dependencies.** A node can need another node first, anywhere in the plan. In the graph, an arrow runs from what is needed to what needs it.

**Status.** You set one of four: *To do*, *In progress*, *Done*, *Dropped*. APM works out the rest:

| You see | It means |
|---|---|
| **Ready** | To do, and nothing it needs is unfinished. You can start it. |
| **Blocked** | To do, but something it needs isn't done yet. A node inside a blocked node is blocked too. |
| **2/5** on a node | Two of the five leaves inside it are done. |

**Handle.** The short code on each node, like `1c0b9a44`. Use it to name a node in the terminal or to Claude.

**The rules.** APM refuses a change that would break the plan's structure: nesting can't loop, dependencies can't form a circle, and a node can't depend on something it contains or is contained by. When it refuses, it says which rule and nothing is changed.

## The screen

**Top bar.** The project name, a search box (press `/`), and counters that open a list when clicked:

- **Needs you**: Claude is waiting for an answer from you.
- **Ready**: what can be started now.
- **Proposals**: breakdowns waiting for your decision.
- **Working**: agents running now.
- **Problems**: broken rules or warnings in the plan.

On the far right you can switch between a **Light**, **Dark** or **Auto** theme. Auto follows your system setting.

**Contents** (left). The whole plan as a tree. Click a node to go to it.

**Graph** (middle). One level of the plan: the nodes inside the node you are in. The path at the top shows where you are; click **All** to go back to the top.

- **Click** a node to select it. **Shift-click** (or ⌘/Ctrl-click) to select several.
- **Double-click** a node to go inside it.
- **Drag** from one node to another to say the second needs the first.
- A dashed stub labelled *from* or *to* is a dependency on a node outside this level. Click it to go there.
- Dashed cards are a **proposal** from Claude, not yet part of the plan.

**Right panel.** An **Actions** bar on top, with buttons for everything you can do to what is selected. Below it, two tabs:

- **Chat**: your conversation with Claude, about the selected node or, with nothing selected, the whole plan. The tab shows which.
- **Details**: the selected node's description, links, delivery and history. With nothing selected, a summary of the plan.

## Talking to Claude

The conversation is the main way to work. Type what you want and press Enter (Shift+Enter for a new line).

What Claude can do in a conversation:

- **Answer questions**: where the plan stands, what is blocked and why, whether a node is well specified.
- **Change the plan**: add, rename, describe, link, move, merge and delete nodes. It does what you ask and then says what it did. For a large reorganisation it proposes the shape first and waits for your yes.
- **Read your code**, so its answers and plans are based on what exists.

Every change it makes is recorded in the plan's history with its name on it, so you can see what happened and undo it.

**It goes turn by turn.** Claude replies, you reply, in the same conversation. Past conversations sit in the row of tabs above; **+ New** starts a fresh one.

**When Claude asks you something**, a card appears in the conversation with the question and its options. Pick one, or type your own answer, and press **Answer**.

**When Claude wants to do something that needs your say-so**, a card shows exactly what, for example the command it wants to run. Press **Allow**, or **Deny**; you can type a reason, which Claude sees. Nothing happens until you answer. The top bar shows **Needs you** while a card is waiting.

**Stop** ends a run that is going.

### Permissions

The **Permissions** selector under the message box sets how much Claude may do without asking. It applies to the next message; a conversation keeps its setting until you change it.

| Setting | What happens |
|---|---|
| **Auto** (the default) | Claude decides what is safe to do by itself and asks you about the rest. |
| **Ask** | You approve every change to a file and every command that changes something. |
| **Accept edits** | File edits go ahead; commands ask. |
| **Plan first** | Claude only reads and plans, then shows you the plan and waits for your go-ahead. |
| **Don't ask** | Anything not already allowed is refused, with no prompt. |

Reading the plan, changing the plan through APM, and reading your code never ask, in any setting.

## Changing the graph yourself

Select a node and use the **Actions** panel. Every button changes the plan immediately, and each change is one entry in the history.

| Button | What it does |
|---|---|
| **To do / In progress / Done / Dropped** | Sets the status. Works on several selected nodes at once. |
| **Open** | Goes inside the node. |
| **Add child** | Adds a node inside it. |
| **Add sibling** | Adds a node next to it. |
| **Rename** | Puts the cursor in the title. Press Enter to save. |
| **Make decision / Make work** | Switches the kind. |
| **Move** | Puts the node, with everything inside it, inside another node or at the top level. |
| **Link** | Says what this node needs first. To remove a link, use the ✕ next to it under **Depends on** in Details. |
| **Merge** | Folds several selected nodes into one (see below). |
| **Delete** | Deletes the selection, with everything inside. Links to it are removed. |

With nothing selected, the panel acts on the whole plan:

| Button | What it does |
|---|---|
| **Add node** | Adds a node at the top level. |
| **Check plan** | Looks for broken rules and warnings. |
| **Reconcile** | Marks nodes done whose pull request or commits have merged. |
| **Commit hand edits** | Records changes you made by editing the plan's files directly. |

To edit a description, press **Edit** in Details. It is markdown; ⌘/Ctrl+Enter saves.

### Merging nodes

Select two or more nodes (shift-click), press **Merge**, choose which one to keep, and give the result a title. The node you keep takes on everything the others had:

- what was inside them,
- what they needed, and what needed them,
- their descriptions, added under a "Merged from" heading.

The others are deleted. A merge is refused if it would break a rule, for example if one node depends on something inside the other.

## Having Claude break a node down

Select a node and press **Decompose**. You can add guidance ("three steps at most", "slice by feature") and choose the permissions setting.

1. Claude reads the node and your code and drafts child nodes.
2. A second agent reviews the draft. If it finds a serious problem, the first one revises once.
3. You get a **proposal**. Its children appear as dashed cards, and **Proposals** in the top bar counts it.

Nothing is written yet. Press **Review** to see the reasoning, the reviewer's notes, and each proposed child. You can edit any child, untick ones you don't want, then **Accept** or **Reject**. Accepting adds the children in one step.

Decompose versus conversation: use Decompose when you want a reviewed proposal to look over before anything changes. Use the conversation when you want to shape things back and forth.

## Having Claude build a node

**Implement** is available on a leaf that is Ready or already In progress. Claude then:

1. works in its own copy of your repository (a git worktree under `.claude/worktrees/`), so your own checkout is not touched;
2. builds what the node describes, on a branch named `apm/<handle>-<title>`;
3. commits, with a line in each commit message naming the node;
4. pushes and opens a pull request, if the GitHub CLI (`gh`) is set up;
5. records the branch, commits and pull request on the node, and ticks the acceptance criteria it verified.

You follow it in the node's conversation and can reply to it there. The node stays **In progress** until you mark it done, or until its pull request merges and you press **Reconcile** (APM also checks when `apm serve` starts).

Two things to know:

- **Claude can only run your tests if the command is allowed.** Add it in the settings (see below), or approve it when asked.
- **The worktree stays** after the run. Remove it with `git worktree remove <path>` when you no longer need it.

## From the terminal

Everything in the UI is also a command. `apm --help` lists them all.

```bash
apm node add "Ship v1"                       # add a node
apm node add "Build the importer" -p 1c0b9a44 --intent "Read the CSV export."
apm node ls                                  # the plan as an outline
apm node show 1c0b9a44                       # one node in full
apm node status 1c0b9a44 done                # set a status
apm node set 1c0b9a44 --title "New title"    # rename
apm node mv 1c0b9a44 7d2e0f11                # move inside another node (or --top)
apm node merge 1c0b9a44 7d2e0f11 --title "Both"   # merge into the first
apm node rm 1c0b9a44 -r                      # delete, with what is inside
apm link-dep 1c0b9a44 7d2e0f11               # the first needs the second
apm ready                                    # what can be started now
apm search "csv"                             # search titles and descriptions
apm check                                    # look for problems
```

Agents:

```bash
apm run assistant -m "Where does the plan stand?"          # chat about the whole plan
apm run assistant 1c0b9a44 -m "Is this well specified?"    # chat about one node
apm run decomposer 1c0b9a44 -m "Three steps at most."      # then: apm proposal show <id>
apm run implementer 7d2e0f11 --mode manual                 # build, asking before each change
apm proposal accept <id>                                    # or: apm proposal reject <id>
apm reconcile                                               # mark merged work done
```

When an agent needs an answer, `apm run` asks you in the terminal.

## In your own Claude Code session

```bash
claude --plugin-dir "$(apm plugin-path)"
```

That session gets the plan's tools (`apm_get_graph`, `apm_ready`, `apm_create_nodes`, `apm_merge_nodes`, and the rest), three skills (`apm-node-format`, `apm-decompose`, `apm-deliver`) and four subagents (`apm-assistant`, `apm-decomposer`, `apm-critic`, `apm-implementer`).

## Settings

Settings live in `.apm/apm.yaml`:

```yaml
name: my-app
code:
  repos:
    my-app: { path: "..", default: true }
runner:
  max_concurrent: 2          # agents working at once
  default_budget_usd: 2.00   # spending cap for one run
  permission_mode: auto      # auto, manual, acceptEdits, plan or dontAsk
  agents:
    apm-assistant: { model: sonnet }
    apm-decomposer: { model: opus }
    apm-critic: { model: sonnet }
    apm-implementer:
      model: opus
      budget_usd: 5.00
      allowed_tools: ["Bash(uv run *)", "Bash(npm test*)"]
```

- `permission_mode` is what agents start in unless you pick another setting for a run.
- `allowed_tools` are commands an agent may run without asking. Give the implementer the commands that run your tests.
- `budget_usd` stops a run that would cost more.

## Where the plan lives

The plan is in a folder called `.apm/` inside your repository. It is its own small git repository, kept **on your machine only**: your code repository ignores it, and nothing is uploaded anywhere.

- `.apm/graph.ndjson` holds the structure, one node per line.
- `.apm/nodes/<id>.md` holds each node's description.
- Every change is a commit there, which is where a node's **History** comes from.
- `.apm/.cache/` holds conversations and proposals. Deleting it loses those and nothing about the plan itself.

You can edit the files by hand. Afterwards run `apm commit` (or press **Commit hand edits**), which checks the result before recording it.

If you ever want a backup or a second machine, `apm link --remote <url>` gives the plan a remote, and `apm push`, `apm pull` and `apm clone` use it. This is optional.

To hand a whole project to someone, `apm export plan.apm.zip` saves the plan and its full history as one file. They run `apm import plan.apm.zip` in a folder that has no plan yet, and get the same plan there. Add `--with-runs` to include agent runs and proposals too; their transcripts can hold tool output and secrets, so check the file before you share it.

## When something goes wrong

| What you see | What to do |
|---|---|
| "This page needs its access link" | Open the full address `apm serve` printed, the one ending in `?token=…`. |
| "The web UI isn't built yet" | In the APM source folder run `cd ui && pnpm install && pnpm build`, then reinstall with `uv tool install --reinstall .`. |
| A run fails with "couldn't start claude" | Install the Claude Code CLI and sign in. Check that `claude --version` works in your terminal. |
| A run fails with a budget error | Raise `default_budget_usd`, or the agent's own `budget_usd`, in `.apm/apm.yaml`. |
| "… is already running" | One conversation takes one turn at a time, and a node has one Decompose or Implement at a time. Wait, or press **Stop**. |
| "2 runs are already active" | Raise `runner.max_concurrent`, or wait for one to finish. |
| Implement is greyed out | Hover over it: the node has children, is blocked, or is already done. |
| A change is refused with a code like `I2` | It would break a rule of the plan. The message says which nodes are involved. |
| "uncommitted changes in the plan repo" | You edited the plan's files by hand. Run `apm commit` first. |
| A run was "interrupted" | `apm serve` was stopped while it ran. Send another message to continue the conversation. |

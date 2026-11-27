# APM user guide

APM is a project planner you run on your own machine. You keep a plan as a graph of nodes, talk it through with Claude, and have Claude build the pieces. This guide covers everything you can do with it.

- [Install](#install)
- [Your first ten minutes](#your-first-ten-minutes)
- [The ideas](#the-ideas)
- [The screen](#the-screen)
- [Talking to Claude](#talking-to-claude)
- [Changing the graph yourself](#changing-the-graph-yourself)
- [Having Claude break a node down](#having-claude-break-a-node-down)
- [Task review: keeping tasks small](#task-review-keeping-tasks-small)
- [Having Claude build a node](#having-claude-build-a-node)
- [Eager mode: a whole feature in one run](#eager-mode-a-whole-feature-in-one-run)
- [Finishing a feature: the review step](#finishing-a-feature-the-review-step)
- [Feature reports](#feature-reports)
- [From the terminal](#from-the-terminal)
- [In your own Claude Code session](#in-your-own-claude-code-session)
- [Settings](#settings)
- [Where the plan lives](#where-the-plan-lives)
- [Sharing or moving a project](#sharing-or-moving-a-project)
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

4. Double-click a node to go inside it. Click a node to select it; the right column then shows a conversation about it and its details, and its **Actions** menu.

5. Pick a node with nothing inside it that says **Ready**, and press **Implement**. Claude builds it on its own branch and reports back in the conversation.

That is the whole loop: talk, shape the plan, build a piece, repeat.

## The ideas

**Node.** One piece of the plan. It has a title, a description, a status, and a kind: *work* (something to build) or *decision* (a choice to make).

**Nesting.** A node can contain other nodes. You start with a big idea and break it down until each piece is small enough to build in one go. A node with nothing inside it is a **leaf**; leaves are what actually get built. A node with children is a **feature**, and its last child is always its **review step** (marked ✓, with a dashed border), which checks the finished feature against its description. APM adds it for you; see [Finishing a feature](#finishing-a-feature-the-review-step).

**Dependencies.** A node can need another node first, anywhere in the plan. In the graph, an arrow runs from what is needed to what needs it.

**Status.** You set one of four: *To do*, *In progress*, *Done*, *Dropped*. APM works out the rest:

| You see | It means |
|---|---|
| **Ready** | To do, and nothing it needs is unfinished. You can start it. |
| **Ready · stacked on #20** | To do, and what it needs is built and pushed but not merged yet. You can start it now: it builds on that work's branch. It becomes plain **Ready** once that merges. |
| **Blocked** | To do, but something it needs isn't done yet, and isn't pushed either. A node inside a blocked node is blocked too. |
| **Pending PR review** | In progress, and its work is delivered: its branch is pushed, usually with a pull request. It waits for you to review and merge. Once the pull request merges, APM marks it **Done**. |
| **2/5** on a node | Two of the five leaves inside it are done. The review step counts, so a feature reaches 5/5 only once it has been reviewed and has landed. |

**Handle.** The short code on each node, like `1c0b9a44`. Use it to name a node in the terminal or to Claude.

**The rules.** APM refuses a change that would break the plan's structure: nesting can't loop, dependencies can't form a circle, and a node can't depend on something it contains or is contained by. When it refuses, it says which rule and nothing is changed.

## The screen

Three rounded panels: the outline on the left, the plan in the middle, and the conversation on the right. Drag the gap between two panels to make one wider, or focus it and press ← →. Double-click a gap to go back to the default width. Press the sidebar button at the top left to hide the outline, and again to bring it back. APM remembers the widths, and whether the outline is shown, in this browser.

**Top bar.** The sidebar button, the project name, a search box (press `/`), and buttons that open a list when clicked:

- **Conversations**: every conversation in the plan, newest first, whatever it is about. Pick one to open it in the Chat tab.
- **Needs you**: Claude is waiting for an answer from you.
- **Ready**: what can be started now.
- **Proposals**: breakdowns waiting for your decision.
- **Working**: agents running now.
- **Problems**: broken rules or warnings in the plan.

On the far right you can switch between a **Light**, **Dark** or **Auto** theme. Auto follows your system setting.

**Contents** (left). The whole plan as a tree. Click a node to go to it.

**Graph** (middle). One level of the plan: the nodes inside the node you are in. The path at the top shows where you are; click **All** to go back to the top. **‹** and **›** next to it go back and forward through the levels you visited, like a browser; your browser's own Back does the same. Picking a node on the same level isn't a step, so Back always takes you to the level you came from.

- **Click** a node to select it. **Shift-click** (or ⌘/Ctrl-click) to select several.
- **Double-click** a node to go inside it.
- **Drag** from one node to another to say the second needs the first.
- A dashed stub labelled *from* or *to* is a dependency on a node outside this level. Click it to go there.
- Dashed cards are a **proposal** from Claude, not yet part of the plan.

**List** (middle, next to **Graph** at the top right). The same level as a table: at the top, your features, inside a feature, its tasks. Each row shows the state, how far it is (or **Not broken down** for a feature with nothing inside it yet: your backlog) and its tags. Type in the search box to filter by title, handle or tag (`#backlog` or `tag:backlog` keeps only nodes with that tag), click a tag to filter by it, and at the top level tick **Not broken down** to see only the features still waiting for a breakdown. Click a row to select it; double-click to go inside. APM remembers which of the two you last used.

**Tags.** Labels on a node, such as `backlog`, `area/ui` or `q3`: lowercase letters, digits and `. _ / -`. Add and remove them under the title in **Details** (type and press Enter). Tags are labels, not content: changing them doesn't redo a task's review, and a node under review can still be tagged. Merging nodes keeps every tag.

**Right panel.** The right third of the window, top to bottom. It is about the node you selected or, with nothing selected, the feature you are inside; at the top level with nothing selected, the whole plan. It has two tabs, and next to them the **Actions** menu: buttons for everything you can do to what is selected, and, under **Here**, **Add child** and **Decompose with Claude** for the node you are inside. It stays open while you use it; click anywhere else or press Escape to close it. The tabs:

- **Details** (shown first): the readable account of the node. Under the title, a card says what matters most now and offers the next step:
  - **Blocked**: the nodes it waits on.
  - **Ready to build**: **Implement**, and **Review task** if its review is missing or out of date.
  - **In progress**: a link to the conversation building it, even when that is a chat about the whole plan.
  - **Pending PR review**: its pull request, with where it stands on GitHub (open or draft, approved or changes requested, checks passing, failing or running), and the conversation that built it.
  - **Done**: its pull request.
  - **A feature**: how far it is, **Implement all ready tasks** (each in its own conversation; you confirm first), and its tasks grouped by what they need from you.

  Then the description, the **Task review** (its verdict; or, once the task changed since, what changed and a **Review task** button), settings, links, and a **Timeline**: who did what and when, each step linked to its conversation or pull request. With nothing selected at the top level, Details is an inbox: what needs you (questions from Claude, proposals, pull requests to review), then what is ready to build (with **Implement**), what is being built, and what is blocked and on what.
- **Chat**: your conversation with Claude about the same node, feature or plan. The tab shows which.

## Talking to Claude

The conversation is the main way to work. Type what you want and press Enter (Shift+Enter for a new line).

What Claude can do in a conversation:

- **Answer questions**: where the plan stands, what is blocked and why, whether a node is well specified.
- **Change the plan**: add, rename, describe, link, move, merge and delete nodes. It does what you ask and then says what it did. For a large reorganisation it proposes the shape first and waits for your yes.
- **Read your code**, so its answers and plans are based on what exists.

Every change it makes is recorded in the plan's history with its name on it, so you can see what happened and undo it.

**It goes turn by turn.** Claude replies, you reply, in the same conversation. Past conversations sit in the row of tabs above; **+ New** starts a fresh one. A conversation elsewhere that changed this node shows there too, with ↗ and a dashed border: a chat about the whole plan that started building it, say. Select a node being built that way and Chat opens on that work while it runs. The address in your browser names the conversation in view (`…&chat=…`), so you can bookmark it or send it to yourself. Under each conversation, **Copy the command to open it in a terminal** gives you `cd <where it ran> && claude --resume <session>`: the same Claude Code session, in your terminal. Stop it here first if it is still running. The row also holds the runs on the node, such as Decompose, Implement and its reviews, each in its own tab. Selecting a node shows your latest chat about it, or a new one, never a run that APM started on its own. In an agent's conversation, the line above the message box says which agent your reply goes to: a reply there continues its work (a task review, for example, runs again with your message). To talk about the node instead, press **+ New**.

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

Select a node and open **Actions**, at the top right. Every button changes the plan immediately, and each change is one entry in the history.

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
| **Reconcile** | Marks nodes done whose work is on your default branch. A pull request merged into another step's branch doesn't count yet: Reconcile says where it went. It also lists open pull requests that still target a branch that has landed, to retarget. |
| **Commit hand edits** | Records changes you made by editing the plan's files directly. |
| **Export archive…** | Downloads the plan and its history as one `.apm.zip` file (see [Sharing or moving a project](#sharing-or-moving-a-project)). |
| **Settings…** | The project's settings: eager mode, the task size limit, feature reports and what `apm serve` does on its own (see [Settings](#settings)). |

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

Nothing is written yet. Press **Review** to see the reasoning, the reviewer's notes, and each proposed child. You can edit any child, untick ones you don't want, then **Accept** or **Reject**. Accepting adds the children in one step, together with the node's review step if it doesn't have one yet.

Each proposed child comes with a full brief, because whoever builds it, usually Claude starting cold, reads little else:

- **Intent**: why it exists.
- **Context**: what to know first, such as the code it builds on (with file paths) and the decisions already made.
- **Approach**: how to build it, such as the files and functions to change, the edge cases and the tests.
- **Acceptance criteria**.
- **Out of scope**: what belongs to another child instead.

The same goes for the parts of a split a task review proposes, and for the follow-ups a feature review proposes. In **Details**, a description reads as a document. Headings with nothing under them, such as an empty **Notes**, are hidden.

Decompose versus conversation: use Decompose when you want a reviewed proposal to look over before anything changes. Use the conversation when you want to shape things back and forth.

## Task review: keeping tasks small

A task should be small enough to read and understand in about five minutes: at most the **task size limit** in lines of real source code, **50** unless you set another (see [Features](#features)). Blank lines, comments, imports, tests, docs and agent or skill markdown don't count. They can come on top, as long as they stay in proportion.

The limit can differ per feature: the reviewer is told the one that applies to its task, and so are the decomposer, which sizes its tasks to it, and the implementer, which aims for it.

APM checks this before a task is built. A **task review** (the `apm-task-reviewer` agent, with the `apm-task-sizing` skill) reads the task and the code it will touch, estimates the real lines it needs, and gives one verdict:

- **Pass**: small enough. Implement is allowed.
- **Too big**: it comes with a proposed split, waiting under **Proposals** and shown dashed next to the task. Accepting replaces the task with those tasks, next to each other in the same feature, so a split never adds a level. The tasks that need no other part take over what the task needed, and whatever needed the task waits for the parts at the end. Each new task is reviewed in turn. Only a top-level node, which no feature holds, becomes a feature with the parts inside it instead.
- **Unclear**: too vague to estimate. It says what is missing; add it to the description.
- **Error**: the review failed or was cut off (for example, `apm serve` was stopped). The task is unlocked and shows the error. APM tries once more for the same content, then waits until you edit the task or start a review yourself.

**When it runs.** While `apm serve` is up, a review is queued whenever a task is created or its title, kind or description changes, from anywhere: the UI, the terminal, MCP or an agent. A burst of edits becomes one review, of the final content. Changing only the status or recording a delivery doesn't count. When `apm serve` starts, it reviews every To do task that has no review for its current content. To start one yourself, press **Review task** on the task, or run `apm run task-reviewer <handle>`. To review every To do task of a feature, press **Review all tasks** on it (see [A whole feature at once](#a-whole-feature-at-once)).

**A feature that isn't broken down yet is not a task.** A top-level node with nothing inside it (**Not broken down** in the List view) is never reviewed on its own, so you can write and discuss it in its conversation as long as you like. Its next step is usually **Decompose**. To build it as one task instead, press **Review task**: Implement waits for that review to pass, as for any task.

**Changing the limit** doesn't undo the reviews that passed: they were for the old limit. Press **Review all tasks** on a feature, or run `apm review-tasks <feature>`, to check its tasks against the new one.

**The lock.** While its review runs, a task can't be edited, moved, merged, deleted, decomposed or implemented. The UI shows **Under review** and disables those actions; the API, the terminal and MCP refuse with "under review". The lock ends when the review finishes, fails or is cancelled, and a lock left behind by a run that died doesn't count.

**The gate.** Implement on a To do task needs a **Pass** for its current content. Edit a passed task's title, kind or description and it needs a new review. Tasks already In progress or Done aren't gated, and neither are review steps. The gate holds even with `automation.task_review` off; that switch only stops the reviews `apm serve` starts on its own (see [Automation](#automation)).

## Having Claude build a node

**Implement** is available on a leaf that is Ready (stacked or not) or already In progress. Claude then:

1. works in its own copy of your repository (a git worktree under `.claude/worktrees/`), so your own checkout is not touched;
2. builds what the node describes, on a branch named `apm/<handle>-<title>`;
3. commits, with a line in each commit message naming the node;
4. pushes and opens a pull request with the GitHub CLI (`gh`);
5. records the branch, commits and pull request on the node, and ticks the acceptance criteria it verified.

**Feature branches.** A node inside a feature is built into the feature's branch, `feature/<handle>-<title>`, not straight into your default branch. APM creates that branch the first time one of the feature's tasks is implemented, and its pull request targets it. The feature goes on to your default branch later, in one pull request, once its review step has passed.

**Stacking.** If the node builds on another node's work that isn't merged yet, the branch starts from that node's branch and the pull request targets it, so each pull request shows only its own changes. APM works this out when the run starts (it fetches first) and tells Claude. Otherwise the base is your default branch. If it waits on several unmerged branches, it builds on the one that was built on the others; if they're on separate branches, the node shows as **Blocked** until all but one of them merge.

**If Claude can't open the pull request** (no `gh`, or `gh` refuses), it still pushes and records the branch, and APM opens the pull request itself once the run ends. You see a line from APM in the conversation either way. If nobody could open it, that line says why, the node's Notes say so, and Details shows **Pull request: None yet**. Nothing is marked done.

You follow it in the node's conversation and can reply to it there. The node stays **In progress** until its work reaches where it belongs (its feature's branch, or your default branch) and you press **Reconcile** (APM also checks when `apm serve` starts), or until you mark it done. You don't need to mark it done for the next step to start: once its branch is pushed, what depends on it is **Ready · stacked**.

Two things to know:

- **Claude can only run your tests if the command is allowed.** Add it in the settings (see below), or approve it when asked.
- **The worktree stays** after the run. Remove it with `git worktree remove <path>` when you no longer need it.

### A whole feature at once

Select a feature (a node with children). The **Tasks** row of the **Actions** menu works on every task inside it, nested features included, but not review steps:

| Button | What it does |
|---|---|
| **Review all tasks** | Starts a task review on every To do task. A task already under review is skipped. A task that passed is reviewed again, against the limit as it is now. |
| **Implement all** | Asks first, then starts Claude on every task that is ready now (**Ready · stacked** included) and has a passing task review of its current content. Blocked tasks, tasks without a passing review and tasks under review are skipped. |

No more than `runner.max_concurrent` agents run at once: the rest wait their turn and start as others finish, so neither button fails for lack of room. Below the buttons you see what started, what waits for room, and what was skipped and why.

In the terminal, the same is:

```bash
apm review-tasks 3f9e2a10              # review every To do task of the feature
apm implement 3f9e2a10                 # build every ready task that passed its review
apm implement 3f9e2a10 -m "Use the new logger."   # guidance for every task
```

Both start what fits, start the rest as runs finish, and follow every run in the terminal, each line marked with its task's handle. They ask you in the terminal when a run needs an answer, like `apm run`.

## Eager mode: a whole feature in one run

Normally Claude builds a feature task by task: one run and one pull request each, and a task that needs another waits until that one is built. In **eager mode** one run builds every task of the feature together, on one branch, and APM then splits the result into one pull request per task. You still review small pull requests, one per task, but the code was written in one go, by one agent that saw the whole feature.

**When to use it.** When a feature's tasks build on each other closely, so building them one at a time would mean a long wait for each, or a tall stack of pull requests, and when the feature is planned well enough that one run can build all of it. Keep it off for features whose tasks are independent: building those one by one, side by side, is just as fast.

**Turning it on.** `eager` under `features:` (see [Features](#features)), for the project or for one feature: select the feature, open **Details**, and set **Eager mode** under its **Settings**. It is off unless you turn it on.

### How a feature is built and split

Press **Implement all** on the feature (or `apm implement <feature>`). Pressing **Implement** on any one of its To do tasks does the same: an eager feature is built as a whole.

1. **The check first.** Every To do task of the feature must have passed its task review for what it says now (see [Task review](#task-review-keeping-tasks-small)), and everything a task needs from outside the feature must be done, because the build starts from the feature branch. If anything isn't, nothing starts, and each task that holds the build back says why.
2. **The build.** APM makes the **eager branch**, `eager/<handle>-<title>`, from the feature branch. One Claude run (the `apm-eager-builder` agent, in its own worktree) gets every task in the order their dependencies need: its title, description, acceptance criteria and what it depends on. It builds them on that branch, commits each task on its own, runs your repository's tests and linters, and pushes the branch. It opens no pull requests.
3. **The split.** APM cuts the eager branch into one commit per task, each built on the one before. When Claude committed task by task, as it is told to, APM does this itself, exactly. Otherwise a second agent, `apm-splitter`, carves the changes up.
4. **The check after.** Before anything is recorded, APM checks the split: one commit per task, in dependency order, each on the one before and naming its task, none empty, and the last one holding exactly what the eager branch holds, nothing lost or added. If it fails, the conversation and the feature's Notes say why, nothing is recorded as delivered, and the eager branch stays: press **Redo** to split it again.
5. **The pull requests.** APM pushes one branch per task (`apm/<handle>-<title>`, as for any task), records each task's delivery, sets it In progress, and opens one pull request per task. The first targets the feature branch; each later one targets the task before it, so **each shows only its own changes**. A part that looks bigger than its task's size limit is shown as a warning.

Follow it all in the feature's conversation. Afterwards the feature's **Details** show an **Eager build** section: the eager branch, each task's pull request in the order to merge them, and their state.

### Merging in order

Merge the pull requests in order, first to last, into the feature branch. Each time one merges, **Reconcile** marks its task done and points the next pull request at the feature branch. `apm serve` reconciles every few minutes on its own (see [Automation](#automation)); in the terminal, `apm reconcile --retarget`. A merge commit, a squash or a rebase all work. If you merge a pull request into the task before it instead, its task is done once that one reaches the feature branch. When every task is done, the feature's review step is ready, as for any feature.

### When a task's pull request changes: Redo

Review often changes a pull request: you push a fix to its branch, or edit it as you merge it. The tasks after it were built on the old version, so APM rebuilds them. A task's pull request counts as **changed** when:

- its branch has new commits that change its content since the split (review fixes, say), or
- it merged into the feature branch with content other than what the split gave it (edited during review, or squashed with changes).

A **Redo** then:

- starts from the changed task's new branch, or from the feature branch if it merged;
- has Claude rebuild **only the tasks after it**, on a new eager branch (`…-2`, `…-3`), telling it exactly what changed;
- splits those tasks again, checked the same way;
- moves their branches and pull requests on to the rebuilt work.

**Nobody's commits are overwritten.** APM never force-pushes. A rebuilt task's branch moves forward: APM adds one commit on top, whose content is the task as rebuilt, so its pull request again shows only its own changes and anyone who has the branch checked out can simply pull. Tasks that are done or merged are never touched. If a task after the change has commits APM didn't make, or has already merged, APM doesn't rebuild over it: the feature's **Eager build** section says why, and you sort it out by hand.

**When it runs.** After every reconcile, `apm serve` looks for changed pull requests; the tasks after one show **Stale: will be rebuilt** and the redo starts on its own, once per change. With `automation.eager_redo` off, they show **Stale: press Redo** and nothing starts until you press **Redo** on the feature (or run `apm eager redo <feature>`). Redo works either way.

### Tasks added later

Once a feature is split, its tasks come from the split: **Implement** on one of them is refused ("use Redo"). A task added to the feature afterwards, such as a follow-up from its feature review, isn't part of the split, so it is built on its own, like any task, into the feature branch.

### Settings and switches

| Setting | What it does for eager mode |
|---|---|
| `features.eager` | Eager mode for the project, or one feature and what is inside it. |
| `features.task_max_lines` | Each task's size limit: Claude aims for it, and a part over it is shown as a warning. |
| `automation.eager_redo` | Redo on its own after a reconcile finds a changed task pull request. Off: only by hand. |
| `automation.reconcile` | The periodic reconcile that moves the chain along and looks for changes. |

In the terminal:

```bash
apm implement 3f9e2a10        # build the eager feature, split it, open the pull requests
apm eager show 3f9e2a10       # its eager branch, each task's pull request, what is stale
apm eager redo 3f9e2a10       # rebuild the tasks after one that changed (or split again)
```

## Finishing a feature: the review step

"Every task is merged" isn't the same as "the feature works". So every feature ends in a **review step**: its last child, which waits on all the others. When they're all done (merged into the feature branch), it becomes **Ready**, and you press **Review feature**, on the step or on the feature itself.

Claude then:

1. checks out the feature branch, exactly as it is, in a separate folder (your checkout isn't touched);
2. reads the feature's description (its intent, acceptance criteria and decisions) and every task;
3. reads what the feature changed, and checks each acceptance criterion against the code: **Met** (with the file and line, or the test, that shows it), **Partly**, **Missing**, or **Not checked** (it can't be verified by reading);
4. looks for what the criteria don't spell out: decisions that weren't followed, places where two tasks don't fit together, missing tests or docs.

It changes no files. Its verdict goes into the review step's description under **Feature review**, as a table you can read later. Then:

- **Complete**: APM opens the feature branch's pull request into your default branch (or into the enclosing feature's branch), titled after the feature and with the review in its description. Then it writes the feature's report, adds it to that branch and links it from the pull request (see [Feature reports](#feature-reports)). The step stays **In progress** until that pull request merges; **Reconcile** then marks the step and the feature done.
- **Incomplete**: the step goes back to **To do**, and the gaps come back as **follow-up tasks**, waiting under **Proposals**, the same way a breakdown does. Accept them, build them, and press **Review feature** again once they're merged. You can also reply in the review's conversation ("look again at error handling"); a new verdict replaces the old one.

Claude says "complete" only when every criterion is met and nothing major is wrong. APM holds it to that: a "complete" with a missing criterion or a major issue is recorded as incomplete.

**To review another branch**, type it in the **Branch** field when you start the review. **To skip a review**, set the step to **Dropped**. You can't delete or move a review step: APM keeps it waiting on exactly the feature's tasks, adding the ones you add later and dropping the ones you drop.

## Feature reports

A feature report tells people who weren't there what a feature is and why it exists, the decisions you made along the way and your reasons, and how it was built. It is a markdown file in your repository, `docs/features/<title>-<handle>.md`, committed on the feature's branch, so it lands with the feature, and the feature's pull request links it at the top of its description.

**What it is written from.** The reporter (the `apm-feature-reporter` agent) reads the feature branch's code, and a **record** that APM gathers for it, so it doesn't have to dig:

- the feature's description, its decision nodes, and every task: its description, status, pull request and the size its task review gave it;
- what you said and chose: every message you typed in a conversation about the feature or anything in it (chats, and the guidance you gave Decompose, Implement and Review feature), your answers to Claude's questions, and the commands you refused, with your reason. A chat about the whole plan counts for the turns that changed this feature;
- the proposals you accepted or rejected, and what you changed in one before accepting it (a renamed task, a criterion added, a child left out);
- the plan's history of the feature, with who made each change and the note that came with it, like the reason you gave for dropping a task;
- every turn of the feature's reviews: what was missing, and the follow-up tasks that fixed it;
- what the feature branch changes, file by file.

A long record is kept to a sensible size: Claude's replies in long conversations are shortened, then left out, and what you typed is never left out, only shortened.

**It doesn't make things up.** The reporter may report a decision only if the record shows it, and give only the reason the record gives. Where you made a decision without saying why, the report says "No reason was recorded." It cites the pull requests and files behind what it says. So the more you say *why* (in a chat, in a guidance box, in a decision node or a status note), the better your reports get.

**What it says**, by `report_detail`:

| Detail | What is in it |
| --- | --- |
| **Brief** | About a page: what it is and why, the key decisions with one-line reasons, and the pull requests. |
| **Standard** (the default) | Also each decision with its rationale and the alternatives that were rejected, how the feature is put together (modules, data flow), a paragraph per task, how it was tested, and what was left for later. |
| **Detailed** | Also notes file by file, what each feature review found and how it was fixed, risks and follow-ups, and a timeline. |

**When it is written.** On its own, once the feature's review passes: APM opens the feature's pull request first, then writes the report, commits it to the feature branch (a new commit, pushed without force, so it joins the open pull request) and links it from the pull request's description. The pull request never waits for the report. If the report fails or takes more than 30 minutes, the review's conversation says so and how to write it by hand. A feature inside a feature gets its report on its own branch, which goes into the enclosing feature's branch with it.

**By hand**, press **Write report** on a feature (in the **Actions** menu, under **Review all tasks** and **Implement all**), or run `apm report <feature>`. It works whenever the feature branch has work on it, before or after the review, and whether or not reports are on. You can add guidance ("say more about the file format"). Each time, APM rewrites the report and adds a new commit. A reply in the report's conversation rewrites it too.

**Where to see it.** A feature's **Details** show its report: not written yet, being written, or written at a commit, with a link to the file on GitHub, and **Show** displays it. `apm report <feature> --show` prints it. Claude's work on it is in the feature's conversation.

**Turning it off, or changing the detail.** In **Settings…** (or `features:` in `apm.yaml`), **Feature report** turns automatic reports on or off and **Report detail** sets how much they say, for the whole project. A feature can have its own in its **Details → Settings**, for itself and the features inside it. In the terminal:

```bash
apm settings set report=off                          # no automatic reports in this project
apm settings set report_detail=brief                 # shorter reports
apm settings set --node 3f9e2a10 report_detail=detailed   # this feature says more
```

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
apm node add "Search" --tag backlog         # add a node with a tag
apm node tag 1c0b9a44 backlog area/ui        # tag a node (-r removes the tags instead)
apm node ls --tag backlog                    # the nodes with every tag given
apm search "csv"                             # search titles, tags and descriptions
apm search "tag:backlog import"              # only nodes tagged backlog
apm check                                    # look for problems
apm export plan.apm.zip                      # the whole project as one file
apm import plan.apm.zip                      # restore it into a folder with no plan
```

Agents:

```bash
apm run assistant -m "Where does the plan stand?"          # chat about the whole plan
apm run assistant 1c0b9a44 -m "Is this well specified?"    # chat about one node
apm run decomposer 1c0b9a44 -m "Three steps at most."      # then: apm proposal show <id>
apm run implementer 7d2e0f11 --mode manual                 # build, asking before each change
apm run feature-reviewer 3f9e2a10                          # review a finished feature (--branch to pick one)
apm run task-reviewer 7d2e0f11                             # check a task is small enough to build
apm review-tasks 3f9e2a10                                  # review every To do task of a feature
apm implement 3f9e2a10                                     # build every ready, reviewed task of it
apm report 3f9e2a10 -m "Say more about the format."        # write the feature's report (--show prints it)
apm eager show 3f9e2a10                                    # an eager feature's build and PR chain
apm eager redo 3f9e2a10                                    # rebuild after a task PR changed
apm proposal accept <id>                                    # or: apm proposal reject <id>
apm reconcile                                               # mark merged work done
apm reconcile --retarget                                    # ...and retarget stacked PRs
```

When an agent needs an answer, `apm run` asks you in the terminal.

Settings:

```bash
apm settings show                                  # the project's settings, and every feature's own
apm settings show 3f9e2a10                         # what applies to a node, and where each comes from
apm settings set task_max_lines=80 eager=on        # project-wide
apm settings set automation.reconcile=off          # an automation switch
apm settings set --node 3f9e2a10 task_max_lines=30 # one feature's own; NAME=inherit inherits again
apm settings clear 3f9e2a10                        # the feature inherits everything again
```

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
automation:                  # what `apm serve` does on its own (see below)
  implement_follow_ups: false
features:                    # how features are built and reported (see below)
  task_max_lines: 80
  overrides:
    0192f0c1-8a2b-7c3d-9e4f-1c0b9a44d2e7: { task_max_lines: 30 }
```

- `permission_mode` is what agents start in unless you pick another setting for a run.
- `allowed_tools` are commands an agent may run without asking. Give the implementer the commands that run your tests.
- `budget_usd` stops a run that would cost more.

**Changing settings.** You can edit `apm.yaml` by hand, or press **Settings…** in the **Actions** menu with nothing selected, or use `apm settings set` (see [From the terminal](#from-the-terminal)). The panel and the command change only what you changed: your comments, your layout and every other value stay as they were. A value APM wouldn't accept, such as a task size limit of 3, is refused with the reason, and the file is left alone. Each change is one entry in the plan's history (`settings: task size limit 80 lines`); edits you had made to `apm.yaml` by hand go into that entry too, and it says so. `apm check` reports a broken `apm.yaml`.

### Features

`features:` sets how features are built and reported. Each setting here is the project's default:

| Key | Default | What it does |
| --- | --- | --- |
| `eager` | `false` | Eager mode: build a feature in one run on one branch, then split it into one pull request per task (see [Eager mode](#eager-mode-a-whole-feature-in-one-run)). |
| `task_max_lines` | `50` | The task size limit, in real lines of code (see [Task review](#task-review-keeping-tasks-small)). At least 5. |
| `report` | `true` | Write a feature report once a feature's review passes: what it is, the decisions made and why, and how it was built (see [Feature reports](#feature-reports)). Off, you can still write one with **Write report**. |
| `report_detail` | `standard` | How much that report says: `brief`, `standard` or `detailed`. |

**A feature's own settings.** Under `overrides:`, by the feature's id, a feature can set any of these for itself and everything inside it. A feature inside it can override them again: the innermost one wins. In the UI, select a feature and open **Details**: its **Settings** show what applies, where each value comes from (the project, an enclosing feature, or set here), and let you pick a value of its own or **Inherit**. **Inherit all** removes its own settings.

When you type an override by hand, a handle works as the key. Put a handle of digits only in quotes (`'12345678':`), or YAML reads it as a number. An override for a node that no longer exists is ignored, and `apm check` and **Problems** say so.

### Automation

While `apm serve` is up it can take a feature from review to pull request on its own: it reviews tasks as they change, reconciles every few minutes (retargeting stacked pull requests), starts a feature's review once its review step is Ready, accepts the follow-up tasks that review proposes, builds them once their task review passes (one pull request each, into the feature branch), reviews the feature again once they merge, and opens the feature's pull request when it passes. For an eager feature it also rebuilds the tasks after one whose pull request changed. Each step has its own switch under `automation:`, and turning one off stops only that step:

| Key | Default | What it does |
| --- | --- | --- |
| `task_review` | `true` | Review a task when it is created or its title, kind or description changes, and review unreviewed To do tasks when `apm serve` starts. Turning it off stops only these automatic reviews: the Implement gate, the review state and the **Review task** button stay, so you review a task by hand before building it. |
| `reconcile` | `true` | Reconcile, and retarget open pull requests, every `reconcile_interval_s` seconds. Every minute APM also asks GitHub how each pull request waiting for review stands (shown in Details), and reconciles at once when one merges, so its task turns **Done** without waiting. |
| `reconcile_interval_s` | `300` | Seconds between those reconciles. Must be above 0. |
| `feature_review` | `true` | Start a feature's review once its review step becomes Ready. |
| `accept_follow_ups` | `true` | Accept the follow-up tasks a feature review proposes. |
| `implement_follow_ups` | `true` | Build those follow-ups once their task review passes. Only follow-ups from a feature review are built automatically, never other tasks. |
| `feature_pr` | `true` | Open the feature's pull request once its feature review passes. With it off, the passing review is recorded and no pull request is opened. |
| `eager_redo` | `true` | Eager mode: when a task pull request of an eager feature changes (new commits, or merged with changes), rebuild and re-split the tasks after it (see [Redo](#when-a-tasks-pull-request-changes-redo)). Off, the tasks show as stale and you press **Redo**. |

Automation never runs more than `runner.max_concurrent` agents at once (the rest wait their turn) and gives each run its agent's `budget_usd`.

## Where the plan lives

The plan is in a folder called `.apm/` inside your repository. It is its own small git repository, kept **on your machine only**: your code repository ignores it, and nothing is uploaded anywhere.

- `.apm/graph.ndjson` holds the structure, one node per line.
- `.apm/nodes/<id>.md` holds each node's description.
- Every change is a commit there, which is where a node's **History** comes from.
- `.apm/.cache/` holds conversations and proposals. Deleting it loses those and nothing about the plan itself.

You can edit the files by hand. Afterwards run `apm commit` (or press **Commit hand edits**), which checks the result before recording it.

If you ever want a backup or a second machine, `apm link --remote <url>` gives the plan a remote, and `apm push`, `apm pull` and `apm clone` use it. This is optional.

To hand the whole project to someone, or move it without a remote, export it as one file: see the next section.

## Sharing or moving a project

An archive is the whole project in one `.apm.zip` file: the plan and its full history. Use it to hand a plan to someone, keep a backup, or move to another machine.

**Export.** In the terminal:

```bash
apm export plan.apm.zip
```

Or, in the UI, select nothing and press **Export archive…** in the **Actions** menu, then **Download**. The file is named after the project.

**Include runs if you want them.** Add `--with-runs` (in the UI, tick **Include runs**) to bring agent conversations, their transcripts and proposals along too. **Transcripts can hold tool output and secrets**: file contents, tokens, keys. Check the file before you share it.

**What is not in an archive:**

- the code repository's `.apm-link`, and any remote you set with `apm link`; import writes a fresh link;
- the server's access token, so `apm serve` on the other side prints a new address;
- edits to the plan's files you haven't committed. The archive holds the plan as last committed, so run `apm commit` first if you want them;
- runs and proposals, unless you asked for them.

**Import.** Go to the folder that should hold the plan, usually the code repository, and run:

```bash
apm import plan.apm.zip
```

You get the same plan, with the same history, as `.apm/`, linked the way `apm init` links it. Then run `apm check` or `apm serve`.

Imported runs are history only: you can read them, but nothing starts again. A run that was still going when the archive was made comes back as **Stopped** ("Stopped before it finished").

**Import refuses**, and changes nothing, when:

- the folder already has a plan (`.apm/` or `.apm-link`). Import only into a folder that has none;
- the archive was made by a newer APM, with a newer archive format or, for runs, a newer store schema. Upgrade APM, then import;
- the file is damaged: truncated, not an APM archive, or its contents don't match its manifest or history.

## When something goes wrong

| What you see | What to do |
|---|---|
| "This page needs its access link" | Open the full address `apm serve` printed, the one ending in `?token=…`. |
| "The web UI isn't built yet" | In the APM source folder run `cd ui && pnpm install && pnpm build`, then reinstall with `uv tool install --reinstall .`. |
| A run fails with "couldn't start claude" | Install the Claude Code CLI and sign in. Check that `claude --version` works in your terminal. |
| A run fails with a budget error | Raise `default_budget_usd`, or the agent's own `budget_usd`, in `.apm/apm.yaml`. |
| "… is already running" | One conversation takes one turn at a time, and a node has one Decompose or Implement at a time. Wait, or press **Stop**. |
| "2 runs are already active" | Raise `runner.max_concurrent`, or wait for one to finish. **Review all tasks** and **Implement all** never say this: their runs wait their turn. |
| A setting is refused, e.g. `features.task_max_lines: Input should be greater than or equal to 5` | The message names the setting and what it accepts. Nothing was changed. |
| "APM couldn't make this change without touching other settings" | Something in `apm.yaml` can't be written back exactly as it is. Make the change by hand in `.apm/apm.yaml`. |
| Implement is greyed out | Hover over it: the node has children, is blocked, or is already done. On a task of an eager feature that was split, its work comes from the split: use **Redo** on the feature. |
| "No feature report: …" after a review passed | The pull request is open without it. Fix what the line says, then press **Write report** on the feature, or run `apm report <feature>`. |
| "APM couldn't commit the report to `feature/…`" | The report is kept: the feature's **Details** show it. Once you can push to that branch again, press **Write report**. |
| "The split failed: …" | The eager build is kept and nothing was delivered. Fix what it names (a task with no changes of its own can be dropped), then press **Redo** on the feature to split it again, or Implement to build it again. |
| "APM won't overwrite them" or "never touches merged work" | A redo would have had to replace someone's commits or merged work. Bring that branch back in line by hand, or rebuild the tasks after it yourself. |
| "No pull request yet: `gh` is not installed" | Install the GitHub CLI and run `gh auth login`, then send any reply in the node's conversation: after that turn, APM tries again. |
| "couldn't tell which GitHub repository … is" | Set the repo's `remote: https://github.com/<owner>/<repo>` in `.apm/apm.yaml`. |
| A change is refused with a code like `I2` | It would break a rule of the plan. The message says which nodes are involved. |
| Import says "… already exists" | The folder already has a plan. Import into a folder without `.apm/` or `.apm-link`. |
| Import says "upgrade APM to import it" | The archive came from a newer APM. Update APM (see [Install](#install)) and try again. |
| "uncommitted changes in the plan repo" | You edited the plan's files by hand. Run `apm commit` first. |
| A run was "interrupted" | `apm serve` was stopped while it ran. Send another message to continue the conversation. |

# The web interface

A browser window onto a live run. You type a request in plain language; the page
streams the planner's reasoning, the plan and how far execution has got, every
tool call, and the figures and files the run writes. It pauses for you to approve
or revise the plan before anything executes.

It drives the **real** agent — the same LangGraph graph, the same RAG, the same
tools as the command line. Nothing is simulated.

---

## Launch it

The interface runs **on the cluster**, on the node where your resources are
allocated. You reach it from your laptop through an SSH tunnel. One-time setup,
then three steps per session.

### One-time: give it a key

```bash
cd ChromaPilot/chromapilot/interface
cp config.example.yaml config.yaml
$EDITOR config.yaml                 # set llm.api_key
```

`config.yaml` is git-ignored, so the key stays local. Put **both** an Anthropic
and an OpenAI key in it (`llm.api_key` and `llm.alt_api_key`) and a model
dropdown appears on the start screen. To reuse a config you already have, point
`IFACE_CONFIG` at it instead of copying.

### 1. Start it on your compute node

Get onto the node where your resources are allocated — **not** a login node,
because once you approve a plan the real bowtie2/samtools/R tools run right
there. Activate the environment and launch:

```bash
conda activate chromapilot
cd ChromaPilot/chromapilot/interface
./run_interface.sh
```

To name the interpreter instead of activating anything:
`IFACE_PYTHON=/path/to/env/bin/python ./run_interface.sh`.

To keep the server alive after you close the launching terminal:

```bash
setsid nohup ./run_interface.sh > iface.log 2>&1 &
```

The launcher checks that `langgraph` is importable and that the pipeline
binaries are on `PATH`, naming anything missing up front rather than letting a
plan fail hours later.

### 2. Tunnel to it from your laptop

The launcher prints its node name and a ready-to-paste `ssh` command. Which one
you want depends on where your own `ssh` lands you, so it prints all three. Run
**one** of them in a **new terminal on your laptop**, where `<ssh-host>` is
however you normally reach the cluster (`you@login-node`, or an alias from your
`~/.ssh/config`) and `<node>` is the node the launcher printed:

| Your situation | Command |
|---|---|
| Your ssh lands somewhere that can reach `<node>` (usual) | `ssh -t -L 8800:<node>:8800 <ssh-host>` |
| Your ssh already lands you **on** `<node>` | `ssh -t -L 8800:localhost:8800 <ssh-host>` |
| Node-to-node traffic is blocked | `ssh -J <ssh-host> $USER@<node> -L 8800:localhost:8800` |

Not sure? Try the first. If the page does not load, run
`curl -s -m 5 http://<node>:8800/api/health` **inside that ssh session**: a reply
of `{"ready": ...}` means the first form is right and something else is wrong;
no reply at all means use the third.

An `~/.ssh/config` alias may land you on a compute node rather than a login node
(a VS Code job, for instance). That is fine — it only decides which row you want.

### 3. Open it

Leave the tunnel terminal open and browse to **<http://localhost:8800>**.

The first start imports the agent and its retrieval index (1-2 minutes); the page
shows *"importing agent…"* until the prompt box unlocks.

Losing the tunnel or reloading the page is safe. The run keeps going on the
cluster and the page re-attaches, replaying everything it missed.

### Stopping

Close the tunnel terminal (Ctrl-D) to drop the tunnel; the server keeps running.
To stop the server itself, on the compute node:

```bash
pkill -f '[s]erver.py'
```

---

## Using it

### Start a run

The landing screen is one large prompt box. Write the request the same way you
would on the command line — see [quickstart.md](quickstart.md#1-describe-your-analysis)
for what to include. There is an **output dir** field; fill it in, or put the
directory in the prompt text, or leave both and results go to
`chromapilot/interface/runs/<timestamp>_<run_id>/`.

Submit, and the page becomes the agent workspace: the prompt shrinks to a
one-line summary at the top (with **← New analysis**) so the screen is devoted to
watching the agent work.

### What you are looking at

**Pipeline map** *(top)* — the dependency DAG of the 13 pipeline tools, drawn
live from `chromapilot/Knowledge/graph.txt`. It scales to fit the width, so the
whole pipeline is always visible. Tools light up *planned → running → done* as
your plan advances. Edge style encodes the protocol (solid = Hiplex CUT&Tag,
dashed = ChIP-DIP). Hover a tool for a one-line summary; click it for its full
catalog entry.

**Agent graph** *(left)* — the control nodes (planner → validator → executor →
tools → reporter), colour-coded by role, lighting up as the agent moves through
them. This is how the agent is reasoning, not just what it is running. The two
embedded sub-graphs — autocode (generate → execute → check → summarise) and the
report builder — show their inner steps as chips while they run.

**Tool catalog** *(left, collapsible)* — every tool from
`chromapilot/Knowledge/node.txt` with its summary, input parameters and
defaults, outputs, and the sub-directory it writes to. This is the same text the
planner reads, so it is always in sync with the tools the agent actually has.

**Status bar** *(under the prompt)* — always moving while a run is active: a
spinner, the current phase (*"Planning the analysis…"*, *"Running a pipeline
tool…"*, *"Needs your input — …"*), an elapsed-time counter, and a ticker of the
agent's latest console line. The planner call can take up to 90 seconds on a
complex request; this is what makes that wait visibly working rather than frozen.

**Activity** *(center)* — the agent's messages, tool calls and findings. Tool
calls and their results render as key/value cards; click an output path to copy
it. A **raw console** toggle surfaces the agent's stdout. When the graph pauses
for you, the form appears here.

**Plan & progress** *(right)* — the plan you approved, with a progress bar. Each
step is ticked off *pending → running → done*, tracked positionally so it stays
correct even when a tool repeats per sample or per mark. Click a step to see its
inputs and outputs as written in the plan; auto-generated code steps are badged.

**Results & figures** *(right)* — a figure gallery plus every file the run wrote,
grouped by kind (figures, report, tables, tracks & alignments, sequences, code,
logs) with its absolute path on the server; click to copy. Figures render whether
the tool wrote PNG or PDF. Click one to view it full size.

**View report** — assembles a scrollable report in the page: your request, the
agent's summary, the pipeline steps, and every figure inline. *Print / save PDF*
exports it. Offered automatically when a run finishes.

### Answering the agent

The agent stops and waits three times at most. The form appears in the Activity
column; whatever you type is handed straight back to the graph, so the planner
genuinely acts on it.

| Pause | When | What to do |
|---|---|---|
| **Plan review** | Always, before anything executes | Read the plan on the right. **Approve & execute** to run it on this node, or **Request changes** and describe what to fix — the planner revises and asks again. **Stop here** keeps the run plan-only. |
| **Code check** | Only if generated code has failed 5 times | **Try once more**, optionally with a note about what is going wrong. A second failure ends that step. |
| **Figure review** | Only when `report_enabled: 'true'` | **Looks good**, or describe what to adjust before the PDF is compiled. |

Useful plan-review replies: *"use mm10, not hg38"*, *"only process C1 and C2"*,
*"skip the annotation step"*, *"the barcode file is at /data/other/barcode.csv"*.

**Plan-only runs.** Because every run pauses before execution, *Stop here* at the
plan review gives you the agent's plan without launching any compute — useful for
checking how it would approach a dataset, or for a demo.

### Switching models

With two providers configured, the start screen shows a model dropdown and each
run uses the LLM you pick; the interface rebinds the agent's model and its
retrievers to your choice. To swap the model without touching the config:

```bash
IFACE_MODEL=claude-haiku-4-5-20251001 ./run_interface.sh
```

Same graph, same prompts, faster planning.

---

## How long things take

The interface adds **no** LLM calls; all latency is the agent's own chain of
model calls. Planning phase, measured with `claude-sonnet-4-6`:

* **A pipeline task the tools already cover** reaches plan review in **~17 s** —
  four sequential LLM calls (path extraction, RAG routing, plan generation,
  parameter extraction). Retrieval is skipped because the tools suffice.
* **A novel task needing generated code** takes **~60-90 s**: the router decides
  it needs domain knowledge and runs retrieval (multi-query expansion plus
  reranking) before planning, and the plan itself is larger.

Everything after approval is the pipeline itself — hours, if you are aligning
reads — and runs on the launch node.

## Where the work happens

The interface allocates nothing of its own. It uses the resources of the node you
launch it on:

| Part | Cost | Runs on |
|---|---|---|
| Web server (stdlib HTTP + SSE) | negligible | the launch node |
| Agent import at start-up (retrieval index) | a few GB RAM, 1-2 min CPU, once | the launch node |
| LLM calls | remote API — no local CPU/GPU | Anthropic / OpenAI / Google |
| **Pipeline execution** after you approve | **heavy CPU and memory** | the launch node |

Launch it on the node where your resources are allocated, then tunnel in.
Launching on a shared login node would put the retrieval index and the real
pipeline tools on that login node.

## Notes

* **Reconnects.** The server keeps every event of a run and replays it on
  demand, so a dropped tunnel, a sleeping laptop or a page reload re-attaches to
  the run in progress. Finished runs stay replayable until 20 newer ones finish.
* **State is in memory.** Restarting the *server* clears in-flight runs; the page
  then tells you the run is gone. Restarting the tunnel or the browser is fine.
* **Figure serving.** `/api/file` serves figures only, and only from inside a
  registered run output directory — an extension allow-list plus a `realpath`
  containment check. This is a local, single-user tool reached over an SSH
  tunnel; it is not hardened for exposure to a network.
* **Output directory.** Put it in the prompt (end with "…written to the following
  output directory: /path/to/my_run") or use the *output dir* field, which
  overrides the prompt. It must be an absolute path.

## Keeping it in sync with the agent

Nothing in the interface hard-codes the tool list: the catalog, the pipeline map
and step highlighting all derive from `chromapilot/Knowledge/node.txt` and
`graph.txt` at server start-up. Adding or changing a tool needs no interface
change — just restart the server.

Three small tables do mirror the agent graph, and are worth checking if
`chromapilot/agent.py` itself changes:

| Table | File | Mirrors |
|---|---|---|
| `CONTROL_NODES` | `agent_runtime.py` | `builder.add_node(...)` names of the main graph |
| `SUB_NODES` | `agent_runtime.py` | node names inside the autocode and report sub-graphs |
| `PHASE_LABELS` | `static/app.js` | status-bar text per main-graph node |

The three pauses are recognised by content (`HUMAN_IN_THE_LOOP` → code check,
"figure" → figure review, anything else → plan review); see `_interrupt_kind` in
`run_session.py` if a new one is added.

## Architecture

```
browser  ──HTTP/SSE──►  server.py          stdlib http.server, no framework
                          │  POST /api/run     start a run
                          │  GET  /api/stream  Server-Sent Events (replayable)
                          │  POST /api/resume  answer a pause
                          │  POST /api/stop  ·  GET /api/file (figures)
                          ▼
                       run_session.py      one worker thread per run
                          │  drives graph.stream(stream_mode=["updates","values"],
                          │                      subgraphs=True)
                          │  diffs each state snapshot into flat UI events
                          │  pauses at interrupt(); resumes with Command(resume=…)
                          ▼
                       agent_runtime.py    the ONLY file that touches the agent
                          │  imports agent.py (the real compiled `graph`)
                          │  rebinds agent.CONFIG / model + _init_rag_retrievers
                          ▼
                       chromapilot/agent.py    imported in place, unmodified
```

* **`agent_runtime.py`** imports the real agent module once at start-up, brings
  the LLM online exactly as `agent.__main__` does, and installs a thread-aware
  stdout router so each run's printouts can be streamed. This is the single
  integration point.
* **`run_session.py`** is the driver. It streams the graph, turns state changes
  into UI events, and when the graph hits an `interrupt()` it surfaces the prompt
  to the browser and waits for your answer.
* **`knowledge.py`** parses the tool catalog and dependency DAG into JSON.
* **`server.py`** is the HTTP + SSE transport.
* **`static/`** is the single-page UI (`index.html`, `style.css`, `app.js`).

The agent is imported in place and never modified. The interface drives the
graph itself rather than calling the batch runner, feeds your form text back as
the `interrupt()` return value, and injects the run's output directory into the
prompt using the agent's own `build_run_prompt` helper.

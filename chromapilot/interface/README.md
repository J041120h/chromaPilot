# ChromaPilot — web interface

A live browser window onto the real agent: it streams the planner's reasoning,
the plan and its progress, every tool call, and the figures and files a run
writes, and pauses for you to approve or revise the plan before anything runs.

**The full guide — launching it, the SSH tunnel, every panel, how to answer the
agent — is [`docs/interface.md`](../../docs/interface.md).**

## Quick start

```bash
cp config.example.yaml config.yaml     # then set llm.api_key
conda activate chromapilot
./run_interface.sh                     # prints the ssh tunnel command for your laptop
```

Tunnel from your laptop and open <http://localhost:8800>.

Run it on the **compute node where your resources are allocated**, not a login
node: once you approve a plan, bowtie2/samtools/bedtools/R execute on that
machine.

## Files

| | |
|---|---|
| `agent_runtime.py` | The only file that touches the agent. Imports `chromapilot/agent.py` in place and brings the LLM online. |
| `run_session.py` | One worker thread per run: streams the graph, diffs state into UI events, handles pauses. |
| `knowledge.py` | Parses `Knowledge/node.txt` and `graph.txt` into JSON for the UI. |
| `server.py` | stdlib HTTP + SSE transport. No framework. |
| `static/` | The single-page UI: `index.html`, `style.css`, `app.js`. |
| `config.example.yaml` | Copy to `config.yaml` (git-ignored) and add your key. |

`runs/` (per-run output when you do not name an output directory) and
`config.yaml` are git-ignored.

## Changing the agent

The tool catalog, pipeline map and step highlighting are all read from
`chromapilot/Knowledge/` at start-up, so adding or changing a tool needs no
interface change — restart the server. Three tables mirror the agent graph and
should be checked when `agent.py` changes: `CONTROL_NODES` and `SUB_NODES` in
`agent_runtime.py`, and `PHASE_LABELS` in `static/app.js`.

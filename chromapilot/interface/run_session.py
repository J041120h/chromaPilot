"""run_session.py — drive ONE agent run and translate it into UI events.

This is the piece that replaces the test harness's `run_once`. The difference is
only in *who answers the interrupts*: `run_once` auto-answers every `interrupt()`
with a canned string; we surface the interrupt to the browser and resume with
whatever the human types. The graph, its nodes, and its routing are untouched —
we just consume the stream and feed `Command(resume=...)` back in.

How it works:
  * `graph.stream(input, config, stream_mode=["updates","values"], subgraphs=True)`
    yields `(namespace, mode, chunk)` tuples. For the top-level graph the
    namespace is empty: "updates" chunks are keyed by the node that just ran
    (used to light up the agent-graph) and "values" chunks are the full
    PipelineState snapshot (diffed to derive plan / log / artifacts). Chunks
    from inside the two sub-graphs (auto-code, report) carry a namespace; we
    only use their "updates" to show which sub-step is running.
  * When a stream segment ends we ask `graph.get_state(config)`: an empty `.next`
    means the run finished; a pending interrupt means the graph paused for human
    input. We emit an `interrupt` event and block the worker thread until the
    browser POSTs an answer, then resume.

Everything is emitted through `emit(event_type, **payload)` — a flat-JSON
callback the server turns into Server-Sent-Events.
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback
from urllib.parse import quote

from agent_runtime import (
    NODE_ROLE, NODE_LABEL, SUB_NODE_LABEL, register_console, unregister_console,
)

# File classification for the artifacts panel.
FIGURE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".webp"}
TABLE_EXTS = {".csv", ".tsv", ".xlsx", ".xls", ".txt", ".feather", ".rds", ".parquet", ".json"}
TRACK_EXTS = {".bigwig", ".bw", ".bedgraph", ".bdg", ".bg", ".bed", ".bedpe", ".bam", ".bai",
              ".sam", ".narrowpeak", ".broadpeak"}
SEQ_EXTS = {".fastq", ".fq", ".fasta", ".fa"}
CODE_EXTS = {".py", ".r", ".sh", ".rscript", ".smk"}
LOG_EXTS = {".log", ".err", ".out"}
REPORT_NAMES = {"report.pdf", "report.html"}
# Nodes after which the plan is frozen (the executor rewrites `plan` to the
# current step, so the planner's full plan must be captured before this).
EXECUTION_NODES = {"executor", "tools", "tools_checker", "auto_code_generator", "replayer",
                   "report_generator"}


def classify(path: str) -> str:
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1]
    if name in REPORT_NAMES:
        return "report"
    if ext == ".gz":                       # look inside e.g. x.fastq.gz / x.bed.gz
        ext = os.path.splitext(name[:-3])[1]
        if ext in TRACK_EXTS:
            return "track"
        return "sequence"
    if ext in FIGURE_EXTS:
        return "figure"
    if ext in TRACK_EXTS:
        return "track"
    if ext in SEQ_EXTS:
        return "sequence"
    if ext in CODE_EXTS:
        return "code"
    if ext in LOG_EXTS:
        return "log"
    if ext in TABLE_EXTS:
        return "table"
    return "other"


class RunSession:
    def __init__(self, runtime, run_id, request_text, output_dir, emit,
                 register_root=None):
        self.rt = runtime
        self.run_id = run_id
        self.request_text = request_text
        # output_dir may be None: when the user wrote the directory inside the
        # prompt (rather than the UI field), we leave their prompt untouched and
        # learn the directory back from the planner's extracted state.
        self.output_dir = output_dir
        self.emit = emit
        self._register_root = register_root  # callback to allow-list a learned dir
        self.config = {"configurable": {"thread_id": run_id}}

        self._thread: threading.Thread | None = None
        self._resume_event = threading.Event()
        self._resume_text: str | None = None
        self._stopped = False
        self._await_interrupt = False  # True while parked on a human prompt

        # Derivation state across snapshots.
        self._prev_msg_count = 0
        self._emitted_sigs: set = set()
        self._last_plan_text = None
        self._last_full_plan = None
        self._last_meta = None
        self._last_validation = None
        self._active_node: str | None = None
        self._seen_files: set[str] = set()
        self._steps: list[dict] = []
        self._n_artifacts = 0
        # Step progress is tracked POSITIONALLY (not by tool name — plans often
        # repeat a tool per sample/mark). `_done_count` only moves forward.
        self._done_count = 0
        self._exec_events = 0          # tool results + code executions seen
        self._last_code_return = None
        self._executing = False        # True once the executor has started
        self._finished = False

    # ----------------------------------------------------------------- start
    def start(self):
        # Only inject our output directory when the user gave one in the UI
        # field. If output_dir is None they specified it in the prompt itself,
        # so we pass the prompt through unchanged and respect their location.
        if self.output_dir:
            prompt = self.rt.compose_prompt(self.request_text, self.output_dir)
        else:
            prompt = self.request_text
        self.emit("run_started", run_id=self.run_id, output_dir=self.output_dir,
                  prompt=prompt, model=self.rt.model_name, provider=self.rt.provider,
                  report_enabled=self.rt.report_enabled)
        self._thread = threading.Thread(target=self._drive, args=(prompt,),
                                        name=f"run-{self.run_id}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- main loop
    def _drive(self, prompt):
        # Route this thread's prints (the agent's "meta" stdout) into the stream.
        register_console(self._console_sink)
        # Light up the planner immediately so the UI moves the moment a run
        # starts — the first real node update only arrives after the (long)
        # planner LLM call returns.
        self._on_node("planner")
        try:
            stream_input = self.rt.build_initial_state(prompt)
            while not self._stopped:
                self._run_segment(stream_input)
                if self._stopped:
                    break
                snap = self.rt.graph.get_state(self.config)
                prompt_value = _pending_interrupt(snap)
                if prompt_value is not None:
                    kind = _interrupt_kind(prompt_value)
                    # Light up the node we're paused at (the interrupt fires
                    # mid-node, so it never showed up in the updates stream).
                    waiting = list(getattr(snap, "next", None) or ())
                    for w in waiting:
                        self._on_node(w)
                    self._await_interrupt = True
                    self.emit("interrupt", run_id=self.run_id, kind=kind,
                              prompt=str(prompt_value), node=(waiting[0] if waiting else None))
                    text = self._wait_for_resume()
                    self._await_interrupt = False
                    if text is None:        # user stopped the run
                        break
                    self.emit("resumed", run_id=self.run_id, text=text, kind=kind)
                    stream_input = self.rt.Command(resume=text)
                    continue
                # No interrupt and the segment ended → the run is complete.
                self._finish(success=True)
                return
            # Loop exited because of stop.
            self._finish(success=True, stopped=True)
        except Exception as e:
            self.emit("error", run_id=self.run_id, message=str(e),
                      traceback=traceback.format_exc())
            self._finish(success=False)
        finally:
            unregister_console()

    def _run_segment(self, stream_input):
        """Stream one segment (until END or the next interrupt)."""
        for item in self.rt.graph.stream(
                stream_input, self.config, stream_mode=["updates", "values"],
                subgraphs=True):
            if self._stopped:
                return
            ns, mode, chunk = _unpack(item)
            if ns:
                # Inside a sub-graph (auto-code / report). Only the node updates
                # matter to the UI: they tell which sub-step is running.
                if mode == "updates" and isinstance(chunk, dict):
                    parent = str(ns[0]).split(":", 1)[0]
                    for node_name in chunk.keys():
                        if node_name != "__interrupt__":
                            self._on_subnode(parent, node_name)
                continue
            if mode == "updates":
                for node_name in (chunk or {}).keys():
                    if node_name != "__interrupt__":
                        self._on_node(node_name)
            else:  # "values" — full state snapshot
                self._on_values(chunk)

    # ------------------------------------------------------------- node graph
    def _on_node(self, name):
        # `updates` fires after a node finishes, so the just-seen node is the
        # current frontier. Mark the previous one done, this one active.
        if self._active_node and self._active_node != name:
            self.emit("node", run_id=self.run_id, name=self._active_node, state="done",
                      role=NODE_ROLE.get(self._active_node, "executor"),
                      label=NODE_LABEL.get(self._active_node, self._active_node))
        self._active_node = name
        if name in EXECUTION_NODES:
            self._executing = True   # past plan review → freeze plan, highlight active step
        self.emit("node", run_id=self.run_id, name=name, state="active",
                  role=NODE_ROLE.get(name, "executor"),
                  label=NODE_LABEL.get(name, name))

    def _on_subnode(self, parent, name):
        if self._active_node != parent:
            self._on_node(parent)
        self.emit("subnode", run_id=self.run_id, parent=parent, name=name,
                  label=SUB_NODE_LABEL.get(name, name.replace("_", " ")))

    # --------------------------------------------------------- state snapshot
    def _on_values(self, v):
        if not isinstance(v, dict):
            v = getattr(v, "model_dump", lambda: {})() or {}
        # Output directory (the planner extracts/refines it from the prompt).
        out = v.get("output_dir")
        if out and out != self.output_dir:
            self.output_dir = out
            if self._register_root:
                self._register_root(out)   # allow figures here to be served
            self.emit("output_dir", run_id=self.run_id, output_dir=out)

        # Plan. The planner writes the full plan into `plan`; once execution
        # starts the executor rewrites `plan` to the *current step* (and finally
        # "END"), so we only take plan updates before the executor has run.
        plan_text = v.get("plan")
        if (not self._executing and plan_text and plan_text != self._last_plan_text
                and plan_text.strip().upper() != "END"):
            self._last_plan_text = plan_text
            self._steps = _parse_steps(plan_text)
            self._done_count = 0
            self._apply_step_status()
            self.emit("plan", run_id=self.run_id, text=plan_text, steps=self._steps,
                      output_dir=v.get("output_dir"))

        # Extracted metadata (arrives one node after the plan).
        meta = {
            "overall_plan": v.get("overall_plan"),
            "required_parameters": v.get("required_parameters"),
            "num_of_sample": v.get("num_of_sample"),
            "input_file_path": list(v.get("input_file_path") or []),
            "output_dir": v.get("output_dir"),
        }
        if any(meta.values()) and meta != self._last_meta:
            self._last_meta = meta
            self.emit("plan_meta", run_id=self.run_id, **meta)

        # The executor's append-only history of executed steps, with the
        # actual output paths filled in. Shown in the report.
        fp = v.get("full_plan")
        if fp and fp != self._last_full_plan:
            self._last_full_plan = fp
            self.emit("executed_plan", run_id=self.run_id, text=fp)

        # A finished auto-generated code step is another unit of progress.
        cr = v.get("code_return")
        if cr and cr != self._last_code_return:
            self._last_code_return = cr
            self._exec_events += 1

        # Validation result. Gate on validation_thinking so we only report once
        # the validator node has actually run (validation itself defaults to
        # False in the state and would otherwise fire a false "plan needs work").
        if v.get("validation_thinking"):
            sig = (v.get("validation"), tuple(v.get("missing_elements") or []))
            if sig != self._last_validation:
                self._last_validation = sig
                self.emit("validation", run_id=self.run_id,
                          valid=bool(v.get("validation")),
                          missing=v.get("missing_elements") or [],
                          thinking=v.get("validation_thinking"))

        # New chat messages → log lines, tool calls, execution events.
        self._process_messages(v.get("messages") or [])

        # Advance step progress from the agent's own remaining-plan + exec events.
        self._update_progress(v)

        # Files produced on disk.
        self._scan_artifacts()

    def _update_progress(self, v):
        """Move the positional done-pointer forward. Two independent signals:
        the count of steps the agent has dropped from `downstream_plan`, and the
        number of tool/code executions observed. We take the max (both only grow)
        and never mark the final step done until the run actually completes."""
        n = len(self._steps)
        if not n:
            return
        cand = self._exec_events
        dp = v.get("downstream_plan")
        if self._executing and dp:
            remaining = len(_parse_steps(dp))
            if remaining:
                cand = max(cand, n - remaining)
        cand = max(0, min(cand, n - 1))          # last step completes only at the end
        self._done_count = max(self._done_count, cand)
        before = [s["status"] for s in self._steps]
        self._apply_step_status()
        if [s["status"] for s in self._steps] != before:
            self.emit("steps", run_id=self.run_id, steps=self._steps)

    def _process_messages(self, messages):
        # `PipelineState.messages` has no reducer, so a node that returns only
        # its own messages (LangGraph's ToolNode does: {"messages": [ToolMessage]})
        # REPLACES the list instead of appending. Position-based diffing would
        # then miss the tool result, so when the list shrinks we fall back to
        # emitting every message we have not emitted before.
        if len(messages) >= self._prev_msg_count:
            new = messages[self._prev_msg_count:]
        else:
            new = [m for m in messages if _msg_sig(m) not in self._emitted_sigs]
        self._prev_msg_count = len(messages)
        for msg in new:
            self._emitted_sigs.add(_msg_sig(msg))
            mtype = getattr(msg, "type", "ai")
            text = _content_to_text(getattr(msg, "content", ""))
            # Tool calls requested by the model.
            for tc in (getattr(msg, "tool_calls", None) or []):
                self.emit("tool_call", run_id=self.run_id,
                          name=tc.get("name"), args=_jsonable(tc.get("args")))
            # A tool finished → count it as a unit of progress and emit a result
            # card (the generic message below is skipped for tool messages so we
            # don't log the same output twice).
            if mtype == "tool":
                self._exec_events += 1
                self.emit("tool_result", run_id=self.run_id,
                          name=getattr(msg, "name", None), text=_short_text(text),
                          result=_parse_result(text))
                continue
            if text.strip():
                # Cap very long bodies (e.g. a full plan echoed as a message) —
                # the Plan panel already shows the plan in full.
                self.emit("message", run_id=self.run_id, role=mtype,
                          text=_short_text(text, 1600))

    def _apply_step_status(self):
        """Positional: steps before the done-pointer are done, the one at the
        pointer is active (once execution has started), the rest are pending."""
        n = len(self._steps)
        for i, s in enumerate(self._steps):
            if self._finished:
                s["status"] = "done"
            elif i < self._done_count:
                s["status"] = "done"
            elif i == self._done_count and self._executing and self._done_count < n:
                s["status"] = "active"
            else:
                s["status"] = "pending"

    # ----------------------------------------------------------- artifacts
    def _scan_artifacts(self):
        if not self.output_dir or not os.path.isdir(self.output_dir):
            return
        for root, dirs, files in os.walk(self.output_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for fn in sorted(files):
                if fn.startswith("."):
                    continue
                path = os.path.join(root, fn)
                if path in self._seen_files:
                    continue
                self._seen_files.add(path)
                kind = classify(path)
                rel = os.path.relpath(path, self.output_dir)
                self._n_artifacts += 1
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = None
                self.emit("artifact", run_id=self.run_id, path=path, rel=rel,
                          name=fn, kind=kind, size=size,
                          url=(f"/api/file?path={quote(path)}"
                               if kind in ("figure", "report") else None))

    # ----------------------------------------------------------- interrupts
    def resume(self, text: str):
        self._resume_text = text
        self._resume_event.set()

    def stop(self):
        self._stopped = True
        self._resume_text = None
        self._resume_event.set()

    def _wait_for_resume(self) -> str | None:
        self._resume_event.wait()
        self._resume_event.clear()
        if self._stopped:
            return None
        return self._resume_text if self._resume_text is not None else ""

    def _finish(self, success, stopped=False):
        if self._active_node:
            self.emit("node", run_id=self.run_id, name=self._active_node, state="done",
                      role=NODE_ROLE.get(self._active_node, "executor"),
                      label=NODE_LABEL.get(self._active_node, self._active_node))
        # On a successful finish, every planned step is complete — reflect that
        # in the plan panel (positional tracking can lag the final step).
        if success and not stopped and self._steps:
            self._finished = True
            self._apply_step_status()
            self.emit("steps", run_id=self.run_id, steps=self._steps)
        self._scan_artifacts()
        self.emit("done", run_id=self.run_id, success=success, stopped=stopped,
                  output_dir=self.output_dir, n_artifacts=self._n_artifacts,
                  n_steps=len(self._steps), executed_plan=self._last_full_plan,
                  executing=self._executing)

    # ----------------------------------------------------------- console sink
    def _console_sink(self, text: str):
        for line in text.splitlines():
            if line.strip():
                self.emit("console", run_id=self.run_id, text=line)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _unpack(item):
    """Normalise a stream item to (namespace, mode, chunk) whether or not the
    LangGraph version includes the namespace element."""
    if isinstance(item, tuple):
        if len(item) == 3:
            ns, mode, chunk = item
            return tuple(ns or ()), mode, chunk
        if len(item) == 2:
            return (), item[0], item[1]
    return (), "values", item


def _pending_interrupt(snap):
    # Newer LangGraph exposes snapshot-level interrupts; older ones only per task.
    for it in (getattr(snap, "interrupts", None) or []):
        return getattr(it, "value", str(it))
    for task in getattr(snap, "tasks", []) or []:
        for it in (getattr(task, "interrupts", None) or []):
            return getattr(it, "value", str(it))
    return None


def _interrupt_kind(prompt_value) -> str:
    s = str(prompt_value)
    if s.strip() == "HUMAN_IN_THE_LOOP":
        return "code"
    if "figure" in s.lower():
        return "figures"
    return "plan"


_STEP_RE = re.compile(r"^\s*(?:\*\*|#+\s*)?Step\s+(\d+)\s*(?:[:.)\-–—]|\*\*)?\s*(.*)$", re.IGNORECASE)
_FUNC_RE = re.compile(r"^\s*(?:[-•*]\s*)?(?:\*\*)?Function(?:\*\*)?\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
AUTO_CODE = "Automatic code generation"


def _parse_steps(plan_text: str) -> list[dict]:
    steps: list[dict] = []
    cur = None
    for raw in plan_text.splitlines():
        line = raw.strip()
        m = _STEP_RE.match(line)
        if m:
            title = m.group(2).strip().strip("*").strip()
            cur = {"index": int(m.group(1)), "title": title,
                   "function": None, "auto_code": False, "status": "pending",
                   "detail": []}
            steps.append(cur)
            continue
        if cur is None:
            continue
        fm = _FUNC_RE.match(line)
        if fm and not cur["function"]:
            val = fm.group(1).strip().strip("`*'\"")
            if "automatic" in val.lower() or "auto-code" in val.lower() or "auto code" in val.lower():
                cur["function"] = AUTO_CODE
                cur["auto_code"] = True
            else:
                tok = re.match(r"^([a-zA-Z_]\w*)", val)
                cur["function"] = tok.group(1) if tok else val
            continue
        if line and len(cur["detail"]) < 40:
            cur["detail"].append(line)
    # If "Function:" lines were absent, fall back to the step title token.
    for s in steps:
        if not s["function"]:
            t = s["title"]
            if "automatic" in t.lower():
                s["function"], s["auto_code"] = AUTO_CODE, True
            else:
                tok = re.match(r"^[`'\"]?([a-zA-Z_]\w*)", t)
                s["function"] = tok.group(1) if tok else None
        s["detail"] = "\n".join(s["detail"])
    return steps


def _msg_sig(msg):
    """Stable identity for a chat message (LangChain ids are often None)."""
    mid = getattr(msg, "id", None)
    if mid:
        return ("id", mid)
    return (getattr(msg, "type", ""), getattr(msg, "name", None),
            getattr(msg, "tool_call_id", None),
            hash(_content_to_text(getattr(msg, "content", ""))[:4000]))


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _parse_result(text: str):
    """Tool results are usually a JSON-encoded dict of output paths; surface it
    structured so the UI can render key → path rows."""
    try:
        obj = json.loads(text)
    except Exception:
        return None
    return _jsonable(obj) if isinstance(obj, (dict, list)) else None


def _jsonable(obj, depth: int = 0):
    if depth > 6:
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x, depth + 1) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _short_text(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …"

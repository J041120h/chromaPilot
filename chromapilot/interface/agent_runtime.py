"""agent_runtime.py — the single bridge between the web interface and the real
LangGraph agent system.

Design rule for this whole `interface/` package: **we never modify the agent.**
We import the compiled graph from `agent.py` exactly as it is, bring the
LLM online the same way `agent.__main__` does, and then *drive* the graph
ourselves so a human can answer its `interrupt()` calls from a web form instead
of the canned auto-feedback string used by the test harness.

Everything that touches `agent` lives in this one file, so the rest of the
interface stays small and readable.

What importing `agent` does (heavy, one-time, no LLM key required):
  * builds the RAG retrievers / FAISS index / reranker at module load,
  * compiles the sub-graphs and the main `graph` (a CompiledStateGraph with a
    MemorySaver checkpointer).
Because that costs tens of seconds, we do it once, at server start-up.

After import we replicate `agent.__main__`'s three setup lines so the
module-global singletons the nodes read (`CONFIG`, `model`, the RAG retrievers)
are populated:

    agent.CONFIG = agent.build_runtime_config(CONFIG_PATH)
    agent.model  = agent.CONFIG.llm
    agent._init_rag_retrievers(agent.CONFIG.llm)

These are attribute assignments on the imported module object — they update the
exact globals the node functions close over. (A `from agent import CONFIG`
copy would NOT work; the nodes read the module global, not our local name.)
"""

from __future__ import annotations

import os
import sys
import threading

# All paths are derived from this file's location so the interface is portable —
# it works from any checkout with no absolute paths baked in.
INTERFACE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_PKG_DIR = os.path.dirname(INTERFACE_DIR)          # the real agent package
PROJECT_ROOT = os.path.dirname(AGENT_PKG_DIR)
KNOWLEDGE_DIR = os.path.join(AGENT_PKG_DIR, "Knowledge")


CONFIG_PATH = os.path.join(INTERFACE_DIR, "config.yaml")


def resolve_config_path() -> str:
    """The interface uses ONE explicit config file: interface/config.yaml (or the
    path in $IFACE_CONFIG). No auto-discovery — edit that file to set your keys."""
    env = os.environ.get("IFACE_CONFIG", "").strip()
    path = os.path.abspath(os.path.expanduser(env)) if env else CONFIG_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config not found: {path}\n"
            "Copy 'config.example.yaml' to 'config.yaml' in the interface folder "
            "and fill in your llm.api_key (and optionally a second provider).")
    return path

# The control-flow nodes of the main LangGraph, grouped into the four roles the
# UI shows. Order matches the typical execution order. Each tuple is
# (node_name, role, human_label). `role` drives the colour-coding in the UI.
CONTROL_NODES = [
    ("planner",             "planner",   "Planner"),
    ("extract_input_state", "planner",   "Extract inputs"),
    ("user_feedback_node",  "planner",   "Plan review"),
    ("validator",           "validator", "Validator"),
    ("executor",            "executor",  "Executor"),
    ("tools",               "executor",  "Run tool"),
    ("tools_checker",       "executor",  "Tool checker"),
    ("auto_code_generator", "executor",  "Code generator"),
    ("replayer",            "executor",  "Replayer"),
    ("report_generator",    "reporter",  "Report"),
]
NODE_ROLE = {name: role for name, role, _ in CONTROL_NODES}
NODE_LABEL = {name: label for name, _, label in CONTROL_NODES}

# The two compiled sub-graphs the main graph embeds ("auto_code_generator" and
# "report_generator"). Their inner nodes never appear in the top-level updates
# stream, so we stream with subgraphs=True and show them as sub-steps.
SUB_NODES = {
    "auto_code_generator": [
        ("auto_code_generator", "Generate code"),
        ("code_executor",       "Execute code"),
        ("error_checker",       "Check for errors"),
        ("human_in_the_loop",   "Needs your approval"),
        ("summary",             "Summarise result"),
    ],
    "report_generator": [
        ("figure_interpretation",        "Interpret figures"),
        ("figure_description",           "Describe figures"),
        ("figure_feedback",              "Figure review"),
        ("section_planning",             "Plan sections"),
        ("build_section_with_web_search", "Write sections"),
        ("compile_report",               "Compile PDF"),
    ],
}
SUB_NODE_LABEL = {name: label for subs in SUB_NODES.values() for name, label in subs}


# --------------------------------------------------------------------------- #
# Thread-aware stdout/stderr router
# --------------------------------------------------------------------------- #
# The agent prints a lot of useful "meta" progress to stdout (e.g.
# "Planner is deriving plan", "[Extractor] ..."). We want to surface that in the
# UI without losing the server's own console. Each run is driven on its own
# worker thread, so we install a router that forwards writes from a *registered*
# thread to that run's sink, while every other thread (including the HTTP server)
# keeps writing to the real stream.
class _ThreadRouter:
    def __init__(self, base):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_sinks", {})
        object.__setattr__(self, "_lock", threading.Lock())

    def register(self, sink):
        with self._lock:
            self._sinks[threading.get_ident()] = sink

    def unregister(self):
        with self._lock:
            self._sinks.pop(threading.get_ident(), None)

    def write(self, text):
        sink = self._sinks.get(threading.get_ident())
        if sink is not None:
            try:
                sink(text)
            except Exception:
                pass
        return self._base.write(text)

    def flush(self):
        try:
            self._base.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        # Delegate anything we don't implement (encoding, fileno, isatty, ...).
        return getattr(self._base, name)


_stdout_router: _ThreadRouter | None = None
_stderr_router: _ThreadRouter | None = None


def register_console(sink):
    """Route the *current thread's* stdout/stderr to `sink` (a callable taking a
    str). Call from inside a run's worker thread."""
    if _stdout_router is not None:
        _stdout_router.register(sink)
    if _stderr_router is not None:
        _stderr_router.register(sink)


def unregister_console():
    if _stdout_router is not None:
        _stdout_router.unregister()
    if _stderr_router is not None:
        _stderr_router.unregister()


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
class Runtime:
    """Holds the compiled graph and the handful of agent helpers the interface
    needs. A single instance is created by `boot()`."""

    def __init__(self, agent, config_path, providers, current_id):
        self.A = agent
        self.config_path = config_path
        self.graph = agent.graph
        self.END = agent.END
        self.Command = agent.Command
        self.config = agent.CONFIG
        self.report_enabled = bool(agent.CONFIG.report_enabled)
        # Available LLM providers (Claude / OpenAI) the user can switch between,
        # and which one is currently active.
        self.providers = providers          # list of {id, provider, model, label, ...}
        self.current_id = current_id
        self._lock = threading.Lock()
        # Raw knowledge strings — identical to what every run receives as
        # node_context / graph_context.
        self.node_txt = _read(os.path.join(KNOWLEDGE_DIR, "node.txt"))
        self.graph_txt = _read(os.path.join(KNOWLEDGE_DIR, "graph.txt"))

    @property
    def provider(self):
        return self.A.CONFIG.provider

    @property
    def model_name(self):
        return self.A.CONFIG.model

    def activate(self, provider_id: str, log=print):
        """Switch the active LLM (e.g. Claude ↔ OpenAI). Rebinds the agent's
        CONFIG/model and re-inits the RAG retrievers, all in place. No-op if the
        requested provider is already active."""
        with self._lock:
            if provider_id == self.current_id:
                return
            entry = next((p for p in self.providers if p["id"] == provider_id), None)
            if entry is None:
                raise ValueError(f"unknown provider '{provider_id}'")
            cfg = self.A.CONFIG
            cfg.provider = entry["provider"]
            cfg.model = entry["model"]
            cfg.api_key = entry["api_key"]
            cfg.admin_api_key = entry.get("admin_api_key", "") or ""
            cfg.base_url = entry.get("base_url") or None
            cfg.init_model()                       # rebuild cfg.llm for this provider
            self.A.model = cfg.llm
            self.A._init_rag_retrievers(cfg.llm)
            self.current_id = provider_id
            log(f"[provider] switched to {entry['label']}")

    def build_initial_state(self, prompt: str) -> dict:
        """The three keys the graph needs to start, exactly as `run_once` builds
        them."""
        return {
            "user_input": prompt,
            "node_context": self.node_txt,
            "graph_context": self.graph_txt,
        }

    def compose_prompt(self, request_text: str, output_dir: str) -> str:
        """Append the run's output directory to the user's request the same way
        the test harness does (reusing the agent's own helper so behaviour is
        identical). The planner re-extracts output_dir from the prompt text, so
        it must live in the string — seeding state is not enough."""
        return self.A.build_run_prompt(request_text, {}, output_dir)


_runtime: Runtime | None = None
_boot_lock = threading.Lock()


def boot(config_path: str | None = None, log=print) -> Runtime:
    """Import the agent, bring the LLM online, and return the shared Runtime.
    Idempotent: safe to call more than once."""
    global _runtime, _stdout_router, _stderr_router
    with _boot_lock:
        if _runtime is not None:
            return _runtime

        cfg = os.path.abspath(config_path) if config_path else resolve_config_path()
        if not os.path.exists(cfg):
            raise FileNotFoundError(f"Runtime config not found: {cfg}")
        log(f"[boot] using config: {cfg}")

        # Install the console router before the heavy import so even import-time
        # prints can be captured later (they go to the real stream for now).
        if _stdout_router is None:
            _stdout_router = _ThreadRouter(sys.stdout)
            sys.stdout = _stdout_router
        if _stderr_router is None:
            _stderr_router = _ThreadRouter(sys.stderr)
            sys.stderr = _stderr_router

        if AGENT_PKG_DIR not in sys.path:
            sys.path.insert(0, AGENT_PKG_DIR)

        # argparse in agent only runs under __main__, but keep argv clean.
        saved_argv = sys.argv
        sys.argv = ["interface"]
        try:
            log("[boot] importing agent (builds RAG index, compiles graph)…")
            import agent  # noqa: E402  heavy, one-time
            log("[boot] agent imported. Bringing the LLM online…")
            agent.CONFIG = agent.build_runtime_config(cfg)
            # Optional speed lever: IFACE_MODEL overrides just the model id (same
            # graph, same prompts — only a faster/cheaper LLM). The latency of a
            # run is dominated by the agent's chain of LLM calls, so a faster
            # model is the cleanest way to speed things up.
            override = os.environ.get("IFACE_MODEL", "").strip()
            if override:
                log(f"[boot] IFACE_MODEL override → {override}")
                agent.CONFIG.model = override
                agent.CONFIG.init_model()      # rebuild llm with the new id
            agent.model = agent.CONFIG.llm
            agent._init_rag_retrievers(agent.CONFIG.llm)
            providers, current_id = _build_providers(agent, cfg)
        finally:
            sys.argv = saved_argv

        _runtime = Runtime(agent, cfg, providers, current_id)
        log(f"[boot] ready — active={_runtime.model_name} ({_runtime.provider}); "
            f"available providers: {', '.join(p['id'] for p in providers)}; "
            f"report_enabled={_runtime.report_enabled}")
        return _runtime


_PROVIDER_LABEL = {"anthropic": "Claude", "openai": "OpenAI", "google": "Google"}


def _build_providers(agent, cfg_path):
    """Discover the LLM providers the user can switch between: the primary (the
    one RuntimeConfig loaded) plus an optional second one from the llm.alt_*
    fields. Returns (providers_list, active_id)."""
    import yaml
    cfg = agent.CONFIG
    raw = (yaml.safe_load(open(cfg_path)) or {}).get("llm", {})

    def label(provider, model):
        return f"{_PROVIDER_LABEL.get(provider, provider.title())} — {model}"

    primary = {
        "id": cfg.provider, "provider": cfg.provider, "model": cfg.model,
        "api_key": cfg.api_key, "admin_api_key": cfg.admin_api_key,
        "base_url": cfg.base_url, "label": label(cfg.provider, cfg.model),
    }
    providers = [primary]

    alt_key = (raw.get("alt_api_key") or "").strip()
    if alt_key:
        alt_provider = agent.RuntimeConfig._detect_provider_from_key(alt_key)
        if alt_provider and alt_provider != cfg.provider:
            providers.append({
                "id": alt_provider, "provider": alt_provider,
                "model": raw.get("alt_model") or "", "api_key": alt_key,
                "admin_api_key": (raw.get("alt_admin_api_key") or ""),
                "base_url": raw.get("alt_base_url") or None,
                "label": label(alt_provider, raw.get("alt_model") or ""),
            })
    return providers, cfg.provider


def get_runtime() -> Runtime | None:
    return _runtime


def _read(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return ""

#!/usr/bin/env bash
# Launch the ChromaPilot web interface.
#
# 1. Point it at the conda env that runs the agent, either way works:
#        conda activate <your-agent-env> && ./run_interface.sh
#        IFACE_PYTHON=/path/to/that/env/bin/python ./run_interface.sh
#
#    Either way this script puts that env's bin/ on PATH. That matters: the
#    pipeline tools shell out to samtools / bedtools / bowtie2 / bedGraphToBigWig
#    / picard, and naming the interpreter directly does NOT put them on PATH the
#    way `conda activate` does — without this the agent plans fine and then every
#    tool dies with "No such file or directory: 'samtools'".
#
# 2. Run it on a properly allocated COMPUTE NODE, not a login node. The agent
#    builds a RAG index at start-up and — once you approve a plan — executes the
#    real bioinformatics tools on THIS machine. (LLM calls are remote; no GPU.)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PY="${IFACE_PYTHON:-python3}"        # the active env's python, unless overridden
export IFACE_PORT="${IFACE_PORT:-8800}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: '$PY' not found. Activate your agent conda env, or set IFACE_PYTHON."
  exit 1
fi
if ! "$PY" -c "import langgraph" >/dev/null 2>&1; then
  echo "ERROR: langgraph is not importable with '$PY'."
  echo "Activate the conda env that runs the agent (the one with langgraph):"
  echo "    conda activate <your-agent-env>     # name differs per server"
  echo "then re-run, or set IFACE_PYTHON=/path/to/that/env/bin/python."
  exit 1
fi

# ---------------------------------------------------------------- environment
# Put the interpreter's own bin/ first on PATH and expose its env root, so the
# subprocesses the tools spawn find the same binaries a `conda activate` would.
# Harmless when the env is already active (the entry is simply already first).
PY_ABS="$(command -v "$PY")"
PY_BIN="$(cd "$(dirname "$(readlink -f "$PY_ABS")")" && pwd)"
case ":$PATH:" in
  *":$PY_BIN:"*) ;;
  *) export PATH="$PY_BIN:$PATH" ;;
esac
ENV_ROOT="$(dirname "$PY_BIN")"
[ -d "$ENV_ROOT/conda-meta" ] && export CONDA_PREFIX="$ENV_ROOT"

# Preflight: report what the pipeline needs and whether it is reachable. Missing
# tools are a warning, not a failure — not every request uses every tool — but
# they are named up front instead of surfacing hours later inside a tool log.
MISSING=""
for t in samtools bedtools bowtie2 cutadapt bedGraphToBigWig picard Rscript; do
  command -v "$t" >/dev/null 2>&1 || MISSING="$MISSING $t"
done
if [ -n "$MISSING" ]; then
  echo "WARNING: these pipeline tools are NOT on PATH:$MISSING"
  echo "         Plans that need them will fail at execution time."
  echo "         PATH begins: $PY_BIN"
else
  echo "pipeline tools OK (samtools, bedtools, bowtie2, cutadapt, bedGraphToBigWig, picard, Rscript)"
fi

echo "starting ChromaPilot interface (python $("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])'); first run imports the agent + RAG index, ~1–2 min)…"
exec "$PY" -u server.py

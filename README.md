# ChromaPilot

**An LLM agent that plans, runs and reports multiplexed epigenomics analyses.**

ChromaPilot turns a plain-language request — *"align these Hiplex CUT&Tag FASTQs
against hg38, call peaks per CRF pair, and compare the C and T groups"* — into a
reviewed, executed and documented analysis. It plans the pipeline, shows you the
plan before anything runs, executes it with a validated tool library, writes its
own code when no tool fits, and finishes with a reproducible script and a PDF
report.

It supports **Hiplex CUT&Tag** and **ChIP-DIP** data end to end, from raw FASTQ
to peaks, count matrices, biclusters, differential regions and figures.

<p align="center">
  <img src="docs/images/overview.png" alt="ChromaPilot architecture: planning, execution and output stages" width="100%">
</p>

**(A) Planning.** You describe the data and the goal. The planner retrieves
domain knowledge only when it needs it (conditional RAG) and drafts a
step-by-step plan. **You review that plan** and can send it back for revision as
many times as you like; a validator then checks it for completeness before
anything executes.

**(B) Execution.** The executor walks the approved plan one step at a time. Steps
that match the tool library run the validated implementation (pre-processing →
count matrix / BED / bigWig / bedGraph → clustering, differential and peak
analysis). Steps with no matching tool go to the autocode generator, which
writes code, runs it, checks the output and debugs itself until the step
succeeds.

**(C) Output.** The replayer condenses the whole session into a single
reproducible Python script (`summary/summary.py`), alongside the executed plan
and the full message history. The report generator then interprets each figure,
retrieves supporting literature and compiles everything into `report.pdf`.

---

## Contents

| | |
|---|---|
| [Installation](docs/installation.md) | Requirements, `setup.sh`, reference genomes |
| [Configuration](docs/configuration.md) | `config.yaml`, choosing a model, context limits |
| [Quick start](docs/quickstart.md) | Your first run, and how to phrase a request |
| [Web interface](docs/interface.md) | Launching it, the SSH tunnel, every panel explained |
| [Tool library](docs/tools.md) | The 13 tools, what they produce, how they connect |
| [Troubleshooting](docs/troubleshooting.md) | Common failures and what they mean |
| [Benchmarks](benchmarks/README.md) | Reproducing the evaluations in the paper |

---

## Install

```bash
git clone https://github.com/J041120h/ChromaPilot.git
cd ChromaPilot
bash setup.sh                      # creates a conda env named 'chromapilot'
```

`setup.sh` builds the conda environment from `environment.yaml`, installs the R
and Bioconductor packages, installs the `multiEpiCore` analysis package, and
creates `config.yaml` from the template. It takes roughly 30 minutes.

Then add your LLM API key:

```yaml
# config.yaml
llm:
  provider: 'auto'                 # detected from the key prefix
  model: 'claude-sonnet-4-6'
  api_key: 'sk-ant-...'            # or an OpenAI 'sk-...' key
```

Full details, including a different environment name and how reference genomes
are fetched, are in **[docs/installation.md](docs/installation.md)**.

## Run

### The web interface (recommended)

```bash
conda activate chromapilot
cd chromapilot/interface
./run_interface.sh                 # prints the ssh tunnel command to run on your laptop
```

Open **http://localhost:8800** and type your request. The page streams the
planner's reasoning, the plan and its progress, every tool call, and the figures
and files the run writes — and pauses for you to approve or revise the plan
before anything executes.

See **[docs/interface.md](docs/interface.md)** for the tunnel, the panels, and
how to answer the agent when it asks.

### The command line

```bash
conda activate chromapilot
python chromapilot/agent.py -r examples/01_hiplex_preprocessing.yaml --interactive
```

Request files are small YAML documents; four annotated starters are in
[`examples/`](examples/). Drop `--interactive` to answer every review pause
automatically with `auto_feedback` — that is how the benchmark runs work.

### From Python

```python
from chromapilot import agent          # builds the retrieval index, compiles the graph
agent.CONFIG = agent.build_runtime_config("config.yaml")
agent.model  = agent.CONFIG.llm
agent._init_rag_retrievers(agent.CONFIG.llm)
agent.graph.stream(...)                # the compiled LangGraph state machine
```

## Repository layout

```
ChromaPilot/
├── chromapilot/            The agent
│   ├── agent.py              LangGraph state machine: planner, validator,
│   │                         executor, autocode sub-graph, reporter
│   ├── rag_pipeline.py       Retrieval: multi-query expansion + reranking
│   ├── rag_source_loader.py  Loads and caches the documentation corpus
│   ├── Knowledge/            node.txt (tool catalog) + graph.txt (tool DAG),
│   │                         read verbatim by the planner
│   ├── rag_sources/          Source list and prebuilt retrieval index
│   └── interface/            The web interface (stdlib HTTP + SSE, no framework)
├── tools/                  The tool library the agent executes
│   ├── *.py                  One module per tool
│   ├── R_scripts/            R entry points (limma-voom, biclustering, rGREAT)
│   └── chipdip_prep/         The ChIP-DIP Snakemake workflow
├── examples/               Annotated starter requests
├── benchmarks/            Request files for the evaluations in the paper
├── docs/                  Documentation
├── ref/                   Reference genomes (downloaded on first use)
├── config.example.yaml    Copy to config.yaml and add your key
├── environment.yaml       Pinned conda environment
└── setup.sh               One-command install
```

## How it works

The agent is a LangGraph state machine. Each request flows through the same
nodes, and three of them stop and wait for you:

| Node | What it does | Pauses for you |
|---|---|---|
| **planner** | Extracts inputs, samples and goal; decides whether it needs RAG; drafts the plan | |
| **human review** | Shows you the plan | **yes** — approve, revise, or stop |
| **validator** | Checks the plan for missing steps and unmet dependencies | |
| **executor** | Runs the plan step by step, choosing tool or generated code | |
| **autocode** | generate → run → check → debug, looping until the step succeeds | **only if stuck** — after 5 failed attempts |
| **reporter** | Interprets figures, retrieves literature, compiles the PDF | **yes** — review the figures |

(The reporter runs only when `report_enabled: 'true'`.)

Because the plan is reviewed before execution, a run costs you a few LLM calls
(~20 s for a tool-only request, ~60-90 s when retrieval is needed) before you
decide whether to commit any compute.

<p align="center">
  <img src="docs/images/detailed_overview.jpg" alt="Detailed agent graph" width="90%">
</p>

## Requirements

* Linux x86-64 (the pinned environment is `linux-64`)
* conda or mamba
* An API key for Anthropic, OpenAI or Google
* A compute node for execution — the pipeline tools (bowtie2, samtools,
  bedtools, cutadapt, picard, R) run locally and are CPU- and memory-hungry.
  Only the LLM calls are remote.
* ~4 GB disk per reference genome, downloaded on first use

## Citation

If you use ChromaPilot in your work, please cite the accompanying paper. See
[`CITATION.cff`](CITATION.cff).

## License

MIT — see [LICENSE](LICENSE).

Third-party components keep their own licenses:

* `tools/chipdip_prep/` bundles the ChIP-DIP Snakemake workflow and the
  `BarcodeIdentification` tool from the Guttman Lab (Caltech). See
  [`tools/chipdip_prep/NOTICE.md`](tools/chipdip_prep/NOTICE.md).
* The R analysis routines are provided by
  [multiEpiCore](https://github.com/Qingjie-Yu/multiEpiCore), installed by
  `setup.sh`.

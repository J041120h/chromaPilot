# Troubleshooting

## Setup

**`Could not find runtime config file. Tried: .../config.yaml`**
`setup.sh` did not finish, or you are running from a different checkout. Create
it by hand: `cp config.example.yaml config.yaml`. The web interface needs its
own copy in `chromapilot/interface/`, and does not fall back to the root one.

**`The 'api_key' field in config.yaml is missing or empty`**
Exactly what it says. The template ships with `YOUR_API_KEY_HERE`, which counts
as unset.

**`Could not auto-detect provider from the API key`**
`provider: 'auto'` only recognises Anthropic (`sk-ant-…`) and OpenAI (`sk-…`)
prefixes. For Google, or a key behind a proxy, name the provider explicitly.

**`The 'report_enabled' field in config.yaml is invalid`**
It must be a quoted boolean-like string: `'true'` or `'false'`. Same for
`rag_verbose` and `langsmith.trace`. Unquoted `true` also parses, but keep the
quotes to match the template.

**`Failed to initialize LLM model`**
The provider/model/key combination was rejected. Check the model id against the
provider's model list — a Claude model id with an OpenAI key fails here, and so
does a model your account cannot access.

**R packages fail to install**
`setup.sh` isolates R inside the conda environment precisely so a system R
library cannot interfere. If installation still fails, confirm nothing is
overriding it:

```bash
conda activate chromapilot
env | grep ^R_          # should show only R_LIBS_USER / R_ENVIRON_USER / R_PROFILE_USER
                        # pointing inside $CONDA_PREFIX
Rscript -e '.libPaths()'
```

A stale `~/.Renviron` or `~/.Rprofile` is the usual culprit. `preprocessCore` is
deliberately built from source with threading disabled; leave that flag alone.

## Running

**`No such file or directory: 'samtools'`** (or bowtie2, bedtools, cutadapt,
picard, `bedGraphToBigWig`)
The environment is not on `PATH`. Naming an interpreter directly is not the same
as activating the environment. Either `conda activate chromapilot` first, or let
`run_interface.sh` do it — it puts the interpreter's `bin/` on `PATH` and warns
about missing binaries before you submit anything.

**`ModuleNotFoundError` inside a generated-code step, but the import works in
your shell**
`conda_env` in `config.yaml` does not match the environment you actually
created. The agent shells out to that name when running generated code.

**The reference genome downloads every time**
The MD5 check against `ref/ref.json` is failing, usually from a truncated
download. Delete the offending directory under `ref/` and let it fetch again.

**The run stops with "Max wall time reached" / "Max steps reached"**
Your `max_wall_time_s`, `max_steps` or `max_interrupts` is too low for the
pipeline. Alignment of several samples takes hours; `max_wall_time_s: 15000`
(just over 4 hours) is the value the benchmark configs use, and heavier runs need
more.

**Generated code keeps failing and the agent asks for help**
The self-correction loop gave up after five attempts. The prompt tells you what
it was trying; reply with a note about what is going wrong — a wrong column name
or an unstated file format is the common cause — and it retries once. Every
attempt is saved in `<out_dir>/auto_code/`, so you can read the actual script.

**Rate limit errors**
The agent sleeps 60 s and retries up to three times per call. Persistent limits
mean the account's quota is genuinely exhausted.

## Planning

**The plan uses the wrong genome, samples or paths**
Say so at the plan review — *"use mm10, not hg38"*, *"only process C1 and C2"* —
and the planner revises. This is what the review pause is for; there is no limit
on rounds.

**The plan is missing a step you wanted**
Usually the request left it implicit. Be explicit about the goal chain:
*"…call peaks per CRF pair, then run pathway enrichment on those peaks, then
compare the C and T groups."*

**Biclustering was skipped**
By design: `biclustering_wrapper` is only invoked when you ask for it by name.

**The agent refuses hg19**
Only hg38 and mm10 are supported. There is no workaround short of adding the
genome to `tools/ref_prep.py` and `ref/ref.json`.

**A plan step ran per sample when it should have been joint (or vice versa)**
`biclustering_wrapper` is meant to run once per analysis *group* with every
count matrix of that group passed together. If the plan splits it per sample,
say at the review: *"bicluster C1 and C2 jointly as one group"*.

## The web interface

**The page does not load at `localhost:8800`**
The tunnel shape is wrong for your cluster. In the ssh session itself, run
`curl -s -m 5 http://<node>:8800/api/health`. A `{"ready": ...}` reply means the
tunnel form is right and something else is wrong; no reply means node-to-node
traffic is blocked — use the `-J` jump-host form. All three forms are printed by
the launcher and listed in [interface.md](interface.md#2-tunnel-to-it-from-your-laptop).

**It says "importing agent…" for a long time**
Normal on the first start: the retrieval index loads in 1-2 minutes. Much longer
than that on a *login* node usually means it is being throttled — launch it on
your allocated compute node.

**`langgraph is not importable`**
The launcher checked and the environment is wrong. `conda activate chromapilot`,
or set `IFACE_PYTHON=/path/to/that/env/bin/python`.

**The run vanished after a restart**
Run state is in memory. Restarting the *server* clears in-flight runs. Dropping
the tunnel or reloading the page is safe — the page re-attaches and replays what
it missed.

**No model dropdown appears**
It only shows with two providers configured. Set both `llm.api_key` and
`llm.alt_api_key` in `chromapilot/interface/config.yaml`.

**No report button / no `report.pdf`**
`report_enabled` is `'false'`. Figures the tools wrote are still in the gallery
and on disk; only the narrated PDF is skipped.

## Getting more detail

* `<out_dir>/log/` — one log per tool call, with the exact command line.
* `<out_dir>/stdout.log` and `stderr.log` — the complete transcript.
* `rag_verbose: 'true'` — shows which documents the retriever used per decision.
* `langsmith.trace: 'true'` — records every LLM call, tool call and state
  transition at <https://smith.langchain.com>. This is the fastest way to see
  why a planner decision went the way it did.

If you open an issue, the plan, the failing step's log, and the relevant part of
`stderr.log` are what make it actionable.

# Benchmarks

The request files behind the evaluations reported in the paper. Each is a
ChromaPilot request config — the same format as [`examples/`](../examples/) —
and runs with:

```bash
python chromapilot/agent.py -r benchmarks/<set>/<task>.json
```

**Paths are placeholders.** Every `/path/to/...` in these files, including
`root_output_dir`, must be pointed at your own data before the file will run.
The original paths were on the authors' cluster; the requests are otherwise
verbatim.

These run **unattended**: `auto_feedback` answers every review pause, so the
agent plans and executes without a human in the loop. That is what makes the
success rates comparable. Add `--interactive` to step through one by hand.

`n_runs` is the number of repeats per request, which is how the success rate is
computed. Lower it to 1 while you are checking that a config works against your
data.

---

## `specificity/` — how much instruction detail the agent needs

The same task written three ways, from a bare statement of the goal to a fully
specified protocol. It measures how much the agent can infer on its own.

| | |
|---|---|
| `preprocess1` | Goal only: "process these data and obtain aligned BAM files using hg38" |
| `preprocess2` | The same goal, broken into numbered steps |
| `preprocess3` | The same steps, with methods and parameters named explicitly |
| `peakanalysis1-3` | The same three levels for peak calling and differential peak analysis |

## `pipelines/` — end-to-end coverage of the tool library

Eleven tasks spanning both protocols and every tool.

| | |
|---|---|
| `hiplex1-5` | Hiplex CUT&Tag, increasing depth: alignment only, through coverage tracks and biclustering, to peak calling with enrichment and differential analysis |
| `chipdip1-2` | ChIP-DIP, from raw FASTQ through biclustering and differential analysis |
| `autocode1-4` | Tasks with **no** matching tool, which must be solved by code generation — signal correlation, genome-track plotting, scRNA-seq preprocessing |

`_task_coverage.json` records the tool path each Hiplex and ChIP-DIP task is
expected to traverse, written as `first_tool → last_tool`.

## `paraphrase/` — robustness to how the request is worded

Each file holds up to seven phrasings of one `pipelines/` task, written to vary
the way a real user might: terse notes, conversational prose, different orderings,
implicit versus explicit parameters. All seven should produce the same plan.

These use the `prompts:` form of a request file, which runs every entry:

```json
{
  "prompts": {
    "user1": "Please take the BAM files at /path/to/... and ...",
    "user2": "I have some bam files in /path/to/... Plot genome tracks ..."
  },
  "root_output_dir": "/path/to/results/paraphrase/autocode2",
  "n_runs": 3
}
```

Each entry gets its own sub-directory under `root_output_dir`, named for its key.

## `replicates/` — stability across biological samples

`hiplex1` and `hiplex5` run against four independent samples, testing that the
agent handles real data variation rather than one convenient dataset.

---

## Reading the results

Every run writes `stdout.log`, `stderr.log` and its outputs into a timestamped
directory, and `summary.txt` lands in `root_output_dir` when all runs finish:

```
Success rate     : 18/20 (90.0%)
Total runtime    : 41203.7s
Avg runtime      : 2060.2s
Total tokens     : 4821003
```

Per-run lines carry the run index, pass/fail, runtime, tokens and output
directory, plus the error message for anything that failed.

Token counts need an organization admin key in `config.yaml`
(`llm.admin_api_key`); without one they read `0` and everything else still works.

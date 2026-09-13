# Quick start

This walks through one real run: what you type, what the agent asks you, and
what you get back. It assumes [installation](installation.md) is done and your
API key is in `config.yaml`.

> **Run on a compute node, not a login node.** Once you approve a plan, bowtie2,
> samtools, bedtools and R execute on whatever machine the agent is running on.

---

## 1. Describe your analysis

A request is plain English. The agent needs four things, and will plan badly if
any of them is missing:

| | Example |
|---|---|
| **Where the data is** | `/data/hiplex/fastq`, plus any barcode or annotation files |
| **What kind of data** | "raw paired-end FASTQ from Hiplex Cut&Tag", "BAM from ChIP-DIP" |
| **Which genome** | `hg38` or `mm10` (hg19 is not supported) |
| **What you want** | "aligned BAM files", "peaks per CRF pair, then differential analysis between C and T" |

Name your samples if the data contains more than one, and say which subset to
process if it is not all of them.

A good request:

```
We have raw paired-end FASTQ files generated from Hiplex Cut&Tag experiments,
located at:

/data/hiplex/fastq

The barcode configuration file is located at:

/data/hiplex/barcode/barcode.csv

The goal is to identify peak regions for each CRF pair using hg38 as the
reference genome, then perform pathway enrichment analysis on those peaks.
Differential peak analysis between the C and T groups should also be conducted.

The dataset contains four samples: C1, C2, T1, and T2.

All intermediate files and final results should be written to the following
output directory:
```

Ending on that last line matters on the command line: the runner appends the
run's output directory right after it. (In the web interface there is a field
for the output directory instead.)

## 2. Start a run

### Web interface — recommended

```bash
conda activate chromapilot
cd chromapilot/interface
./run_interface.sh
```

Tunnel to it and open <http://localhost:8800>. Full instructions, including
which `ssh` command to use, are in [interface.md](interface.md).

### Command line

Copy an example, edit the paths, and run it:

```bash
cp examples/01_hiplex_preprocessing.yaml my_run.yaml
$EDITOR my_run.yaml                       # set the /path/to/... lines and root_output_dir
conda activate chromapilot
python chromapilot/agent.py -r my_run.yaml --interactive
```

`--interactive` lets you answer the agent's questions at the terminal. Without
it, every pause is answered automatically with the `auto_feedback` string — which
is what you want for unattended batches, and what the benchmarks use.

The request file:

```yaml
prompt_template: >
  ...your request, ending with "...the following output directory:"
root_output_dir: /path/to/results/my_run    # each run gets a timestamped subfolder
auto_feedback: "good"                       # used when --interactive is off
n_runs: 1                                   # repeat the same request N times
max_wall_time_s: 15000                      # give up after this long
max_steps: 2000
max_interrupts: 20
```

`prompt_vars` can supply values for any `{placeholder}` you put in the template.
To run several phrasings of the same request in one go, replace `prompt_template`
with a `prompts:` mapping — see [benchmarks/README.md](../benchmarks/README.md).

## 3. Review the plan

**Nothing executes until you approve.** After roughly 20 seconds (or 60-90 s if
the request needs documentation retrieval), the agent shows a numbered plan: each
step, the tool it will call, and the inputs and outputs it expects.

Read it for the things a model gets wrong:

* Are the input paths the ones you meant?
* Is the genome right?
* Are all your samples there — and only your samples?
* Is any step missing, or present that you did not ask for?

Then answer:

| You want | Type |
|---|---|
| Run it | `good` (or just press Enter) |
| Change something | Say what, in plain English — *"use mm10, not hg38"*, *"drop the annotation step"*, *"only process C1 and C2"* |
| Stop | *Stop here* in the interface; Ctrl-C at the terminal |

Feedback goes back to the planner, which revises and shows you the plan again.
There is no limit on the number of rounds. A validator then checks the approved
plan for missing steps and unmet dependencies before execution starts.

## 4. Let it run

The executor walks the plan one step at a time. For each step it either calls a
tool from the [tool library](tools.md) or, when no tool fits, hands the step to
the autocode generator: write code → run it → check the output → debug → repeat.

The agent interrupts you twice more, at most:

* **If generated code keeps failing** — after five failed self-correction
  attempts it stops and asks. Reply to let it retry once more, optionally adding
  a note about what is going wrong. A second failure ends that step and the run
  moves on.
* **Figure review**, if `report_enabled: 'true'` — approve the figures or say
  what to adjust before the PDF is compiled.

Runtimes are dominated by the pipeline, not the agent: preprocessing a handful
of samples is hours of bowtie2, while the agent's own LLM calls are a few
minutes in total.

## 5. Collect the results

Everything lands under the output directory you gave, organised by step:

```
<output_dir>/
├── log/                             one log file per tool call
│
├── barcode/                         barcode FASTAs (Hiplex)
├── demux/<sample>/                  demultiplexed reads
├── trim/<sample>/                   adapter-trimmed reads
├── bam/<sample>/                    sorted, indexed, deduplicated alignments
├── chipdip_prep/splitbams/<sample>/ per-target BAMs (ChIP-DIP)
│
├── bed/<sample>/                    fragment-level BED
├── bedgraph/<sample>/               coverage tracks
├── bigwig/<sample>/                 browser tracks
│
├── count_matrix/<sample>/           *.feather region x CRF-pair counts
├── bicluster/<group>/               row_table.tsv, col_table.tsv, heatmap,
│                                    TFBS_enrichment/, genomic_distribution/
├── peak/<sample>/                   narrowPeak, pathway_annotation/,
│                                    genomic_distribution/, TFBS_enrichment/
├── peak/differential/<comparison>/  differential peaks between groups
├── differential/<comparison>/       differentially accessible regions
│                                    (*_all.tsv, *_sig.tsv, summary.tsv/pdf)
│
├── auto_code/                       every script the agent generated
├── summary/
│   ├── summary.py                   reproducible script for the whole session
│   ├── full_plan.txt                the plan that was executed
│   ├── input_file_path.txt          every input the run consumed
│   └── messages.json                the agent's full message history
├── report.pdf                       if report_enabled
├── stdout.log  stderr.log           the full transcript
└── summary.txt                      success, runtime, tokens (command-line runs)
```


`summary/summary.py` is what the replayer produces: the whole session condensed
into one script you can re-run without the agent. Together with the individual
scripts in `auto_code/` it is the record of what actually ran — keep it with the
results.

## Next

* [Web interface](interface.md) — the panels, and how to answer the agent
* [Tool library](tools.md) — what each tool does and what it needs
* [Troubleshooting](troubleshooting.md) — when a run does not go to plan

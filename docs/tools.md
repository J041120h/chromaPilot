# Tool library

Thirteen validated tools are registered with the agent. The planner reads their
full specification — inputs, defaults, outputs, and the sub-directory each writes
to — from [`chromapilot/Knowledge/node.txt`](../chromapilot/Knowledge/node.txt),
and their dependency order from
[`graph.txt`](../chromapilot/Knowledge/graph.txt). Those two files are the source
of truth; this page is a map.

Anything the tools do not cover goes to the **autocode generator** instead:
the agent retrieves relevant documentation, writes a script, runs it, checks the
output and debugs itself until the step succeeds. Generated scripts land in
`<out_dir>/auto_code/`.

## Shared parameters

Every tool takes the same three, so you only state them once in your request:

| | |
|---|---|
| `out_dir` | Root output directory for the whole analysis. Each tool creates its own sub-directory under it — never point this at a single file's destination. |
| `sample` | The sample being processed, for tools that work per sample. |
| `ref_genome` | `hg38` or `mm10` only. Saying "human" or "mouse" is enough; hg19/GRCh37 is rejected at planning time. |

Every tool also returns `log_out`, a log file under `<out_dir>/log/` named
`<sample>_<tool>.log`, or `<tool>.log` for tools that are not per-sample.

## The pipeline

### Hiplex CUT&Tag

```
create_barcode_fasta → demultiplex_reads → identify_adapter → align_reads
                                                                  │
                    ┌─────────────────────────────────────────────┤
                    ▼                                             ▼
             convert_bam_to_bed                          build_count_matrix
                    │                                             │
        ┌───────────┴───────────┐                    ┌────────────┴────────────┐
        ▼                       ▼                    ▼                         ▼
convert_bed_to_bedgraph  convert_bed_to_bigwig  biclustering_wrapper  differential_analysis
        │
        ▼
  peak_profiling → peak_differential_analysis
```

### ChIP-DIP

`chipdip_prep` replaces the four Hiplex preprocessing steps; everything
downstream is shared.

```
chipdip_prep ──┬──► convert_bam_to_bed ──┬──► convert_bed_to_bedgraph ──► peak_profiling
               │                         │                                     │
               │                         └──► convert_bed_to_bigwig            ▼
               └──► build_count_matrix ──► differential_analysis    peak_differential_analysis
```

## Preprocessing

| Tool | What it does |
|---|---|
| **create_barcode_fasta** | Turns a barcode spreadsheet (`.xlsx`/`.csv`/`.tsv`) into forward and reverse barcode FASTAs. Column names are matched case-insensitively (`target`/`crf`, `bc`/`barcode`/`sequence`/`index`); failing that, a constant-length A/C/G/T column is inferred. Not per-sample. → `barcode/` |
| **demultiplex_reads** | Splits paired-end FASTQ containing every barcode for a sample, drops empty barcode outputs, and merges symmetric barcode combinations (from bidirectional fragment orientation) into canonical pairs. → `demux/<sample>/` |
| **identify_adapter** | Finds the adapter structure at the 5′ end of each read against a user-defined linker, and trims. → `trim/<sample>/` |
| **align_reads** | Bowtie2 alignment, then duplicate removal matched to the library design — coordinate-based, CB-aware, UMI-aware, or CB+UMI-aware — producing coordinate-sorted, indexed BAMs. → `bam/<sample>/` |
| **chipdip_prep** | Runs the ChIP-DIP Snakemake workflow: barcode identification, DPM/BPM read separation, adapter trimming, Bowtie2 alignment, cluster-based splitting into per-target BAMs. → `chipdip_prep/splitbams/<sample>/` |

## Coverage and format conversion

| Tool | What it does |
|---|---|
| **convert_bam_to_bed** | Name-sorts BAM, converts paired-end alignments to BEDPE with bedtools, extracts valid fragment coordinates, writes chromosome-sorted fragment BED. → `bed/<sample>/` |
| **convert_bed_to_bedgraph** | `bedtools genomecov` over the fragment BED, giving genome-wide coverage. → `bedgraph/<sample>/` |
| **convert_bed_to_bigwig** | Fragment BED straight to bigWig for genome browsers, with optional RPM normalisation so tracks are comparable across samples. → `bigwig/<sample>/` |

## Quantification and downstream analysis

| Tool | What it does |
|---|---|
| **build_count_matrix** | Fragment × region count matrix from BAMs, over fixed bins or custom regions (BED / TSV / CSV), using proportional fragment overlaps. Written as Feather. → `count_matrix/<sample>/` |
| **biclustering_wrapper** | Biclusters regions against CRF pairs with k-means (`row_km` regions, `col_km` pairs), after optional region filtering (`top_pct`, `hvr` or `none`), then annotates with genomic distribution and TFBS enrichment. **Runs once per analysis group**, not per sample: pass every count matrix of a group together so the model learns shared patterns. Only invoked when you explicitly ask for biclustering. → `bicluster/<group>/` |
| **differential_analysis** | Differentially accessible regions between conditions, via a two-round limma-voom framework. Count matrices and condition labels must be in matching order. → `differential/<comparison>/` |
| **peak_profiling** | Calls peaks from bedGraph coverage — significant signal blocks scored against local background, written as narrowPeak — then optionally annotates: genomic distribution, rGREAT/MSigDB pathway enrichment, TFBS enrichment. → `peak/<sample>/` |
| **peak_differential_analysis** | Merges per-sample narrowPeak files into a consensus region set (minimum support threshold), counts BAM fragments over those regions, and runs limma-voom per condition pair. → `peak/differential/<comparison>/` |

## Implementation

Each tool is one module under `tools/`, wrapped as a LangChain `@tool` with an
`@auto_log_capture()` decorator that tees everything into the run's log
directory. The heavier statistical routines call R through
`tools/R_scripts/*_cli.R`, which front the
[multiEpiCore](https://github.com/Qingjie-Yu/multiEpiCore) package installed by
`setup.sh`.

`tools/chipdip_prep/` vendors the ChIP-DIP Snakemake workflow — see
[`NOTICE.md`](../tools/chipdip_prep/NOTICE.md) for attribution. The first run of
`chipdip_prep` builds that workflow's own conda environment under
`tools/chipdip_prep/env/` (several GB, one time, git-ignored).

### Modules present but not registered

`tools/` also contains `qc.py`, `calculate_mapping_rate.py`, `seacr_peak.py` and
`env_tools.py`. They are not imported by `chromapilot/agent.py` and the planner
cannot call them — `peak_profiling` supersedes `seacr_peak`. They are kept for
reference and for direct use from your own scripts.

## Adding a tool

1. Write `tools/my_tool.py` as a `@tool`-decorated function taking `out_dir` and,
   if per-sample, `sample`.
2. Import and register it in `chromapilot/agent.py` alongside the other
   `from tools... import` lines.
3. Add a numbered entry to `chromapilot/Knowledge/node.txt` following the exact
   layout of the existing ones — inputs with defaults, outputs, function
   summary, path convention. **This is what the planner reads**; a tool absent
   from `node.txt` will never be planned.
4. Add its edges to `chromapilot/Knowledge/graph.txt`.
5. Restart the web interface. The catalog and pipeline map pick the tool up
   automatically — no interface code changes.

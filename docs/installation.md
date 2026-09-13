# Installation

## Requirements

| | |
|---|---|
| **OS** | Linux x86-64. `environment.yaml` is a pinned `linux-64` export; other platforms need to relax the pins. |
| **conda** | Any recent conda or mamba. Mamba is much faster for an environment this size. |
| **Disk** | ~15 GB for the environment, ~4 GB per reference genome, plus your own data and results. |
| **Compute** | A real compute node. Only the LLM calls are remote — bowtie2, samtools, bedtools, cutadapt, picard and R all run locally. |
| **LLM key** | One key from Anthropic, OpenAI or Google. See [configuration.md](configuration.md). |

## Install

```bash
git clone https://github.com/J041120h/ChromaPilot.git
cd ChromaPilot
bash setup.sh
```

This creates a conda environment named `chromapilot`. To use a different name:

```bash
bash setup.sh my_env_name
```

Expect **about 30 minutes**, mostly conda solving and compiling R packages. If
the environment already exists, `setup.sh` asks before removing and recreating
it.

### What setup.sh does

1. Creates the conda environment from `environment.yaml` — Python 3.11, R 4.4,
   and the pipeline binaries (bowtie2 2.5.4, samtools 1.22, bedtools 2.31.1,
   cutadapt 5.1, picard 3.4.0, snakemake 9.3.3, `bedGraphToBigWig` 482).
2. Copies `config.example.yaml` to `config.yaml` and records the environment
   name in it.
3. Isolates R inside the environment (its own `R_LIBS_USER`, `.Renviron` and
   `.Rprofile`), so a system-wide R library cannot shadow the pinned packages.
4. Installs the Bioconductor packages: `preprocessCore` (built from source with
   threading disabled), `rGREAT`, the hg38/mm10 `TxDb` packages, and
   `org.Hs.eg.db` / `org.Mm.eg.db`.
5. Installs [multiEpiCore](https://github.com/Qingjie-Yu/multiEpiCore), the R
   package behind the count-matrix, biclustering, differential and peak tools.

### Add your API key

`setup.sh` leaves `config.yaml` with a placeholder key. Open it and set one:

```yaml
llm:
  provider: 'auto'                 # detected from the key prefix
  model: 'claude-sonnet-4-6'
  api_key: 'sk-ant-...'
```

`config.yaml` is in `.gitignore` — it never gets committed. Every configuration
field is documented in [configuration.md](configuration.md).

### Check the install

```bash
conda activate chromapilot
python -c "import langgraph, langchain, faiss; print('python deps OK')"
Rscript -e 'library(multiEpiCore); cat("R deps OK\n")'
for t in bowtie2 samtools bedtools cutadapt picard bedGraphToBigWig Rscript; do
    command -v $t >/dev/null && echo "  ok  $t" || echo "  MISSING  $t"
done
```

`chromapilot/interface/run_interface.sh` runs the same binary check at start-up
and names anything missing before you submit a request.

## Reference genomes

ChromaPilot supports **hg38** (GRCh38) and **mm10** (GRCm38). It does **not**
support hg19/GRCh37 — a request naming hg19 is rejected at planning time.

You do not download anything by hand. The first run that needs a genome fetches
it into `ref/` and verifies it against the MD5 checksums in `ref/ref.json`:

| What | Source | Size |
|---|---|---|
| Bowtie2 index | `genome-idx.s3.amazonaws.com` | ~3.5 GB per genome |
| Chromosome sizes | UCSC goldenPath | a few KB |
| Blacklist regions | ENCODE blacklist v2 | ~50 KB |

`ref/` is git-ignored apart from `ref.json`. To share one download across users,
point several checkouts at the same directory — see [ref/README.md](../ref/README.md).

The ChIP-DIP workflow additionally builds its own conda environment under
`tools/chipdip_prep/env/` the first time `chipdip_prep` runs. That is several GB
and happens once.

## Upgrading

```bash
git pull
conda env update -n chromapilot -f environment.yaml --prune
```

Your `config.yaml` and everything under `ref/` are untouched. If the tool catalog
(`chromapilot/Knowledge/node.txt`) changed, restart the web interface — it reads
the catalog at start-up.

## Uninstall

```bash
conda env remove -n chromapilot
rm -rf ChromaPilot          # includes ref/ and any results left inside it
```

# Reference genomes

This directory holds the reference data the pipeline needs. Everything in it
except `ref.json` and this file is **downloaded automatically** on first use and
is git-ignored.

## What gets downloaded

| Directory | Contents | Source | Size |
|---|---|---|---|
| `bowtie2_hg38/` | `GRCh38_noalt_as.*.bt2` | `genome-idx.s3.amazonaws.com` | ~3.5 GB |
| `bowtie2_mm10/` | `mm10.*.bt2` | `genome-idx.s3.amazonaws.com` | ~3.5 GB |
| `chromsize_hg38/` | `hg38.chrom.sizes` | UCSC goldenPath | a few KB |
| `chromsize_mm10/` | `mm10.chrom.sizes` | UCSC goldenPath | a few KB |
| `blacklist_hg38/`, `blacklist_mm10/` | `*-blacklist.v2.bed` | [Boyle-Lab/Blacklist](https://github.com/Boyle-Lab/Blacklist) v2 | ~50 KB each |

Only the genome a run actually asks for is fetched. `hg38` and `mm10` are the
only supported assemblies; hg19/GRCh37 is rejected at planning time.

## `ref.json`

MD5 checksums for every file above. `tools/ref_prep.py` verifies each download
against it and re-fetches anything that does not match, so a truncated download
repairs itself. This file **is** committed — do not delete it.

## Sharing one copy between users

The download is the same for everyone, so point several checkouts at one
directory rather than paying for it repeatedly:

```bash
rm -rf ref && ln -s /shared/path/to/ref ref
```

Make sure `ref.json` is present in the shared directory, and that everyone has
read access. `ref_prep` writes there when a genome is missing, so the first user
to need a new genome must also have write access.

## Downloading ahead of time

To pre-fetch instead of waiting during a run:

```bash
conda activate chromapilot
python -c "
from tools.ref_prep import ref_prep
r = ref_prep()
r.get_bowtie2_index('hg38')   # the big one: ~3.5 GB
r.get_chromsize('hg38')
r.get_blacklist('mm10')       # blacklist is only published for mm10 here
"
```

Each call downloads only what is missing and verifies it against `ref.json`, so
re-running is cheap and safe.

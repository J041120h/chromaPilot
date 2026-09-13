import os
import json
import shutil
from typing import List, Optional
from collections import defaultdict
import subprocess
from .ref_prep import ref_prep
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_files_path, get_cpu_core
import pysam

def bam_is_empty(p):
  with pysam.AlignmentFile(p, "rb", check_sq=False) as bam:
    for _ in bam.fetch(until_eof=True):
      return False
  return True

@tool
@auto_log_capture()
def chipdip_prep(
    samples: List[str],
    fastq_r1_files: List[str],
    fastq_r2_files: List[str],
    out_dir: str,
    ref_genome: str,
    barcode_config: str,
    dpm_fasta: str,
    bpm_fasta: str,
    barcode_format: Optional[str] = None,
    umi_length: int = 8,
):
  """Run the CHIP-DIP pipeline via Snakemake to process paired-end FASTQ files into per-target split BAM files.

  This tool wraps the CHIP-DIP Snakemake workflow, which performs the following steps:
  1) Split FASTQ files into chunks for parallel processing.
  2) Identify combinatorial barcodes using BarcodeIdentification.
  3) Separate DPM (genomic DNA) and BPM (bead oligo) reads.
  4) Trim adapters and align DPM reads to the reference genome via Bowtie2.
  5) Convert BPM reads to BAM and deduplicate.
  6) Assign cluster labels to reads and split into per-target BAM files..

  Args:
          samples: List of sample names. 
                        - Must be the same length as fastq_r1_files and fastq_r2_files, and positionally aligned: samples[i] corresponds to fastq_r1_files[i] and fastq_r2_files[i].
          fastq_r1_files: List of absolute or relative paths to R1 (forward) FASTQ.gz files, one per sample.
          fastq_r2_files: List of absolute or relative paths to R2 (reverse) FASTQ.gz files, one per sample.
          out_dir: Base output directory.
          ref_genome: Reference genome name used to obtain chromosome size information. Supported values: 'hg38' and 'mm10'.
          barcode_config: Path to the BarcodeIdentification config file defining barcode structures and target names.
          dpm_fasta: Path to the FASTA file containing DPM adapter sequences for cutadapt trimming.
          bpm_fasta: Path to the FASTA file containing bead oligo sequences for cutadapt trimming.
          barcode_format: Optional path to a barcode format file for validating barcode combinations. (default: None)
                          If None or file does not exist, barcode validation is skipped.
          umi_length: Length of the bead UMI sequence in BPM reads (default: 8).

  Returns:
        Dict: Dictionary containing:
            - "bam_dirs" (Dict[str, str] or None):
              Maps each sample name to the absolute path of its per-sample BAM directory `<out_dir>/chipdip_prep/splitbams/<sample>/`, containing per-target split BAM and BAI files. None if the pipeline fails.
            - 'log_out' (str): 
              Path to the log file containing all captured logs
  """
  # path check
  for p in fastq_r1_files:
    if not os.path.exists(p):
      print(f"[Error] Missing forward fastq.gz file {p}")
      return {"bam_dirs": None}
  for p in fastq_r2_files:
    if not os.path.exists(p):
      print(f"[Error] Missing reverse fastq.gz file {p}")
      return {"bam_dirs": None}
  for p in [barcode_config, dpm_fasta, bpm_fasta]:
    if not os.path.exists(p):
      print(f"[Error] Missing config file {p}")
      return {"bam_dirs": None}

  # Create output dir
  out_dir = os.path.abspath(out_dir)

  record_dir = os.path.join(out_dir, ".snakemake")
  result_dir = os.path.join(out_dir, "chipdip_prep")
  os.makedirs(result_dir, exist_ok=True)

  bam_root_dir = os.path.join(result_dir, "splitbams")
  if os.path.exists(bam_root_dir):
    shutil.rmtree(bam_root_dir)
  os.makedirs(bam_root_dir, exist_ok=True)

  # Create sample json
  if len(samples) != len(fastq_r1_files) or len(samples) != len(fastq_r2_files):
    print(f"[Error] Length of samples, fastq_r1_files, fastq_r2_files doesn't matched")
    return {"bam_dirs": None}
  
  fastqs = defaultdict(lambda: defaultdict(list))
  for s, r1, r2 in zip(samples, fastq_r1_files, fastq_r2_files):
    fastqs[s]["R1"].append(os.path.abspath(r1))
    fastqs[s]["R2"].append(os.path.abspath(r2))
  js = json.dumps(fastqs, indent=4, sort_keys=True)

  sample_json = os.path.join(out_dir, "samples.json")
  with open(sample_json, "w") as f:
    f.write(js)

  # Detect available cores
  threads = get_cpu_core()
  num_chunks = max(1, threads // 4)

  # Get Bowtie2 reference prefix
  ref_preparation = ref_prep()
  index_prefix = ref_preparation.get_bowtie2_index(ref_genome)
  blacklist_bed = ref_preparation.get_blacklist(ref_genome)

  # Get chipdip dir
  chipdip_dir = os.path.join(os.path.dirname(
      os.path.abspath(__file__)), "chipdip_prep")
  snakefile = os.path.join(chipdip_dir, "Snakefile")
  scripts_dir = os.path.join(chipdip_dir, "scripts")
  conda_prefix = os.path.join(chipdip_dir, "env")
  conda_env = os.path.join(chipdip_dir, "chipdip.yaml")

  # Build command
  cmd = [
      "snakemake", 
      "--directory", out_dir,
      "--snakefile", snakefile,
      "--use-conda",
      "--conda-prefix", conda_prefix,
      "--cores", str(threads),
      "--config",
      f"samples={os.path.abspath(sample_json)}",
      f"output_dir={result_dir}",
      f"bowtie2_index={os.path.abspath(index_prefix)}",
      f"num_chunks={str(int(num_chunks))}",
      f"barcode_config={os.path.abspath(barcode_config)}",
      f"cutadapt_dpm={os.path.abspath(dpm_fasta)}",
      f"cutadapt_oligos={os.path.abspath(bpm_fasta)}",
      f"bead_umi_length={str(int(umi_length))}",
      f"mask={os.path.abspath(blacklist_bed)}",
      f"scripts_dir={scripts_dir}",
      f"conda_env={conda_env}"
  ]

  if barcode_format and os.path.exists(barcode_format):
    cmd.append(f"barcode_format={os.path.abspath(barcode_format)}")

  # Run
  try:
    subprocess.run(cmd, check=True)
  except Exception as e:
    print(e)
    return {"bam_dirs": None}
  finally:
    if os.path.exists(record_dir):
      shutil.rmtree(record_dir)
  
  # Get output
  EXCLUDE_TARGETS = {'ambiguous', 'none', 'uncertain', 'filtered'}
  samples_sort = sorted(samples, key=len, reverse=True)
  bam_dirs = {}
  for p in get_files_path(bam_root_dir, ext=".bam"):
    b = os.path.basename(p)
    n = os.path.splitext(b)[0]
    sample = next((s for s in samples_sort if n.startswith(s + '.')), None)
    target = n[len(sample) + 1:]
    if target not in EXCLUDE_TARGETS:
      bam_dir = os.path.join(bam_root_dir, sample)
      bai_p = p + ".bai"
      new_bam = os.path.join(bam_dir, target + ".bam")
      new_bai = new_bam + ".bai"
      if bam_is_empty(p):
        os.remove(p)
        if os.path.exists(bai_p):
          os.remove(bai_p)
      else:
        if not os.path.isdir(bam_dir):
          os.makedirs(bam_dir)
          bam_dirs[sample] = bam_dir
        shutil.move(p, new_bam)
        shutil.move(bai_p, new_bai)

  return {"bam_dirs": bam_dirs if bam_dirs else None}


if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(
      description="Run the CHIP-DIP pipeline via Snakemake")
  parser.add_argument("--samples", required=True,
                      help="Comma-separated list of sample names")
  parser.add_argument("--fastq_r1_files", required=True,
                      help="Comma-separated list of paths to R1 (forward) FASTQ.gz files")
  parser.add_argument("--fastq_r2_files", required=True,
                      help="Comma-separated list of paths to R2 (reverse) FASTQ.gz files")
  parser.add_argument("--out_dir", required=True,
                      help="Base output directory")
  parser.add_argument("--ref_genome", required=True,
                      help="Reference genome name")
  parser.add_argument("--umi_length", type=int, default=8,
                      help="Length of the bead UMI sequence in BPM reads")
  parser.add_argument("--barcode_config", required=True,
                      help="Path to the BarcodeIdentification config file")
  parser.add_argument("--dpm_fasta", required=True,
                      help="Path to the FASTA file containing DPM adapter sequences")
  parser.add_argument("--bpm_fasta", required=True,
                      help="Path to the FASTA file containing bead oligo sequences")
  parser.add_argument("--barcode_format", default=None,
                      help="Optional path to a barcode format file for validating barcode combinations")
  args = parser.parse_args()

  res = chipdip_prep.invoke({
      "samples": args.samples.split(","),
      "fastq_r1_files": args.fastq_r1_files.split(","),
      "fastq_r2_files": args.fastq_r2_files.split(","),
      "out_dir": args.out_dir,
      "ref_genome": args.ref_genome,
      "barcode_config": args.barcode_config,
      "dpm_fasta": args.dpm_fasta,
      "bpm_fasta": args.bpm_fasta,
      "barcode_format": args.barcode_format,
      "umi_length": args.umi_length
  })
  pprint(res)

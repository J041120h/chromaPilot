import os
import shutil
import subprocess
from typing import List, Dict, Tuple, Optional
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_files_path, get_files_prefix


def count_mapped_reads(bam: str) -> int:
  proc = subprocess.check_output(["samtools", "idxstats", bam], text=True)
  total = 0
  for line in proc.splitlines():
    parts = line.split("\t")
    if len(parts) >= 3:
      try:
        total += int(parts[2])
      except ValueError:
        pass
  return total


def write_readcount_tsv(path: str, rows: List[Tuple[str, str, int]], target_col="pair", read_count_col="read_count") -> None:
  with open(path, "w", encoding="utf-8") as f:
    f.write(f"{target_col}\t{read_count_col}\n")
    for _, prefix, count in rows:
      f.write(f"{prefix}\t{count}\n")


@tool
@auto_log_capture()
def qc_by_percentile(
  bam_dir: str,
  sample: str,
  out_dir: str,
  percentile: float = 0.25,
  exclude: str = "unknown, IgG_control"
) -> Dict[str, Optional[str]]:
  """
  Perform read-count-based quality control on BAM files for a single sample using a percentile filtering strategy.

  For each BAM file belonging to the same sample, the function computes the number of mapped reads using `samtools idxstats`. 

  Before computing the percentile threshold, BAM files can be automatically excluded based on predefined names (e.g. "unknown", "IgG_control"). 

  The function outputs read-count summary tables and optionally generates a QC heatmap when BAM file prefixes 
  represent factor pairs (dash-separated naming).

  Args:
    bam_dir: Path to the BAM directory containing only QC-passed BAM files, where all unpassed files are moved to a separate subdirectory, or None if the QC process fails
    sample: Name of the single sample being processed
    out_dir: Base output directory
    percentile: Fraction (between 0 and 1) specifying the lower cutoff for read-count filtering (default: 0.25)
      - Example: percentile=0.25 removes approximately the lowest 25% of BAM files ranked by mapped read count
    exclude: Comma-separated string specifying FASTQ prefixes to exclude before adapter identification (default: "unknown, IgG_control")
      - For dash-separated pairs (e.g. "H3K27ac-IgG"), exclusion is triggered if any component matches
      - For simple names, exact matching is used
      - For mixed naming styles, substring matching is applied

  Returns:
    Dict: Dictionary containing:
      - 'bam_dir': Path to the BAM directory containing only QC-passed BAM files, where all unpassed files are moved to a separate subdirectory, or None if the QC process fails
      - 'all_read_count_path': Path to `all_read_count.tsv`, containing read counts for all non-excluded BAM files
      - 'filtered_read_count_path': Path to `filtered_read_count.tsv`, containing only BAM files that passed QC
      - 'qc_heatmap': Path to QC heatmap PDF when pair-style BAM prefixes are detected (optional)
      - 'log_out': Path to the log file capturing messages generated during execution

  Example:
    result = qc_by_percentile(
      bam_dir="/data/sample_001",
      sample="sample_001",
      out_dir="/path/to/output",
      percentile=0.25
    )

    # result will contain:
    # {
    #     'bam_dir':'/path/to/output/bam',
    #     'all_read_count_path': '/path/to/output/qc/sample_001/all_read_count.tsv',
    #     'filtered_read_count_path': '/path/to/output/qc/sample_001/filtered_read_count.tsv',
    #     'qc_heatmap': '/path/to/output/qc/sample_001/qc_heatmap.pdf'
    # }
  """
  # input validation
  if not (0.0 <= percentile <= 1.0):
    raise ValueError("percentile must be between 0 and 1")

  # create output dir
  qc_dir = os.path.join(out_dir, 'qc', sample)
  if os.path.exists(qc_dir):
    shutil.rmtree(qc_dir)
  os.makedirs(qc_dir, exist_ok=True)

  all_tsv = os.path.join(qc_dir, "all_read_count.tsv")
  filtered_tsv = os.path.join(qc_dir, "filtered_read_count.tsv")

  # build results
  result = {"bam_dir": None, "all_read_count_path": None, "filtered_read_count_path": None}

  # Detect naming mode & exclude
  bam_files = get_files_path(bam_dir, ext = ".bam")
  if not bam_files:
    print(f"No BAM files found in {bam_dir}")
    return result

  prefix_list = get_files_prefix(bam_files, '.bam')
  has_dash = ["-" in prefix for prefix in prefix_list]
  exclude = [s.strip() for s in exclude.split(",") if s.strip()]
  
  if all(has_dash):
    mode = "all_dash"
    should_exclude = [any(p in exclude for p in prefix.split("-"))
                      for prefix in prefix_list]
  elif not any(has_dash):
    mode = "no_dash"
    should_exclude = [prefix in exclude for prefix in prefix_list]
  else:
    mode = "mixed"
    should_exclude = [any(item in prefix for item in exclude)
                      for prefix in prefix_list]

  prefix_to_bam = {
      prefix: bam
      for prefix, bam, _exclude in zip(prefix_list, bam_files, should_exclude) if not _exclude
  }
  if not prefix_to_bam:
    print(f"All BAM files excluded for sample {sample}")
    return result

  try:
    # Get all read counts
    rows = [
        (bam, prefix, count_mapped_reads(bam))
        for prefix, bam in prefix_to_bam.items()
    ]
    rows = sorted(rows, key=lambda x: x[2], reverse=True)
    write_readcount_tsv(all_tsv, rows)

    # Filter read counts
    counts_sorted = sorted([c for _, _, c in rows])
    threshold = counts_sorted[int((len(counts_sorted)-1)*percentile)]
    filtered_rows = [(bam, prefix, c) for bam, prefix, c in rows if c > threshold]
    unpassed_rows = [(bam, prefix, c) for bam, prefix, c in rows if c <= threshold]
    write_readcount_tsv(filtered_tsv, filtered_rows)

    # Get passed/unpassed .bam files
    filtered_bam_files = [bam for bam, _, _ in filtered_rows]
    unpassed_bam_files = [bam for bam, _, _ in unpassed_rows]

    # Move unpassed .bam and associated files to subdir
    if unpassed_bam_files:
      unpass_dir = os.path.join(bam_dir, "unpassed")
      os.makedirs(unpass_dir, exist_ok=True)
      for bam in unpassed_bam_files:
        pair_name = os.path.splitext(os.path.basename(bam))[0]
        for f in os.listdir(bam_dir):
          if f.startswith(pair_name + "."):
            src = os.path.join(bam_dir, f)
            if os.path.isfile(src):
              shutil.move(src, os.path.join(unpass_dir, f))

  except Exception as e:
    print(e)
    return result

  result["bam_dir"] =  bam_dir
  result["all_read_count_path"] = all_tsv 
  result["filtered_read_count_path"] = filtered_tsv

  # Plot when `mode = all_dash`
  if mode != "all_dash":
    return result

  current_dir = os.path.dirname(os.path.abspath(__file__))
  r_script_path = os.path.join(
      current_dir, "R_scripts", "qc_visualization_cli.R")

  cmd = [
      "Rscript", r_script_path,
      "--all_read_count_path", os.path.abspath(all_tsv),
      "--filtered_read_count_path", os.path.abspath(filtered_tsv),
      "--out_dir", qc_dir,
      "--split_pair_by", "-"
  ]
  try:
    subprocess.run(cmd, check=True)
    result["qc_heatmap"] = os.path.join(qc_dir, "qc_heatmap.pdf")
  except Exception as e:
    print(e)

  return result


if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(
      description="Run qc_by_percentile on BAM files")
  parser.add_argument("--bam_dir", required=True,
                      help="Absolute path to the directory containing BAM files")
  parser.add_argument("--sample", required=True, help="Sample name")
  parser.add_argument("--out_dir", required=True,
                      help="Base output directory")
  parser.add_argument("--percentile", type=float, default=0.1,
                      help="Percentile threshold (default: 0.25)")
  parser.add_argument("--exclude", default="unknown,IgG_control",
                      help="Comma-separated sample names to exclude")
  args = parser.parse_args()

  res = qc_by_percentile.invoke({
    "bam_dir": args.bam_dir,
    "sample": args.sample,
    "out_dir": args.out_dir,
    "percentile": args.percentile,
    "exclude": args.exclude
  })
  pprint(res)

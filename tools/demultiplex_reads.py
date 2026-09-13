import os
import subprocess
import gzip
import shutil
import tempfile
from typing import Dict, List
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_files_path, get_cpu_core, drop_empty_files, get_fastq_prefix


def merge_symmetric_fastq(demux_files: List[str]) -> List[str]:
  unique_combs_dict = get_fastq_prefix(demux_files)
  unique_combs = set(unique_combs_dict.keys())

  print(f"\nGenerated {len(unique_combs)} barcode pairs")
  print("\nStarting file merging process...")
  buf = 8 * 1024 * 1024
  merged_fastq = []
  try:
    for comb in list(unique_combs):
      target1, target2 = comb.split("-")
      # reverse orientation counterpart of comb
      rev_comb = f"{target2}-{target1}"

      if target1 > target2:
        has_rev_comb = rev_comb in unique_combs
        r1 = unique_combs_dict[comb]['fwd']
        r2 = unique_combs_dict[comb]['rev']

        if has_rev_comb:
          rev_r1 = unique_combs_dict[rev_comb]["fwd"]
          rev_r2 = unique_combs_dict[rev_comb]["rev"]

          tmp1 = tempfile.NamedTemporaryFile(
              delete=False, dir=os.path.dirname(rev_r1), prefix=f"{rev_comb}_R1.tmp."
          )
          tmp2 = tempfile.NamedTemporaryFile(
              delete=False, dir=os.path.dirname(rev_r2), prefix=f"{rev_comb}_R2.tmp."
          )
          tmp1_path, tmp2_path = tmp1.name, tmp2.name
          tmp1.close()
          tmp2.close()

          with gzip.open(tmp1_path, "wb") as w:
            for src in (rev_r1, r1):
              with gzip.open(src, "rb") as f:
                shutil.copyfileobj(f, w, length=buf)
          with gzip.open(tmp2_path, "wb") as w:
            for src in (rev_r2, r2):
              with gzip.open(src, "rb") as f:
                shutil.copyfileobj(f, w, length=buf)
          os.replace(tmp1_path, rev_r1)
          os.replace(tmp2_path, rev_r2)
          os.remove(r1)
          os.remove(r2)

        else:
          new_r1 = os.path.join(os.path.dirname(r1), f"{rev_comb}_R1.fastq.gz")
          new_r2 = os.path.join(os.path.dirname(r2), f"{rev_comb}_R2.fastq.gz")
          os.replace(r1, new_r1)
          os.replace(r2, new_r2)
          merged_fastq.append(new_r1)
          merged_fastq.append(new_r2)
      else:
        merged_fastq.append(unique_combs_dict[comb]['fwd'])
        merged_fastq.append(unique_combs_dict[comb]['rev'])

    print("Symmetric FASTQ merge completed successfully!")
    return merged_fastq
  except Exception as e:
    print(f"Error: symmetric FASTQ merge failed with: {e}")
    return None


@tool
@auto_log_capture()
def demultiplex_reads(
    barcode_fwd_fasta: str,
    barcode_rev_fasta: str,
    sample: str,
    fastq_r1: str,
    fastq_r2: str,
    out_dir: str,
    error_rate: float = 0
) -> Dict:
  """Demultiplex paired-end FASTQ files for one sample using barcode FASTA files, then merge symmetric barcode combinations. This tool runs cutadapt to demultiplex one sample's paired-end reads into barcode-combination FASTQs and then merges symmetric barcode pairs produced by bidirectional fragment orientations.

  Symmetric merge logic (canonical comb is sorted by barcode name, e.g. A-B where A <= B):
  - Same-barcode pair (A-A): copy as-is.
  - Different-barcode pair (A-B, A != B):
      * If only A-B exists (B-A missing): copy A-B.
      * If only B-A exists (A-B missing): copy B-A into A-B output name.
      * If both exist: concatenate reads in order [A-B, B-A] to produce A-B outputs (for both R1 and R2).

  Args:
      barcode_fwd_fasta: Absolute path to FASTA file containing forward barcode sequences.
      barcode_rev_fasta: Absolute path to FASTA file containing reverse barcode sequences.
      sample: Name of the single sample being processed.
      fastq_r1: Absolute path to forward read FASTQ file (R1) for this sample (.fastq or .fastq.gz).
      fastq_r2: Absolute path to reverse read FASTQ file (R2) for this sample (.fastq or .fastq.gz).
      out_dir: Base output directory.
      error_rate: Mismatch rate allowed for barcode matching in cutadapt (default: 0).

  Returns:
      Dict with:
          - 'demux_dir': Absolute path to the directory containing demultiplexed FASTQ files after dropping empty outputs, or None if the process failed.
          - 'log_out': Path to the log file containing all messages generated during function execution

  Example:
      result = demultiplex_reads(
          barcode_fwd_fasta="/path/to/forward_barcodes.fasta",
          barcode_rev_fasta="/path/to/reverse_barcodes.fasta", 
          sample="sample_001",
          fastq_r1="/data/sample_001_R1.fastq.gz",
          fastq_r2="/data/sample_001_R2.fastq.gz",
          out_dir="/path/to/output"
      )
      # result will contain:
      # {
      #     'demux_dir': '/path/to/output/demux/sample_001',
      #     'log_out': '/path/to/output/log/sample_001_demux_reads.log'
      # }

  """

  fastq_dir = os.path.join(out_dir, 'demux', sample)
  if os.path.exists(fastq_dir):
    shutil.rmtree(fastq_dir)
  os.makedirs(fastq_dir, exist_ok=True)

  threads = get_cpu_core()

  try:
    cmd = [
        "cutadapt",
        "-e", str(error_rate),
        "-j", str(threads),
        "--no-indels",
        "--action", "trim",
        "-g", f"^file:{barcode_fwd_fasta}",
        "-G", f"^file:{barcode_rev_fasta}",
        "-o", os.path.join(fastq_dir, "{name1}-{name2}_R1.fastq.gz"),
        "-p", os.path.join(fastq_dir, "{name1}-{name2}_R2.fastq.gz"),
        fastq_r1,
        fastq_r2
    ]
    subprocess.run(cmd, check=True)
    print("Cutadapt execution successful")

    raw_fastq = drop_empty_files(get_files_path(fastq_dir, ext=".fastq.gz"))
    merged_fastq = merge_symmetric_fastq(raw_fastq)
    if merged_fastq is None:
      print("ERROR: Merging symmetric FASTQ files failed")
      return {'demux_dir': None}
    else:
      print("Demultiplexing completed successfully!")
      return {'demux_dir': fastq_dir}

  except Exception as e:
    print(f"ERROR: Demultiplexing failed with {e}")
    return {'demux_dir': None}


if __name__ == "__main__":
  import argparse
  from pprint import pprint
  parser = argparse.ArgumentParser(
      description="Demultiplex paired-end FASTQ files using barcode FASTA files")
  parser.add_argument("-1", "--fastq_r1", required=True)
  parser.add_argument("-2", "--fastq_r2", required=True)
  parser.add_argument("-s", "--sample", required=True)
  parser.add_argument("-o", "--out_dir", required=True)
  parser.add_argument("-f", "--barcode_fwd_fasta", required=True)
  parser.add_argument("-r", "--barcode_rev_fasta", required=True)
  parser.add_argument("-e", "--error_rate", type=float, default=0.0)

  args = parser.parse_args()

  res = demultiplex_reads.invoke({
      "barcode_fwd_fasta": args.barcode_fwd_fasta,
      "barcode_rev_fasta": args.barcode_rev_fasta,
      "sample": args.sample,
      "out_dir": args.out_dir,
      "fastq_r1": args.fastq_r1,
      "fastq_r2": args.fastq_r2,
      "error_rate": args.error_rate
  })
  pprint(res)

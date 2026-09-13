import os
import shlex
import shutil
import subprocess
from typing import Dict, List
from multiprocessing import Pool
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_cpu_core, get_files_path

def is_paired_end(bam):
  result = subprocess.run(
    ["samtools", "view", "-c", "-f", "1", bam],
    capture_output=True,
    text=True,
    check=True
  )
  return int(result.stdout.strip()) > 0

def bam_to_bed(bam, prefix, bed_dir):
  sort_bam = os.path.join(bed_dir, f"{prefix}.sort.bam")
  bedpe = os.path.join(bed_dir, f"{prefix}.bedpe")
  tmp_bed = os.path.join(bed_dir, f"{prefix}.tmp.bed")
  bed = os.path.join(bed_dir, f"{prefix}.bed")

  subprocess.run(
    ["samtools", "sort", "-n", bam, "-o", sort_bam],
    check=True
  )

  paired = is_paired_end(bam)

  if paired:
    # paired-end: name sort required
    subprocess.run(
      ["bash", "-c",
      f"bedtools bamtobed -i {shlex.quote(sort_bam)} -bedpe > {shlex.quote(bedpe)}"],
      check=True
    )
    subprocess.run(
      [
        "bash", "-c",
        f"""awk 'BEGIN{{OFS="\\t"}} NF>=6 && $1==$4 {{print $1,$2,$6}}' {shlex.quote(bedpe)} > {shlex.quote(tmp_bed)}"""
      ],
      check=True
    )
  else:
      # single-end: coordinate sort not required
      subprocess.run(
        [
          "bash", "-c",
          f"""bedtools bamtobed -i {shlex.quote(sort_bam)} | awk 'BEGIN{{OFS="\\t"}} {{print $1,$2,$3}}' > {shlex.quote(tmp_bed)}"""
        ],
        check=True
      )

  subprocess.run(
      ["sort", "-k1,1", "-k2,2n", tmp_bed, "-o", bed],
      check=True
  )

  # cleanup
  for p in [sort_bam, tmp_bed, bedpe]:
      if p != bam and os.path.exists(p):
          os.remove(p)

  return bed


@tool
@auto_log_capture()
def convert_bam_to_bed(
    bam_dir: str,
    sample: str,
    out_dir: str
) -> Dict:
  """Convert a single sample's BAM files to sorted BED format (not peak files) for genomic analysis.

  This tool processes BAM alignment files from one specific sample and converts them to BED format,
  which is more suitable for downstream genomic analyses. For multiple samples, call this function
  separately for each sample. The process involves:
  1. Sorting BAM files by read name
  2. Converting to BEDPE format using bedtools
  3. Extracting relevant columns to create standard BED format
  4. Sorting the final BED file by chromosome and position

  Args:
      bam_dir: Absolute path to the directory containing BAM files for this specific sample
      sample: Name of the single sample being processed
      out_dir: Base output directory where BED files will be saved

  Returns:
      Dict: Dictionary containing:
          - 'bed_dir': Absolute path to the directory containing sorted BED files converted from the input BAM files, or None if the conversion fails
          - 'log_out': Path to the log file containing all messages generated during function execution

  Example:
      result = convert_bam_to_bed(
          bam_dir="/bam",
          sample="sample_001",
          out_dir="/path/to/output"
      )
      # result will contain:
      # {
      #     'bed_dir': '/path/to/output/bed/sample_001',
      #     'log_out': '/path/to/output/log/sample_001_convert_bam_to_bed.log'
      # }

  Note:
      - Processes files for one sample per function call
      - Requires samtools and bedtools to be installed and accessible in PATH
      - Intermediate files (sorted BAM, BEDPE, unsorted BED) are automatically cleaned up
      - Final output files have '.bed' extension
  """

  bed_dir = os.path.join(out_dir, 'bed', sample)
  if os.path.exists(bed_dir):
    shutil.rmtree(bed_dir)
  os.makedirs(bed_dir, exist_ok=True)

  bam_files = get_files_path(bam_dir, ext=".bam")
  if not bam_files:
    print(f"No BAM files found in {bam_dir}")
    return {'bed_dir': None}
  
  threads = min(8, get_cpu_core())
  with Pool(processes=threads) as pool:
    async_results = []
    for bam in bam_files:
      prefix = os.path.basename(bam).removesuffix('.bam')
      ar = pool.apply_async(
          bam_to_bed, args=(bam, prefix, bed_dir))
      async_results.append((prefix, ar))

    bed_files = []
    for prefix, ar in async_results:
      try:
        bed_files.append(ar.get())
      except Exception as e:
        print(f"Failed: {prefix} -> {e}")
        pool.terminate()
        return {'bed_dir': None}
  print("Conversion from bam to bed completed successfully!")
  return {'bed_dir': bed_dir}


if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(
      description="Convert BAM files to bed format")
  parser.add_argument("--bam_dir", required=True,
                      help="Absolute path to the directory containing BAM files")
  parser.add_argument("--sample", required=True, help="Sample name")
  parser.add_argument("--out_dir", required=True, help="Base output directory")
  args = parser.parse_args()

  res = convert_bam_to_bed.invoke({
      "bam_dir": args.bam_dir,
      "sample": args.sample,
      "out_dir": args.out_dir
  })
  pprint(res)

import os
import shutil
import subprocess
from typing import Dict, Optional, List, Union
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_files_path

@tool
@auto_log_capture()
def build_count_matrix(
  bam_dir: str,
  sample: str,
  out_dir: str,
  ref_genome: str = "hg38",
  regions: Union[int, str] = 800,
  force_chr_coord: bool = False
) -> Dict:
  """Build fragment-overlap count matrix from paired-end BAM files.

  This tool processes paired-end sequencing BAM files to generate a count matrix quantifying 
  fragment overlaps with specified genomic regions. The process involves: 
  1. Reading paired-end fragments from BAM files 
  2. Defining genomic regions (either fixed-size bins or custom regions from a file) 
  3. Calculating proportional overlap between fragments and regions 
  4. Aggregating counts into a matrix (rows = regions, columns = samples) 
  5. Saving results in Feather format for efficient downstream analysis

  Args:
        bam_dir: Absolute path to the directory containing BAM files for this specific sample
        sample: Name of the sample being processed. Used as output filename prefix.
        out_dir: Base output directory where feather files will be saved.
        ref_genome: Reference genome assembly for tiling when regions is an integer. 
                    Options: "hg38" (human) or "mm10" (mouse).  (default: "hg38")
        regions: Genomic regions to quantify (default: 800). 
                 Can be either:
                  - Integer: Bin size in base pairs (e.g., 5000 for 5kb bins). The genome 
                      will be tiled into fixed-size non-overlapping bins. Standard chromosomesonly (excludes mitochondrial DNA).
                  - String: Absolute path to a single region file containing custom genomic regions. Supported formats:
                      * BED format (.bed): chr, start, end, [optional: gene_id]
                      * TSV/TXT format (.tsv/.txt): columns named seqnames, start, end, [gene_id]
                      * CSV format (.csv): same column requirements as TSV
                      BED coordinates are 0-based half-open; TSV/CSV are 1-based inclusive.
        force_chr_coord: If True, always use "CHR_start_end" format for region IDs,even if gene_id is available in custom regions. (default: False)

  Returns:
          Dict: Dictionary containing:
                - 'count_matrix_file': Absolute path to the generated Feather count matrix file if successful, or None if failed.
                - 'log_out': Path to the log file containing all messages generated during function execution

  Example:
          # For general, use fixed bin size:
          result = build_count_matrix(
                  bam_files=["data/A-A.bam", "data/A-B.bam"],
                  sample="C1",
                  out_dir="output",
                  regions=800,
                  ref_genome="hg38"
          )
          # Returns: {'count_matrix_file': 'output/count_matrix/C1/C1_Count_Matrix_800.feather', 'log_out': 'output/log/C1_build_count_matrix.log'}

          # If user is interested in specifc regions, use custom regions:
          result = build_count_matrix(
                  bam_files=["data/A-A.bam", "data/A-B.bam"],
                  sample="C2",
                  out_dir="output",
                  regions="data/promoters.bed"
          )
          # Returns: {'count_matrix_file': 'output/count_matrix/C2/C2_Count_Matrix_promoters.feather', 'log_out': 'output/log/C2_build_count_matrix.log'}
  """

  # Create output directory
  feather_dict = os.path.join(out_dir, 'count_matrix', sample)
  if os.path.exists(feather_dict):
    shutil.rmtree(feather_dict)
  os.makedirs(feather_dict, exist_ok=True)

  # Generate output filename
  if isinstance(regions, int):
    filename = f"Count_Matrix_{regions}"
  else:
    prefix = os.path.splitext(os.path.basename(regions))[0]
    filename = f"Count_Matrix_{prefix}"

  feather_filename = f"{sample}_{filename}.feather"
  feather_path = os.path.join(feather_dict, feather_filename)

  # Get R script path
  current_dir = os.path.dirname(os.path.abspath(__file__))
  r_script_path = os.path.join(
      current_dir, "R_scripts", "build_count_matrix_cli.R")

  # Convert bam_files to comma-separated string
  bam_files = get_files_path(bam_dir, ext=".bam")
  bam_paths_str = ",".join(bam_files)

  # Convert regions to string
  regions_str = str(regions)

  # Build command
  cmd = [
      "Rscript", r_script_path,
      "--bam_paths", bam_paths_str,
      "--regions", regions_str,
      "--out_dir", feather_dict,
      "--ref_genome", ref_genome,
      "--sample_name", sample
  ]

  if force_chr_coord:
    cmd.append("--force_chr_coord")

  try:
    subprocess.run(cmd, check=True, cwd=os.path.dirname(r_script_path))
    return {"count_matrix_file": feather_path}
  except Exception as e:
    print(e)
    return {"count_matrix_file": None}


if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(
      description="Build fragment-overlap count matrix from paired-end BAM files")
  parser.add_argument("--bam_files", required=True,
                      help="Comma-separated list of paths to input BAM files")
  parser.add_argument("--sample", required=True, help="Sample name")
  parser.add_argument("--out_dir", required=True,
                      help="Base output directory")
  parser.add_argument("--regions", required=True,
                      help="Bin size in base pairs (integer) or path to a custom regions file")
  parser.add_argument("--ref_genome", default="hg38",
                      help="Reference genome assembly (default: hg38)")
  parser.add_argument("--force_chr_coord", action="store_true",
                      default=False, help="Always use CHR_start_end format for region IDs")
  args = parser.parse_args()

  # regions can be an integer (bin size) or a string (file path)
  try:
    regions = int(args.regions)
  except ValueError:
    regions = args.regions

  res = build_count_matrix.invoke({
      "bam_files": args.bam_files.split(","),
      "sample": args.sample,
      "out_dir": args.out_dir,
      "regions": regions,
      "ref_genome": args.ref_genome,
      "force_chr_coord": args.force_chr_coord
  })
  pprint(res)

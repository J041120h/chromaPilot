import os
import shlex
import shutil
import subprocess
from typing import Dict, List
from langchain.tools import tool
from multiprocessing import Pool
from .ref_prep import ref_prep
from .LogCapture import auto_log_capture
from .utils import get_cpu_core, get_files_path


def bed_to_bigwig(bed, prefix, bw_dir, chrom_sizes, normalized=True):
  bg = os.path.join(bw_dir, f"{prefix}.bedGraph")
  sort_bg = os.path.join(bw_dir, f"{prefix}.sorted.bedGraph")
  bw = os.path.join(bw_dir, f"{prefix}.bw")

  with open(bed, "r") as f:
    total_reads = sum(1 for line in f if line.strip())
  if total_reads == 0:
    print(f"Empty BED file: {bed}")
    return None

  scale_opt = ""
  if normalized:
    scale_factor = 1e6 / total_reads
    scale_opt = f"-scale {scale_factor}"

  subprocess.run(
      [
          "bash", "-c",
          f"bedtools genomecov -bg {scale_opt} -i {shlex.quote(bed)} -g {shlex.quote(chrom_sizes)} > {shlex.quote(bg)}"
      ],
      check=True
  )
  subprocess.run(
      [
          "bash", "-c",
          f"LC_ALL=C sort -k1,1 -k2,2n {shlex.quote(bg)} > {shlex.quote(sort_bg)}"
      ],
      check=True
  )
  subprocess.run(
      ["bedGraphToBigWig", sort_bg, chrom_sizes, bw],
      check=True
  )

  for p in [bg, sort_bg]:
    if os.path.exists(p):
      os.remove(p)
  return bw


@tool
@auto_log_capture()
def convert_bed_to_bigwig(
  bed_dir: str,
  sample: str,
  out_dir: str,
  ref_genome: str,
  normalized: bool = True
) -> Dict:
  """
  Convert BED files directly to bigWig signal tracks.

  This function generates bigWig files intended for genome browser
  visualization and cross-sample comparison.

  Args:
      bed_dir: Absolute path to the directory containing BED files for this specific sample
      sample: Sample identifier used for output directory organization.
      out_dir: Base output directory.
      ref_genome: Reference genome name used to obtain chromosome size information. Supported values: 'hg38' and 'mm10'.
      normalized: Whether to apply RPM normalization (1e6 / total_reads). Default: True.

  Returns:
      Dict with:
          - 'bigwig_dir': Absolute path to the directory containing generated bigWig files, or None if the conversion fails
          - 'log_out': absolute path to the execution log file
  """

  # output directory
  bw_dir = os.path.join(out_dir, "bigwig", sample)
  if os.path.exists(bw_dir):
    shutil.rmtree(bw_dir)
  os.makedirs(bw_dir, exist_ok=True)

  # Get chromsizes
  ref_preparation = ref_prep()
  chrom_sizes = ref_preparation.get_chromsize(ref_genome)

  # Get bed files
  bed_files = get_files_path(bed_dir, ext = ".bed")
  if not bed_files:
    print(f"No BED files found in {bed_dir}")
    return {'bigwig_dir': None}
  
  # Run in parallel
  threads = min(8, get_cpu_core())
  with Pool(processes=threads) as pool:
    async_results = []
    for bed in bed_files:
      prefix = os.path.basename(bed).removesuffix('.bed')
      ar = pool.apply_async(bed_to_bigwig, args=(bed, prefix, bw_dir, chrom_sizes, normalized))
      async_results.append((prefix, ar))

    bw_list = []
    for prefix, ar in async_results:
      try:
        bw_list.append(ar.get())
      except Exception as e:
        print(f"Failed: {prefix} -> {e}")
        pool.terminate()
        return {'bigwig_dir': None}
  print("Conversion from bed to bigwig completed successfully!")
  return {'bigwig_dir': bw_dir}


if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(
      description="Convert BED files to bigwig format")
  parser.add_argument("--bed_dir", required=True,
                      help="Absolute path to the directory containing BED files")
  parser.add_argument("--sample", required=True, help="Sample name")
  parser.add_argument("--ref_genome", default="hg38",
                      help="Reference genome assembly (default: hg38)")
  parser.add_argument("--out_dir", required=True,
                      help="Base output directory")
  parser.add_argument(
      "--no-normalized",
      dest="normalized",
      action="store_false",
      default=True,
      help="Disable RPM normalization"
  )
  args = parser.parse_args()

  res = convert_bed_to_bigwig.invoke({
      "bed_dir": args.bed_dir,
      "ref_genome": args.ref_genome,
      "sample": args.sample,
      "out_dir": args.out_dir,
      "normalized": args.normalized,
  })
  pprint(res)

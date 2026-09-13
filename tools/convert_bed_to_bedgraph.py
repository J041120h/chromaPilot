import os
import shlex
import shutil
import subprocess
from typing import Dict, List
from multiprocessing import Pool
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .ref_prep import ref_prep
from .utils import get_cpu_core, get_files_path


def bed_to_bedgraph(bed, prefix, bg_dir, chrom_sizes):
  bg = os.path.join(bg_dir, f"{prefix}.bedGraph")
  subprocess.run(
    ["bash", "-c",
      f"bedtools genomecov -bg -i {shlex.quote(bed)} -g {shlex.quote(chrom_sizes)} > {shlex.quote(bg)}"],
    check=True
  )
  return bg


@tool
@auto_log_capture()
def convert_bed_to_bedgraph(
  bed_dir: str,
  sample: str,
  out_dir: str,
  ref_genome: str,
) -> Dict:
  """Convert BED files to bedGraph format for genome coverage visualization.

  This tool uses bedtools genomecov to convert BED files into bedGraph format,
  which represents genome coverage data. The bedGraph format is suitable for
  visualization in genome browsers and downstream analysis of coverage patterns.
  For multiple samples, call this function separately for each sample.

  Args:
      bed_dir: Absolute path to the directory containing BED files for this specific sample
      sample: Sample name used to create output directory structure
      out_dir: Base output directory where bedGraph files will be saved
      ref_genome: Reference genome name used to obtain chromosome size information. Supported values: 'hg38' and 'mm10'.

  Returns:
      Dict: Dictionary containing:
          - 'bedgraph_dir': Absolute path to the directory containing generated bedGraph files, or None if the conversion fails
          - 'log_out': Path to the log file containing all messages generated during function execution

  Example:
      result = convert_bed_to_bedgraph(
              bed_dir="/path/to/output/bed/sample_001",
              ref_genome="hg38",
              sample="sample_001",
              out_dir="/path/to/output"
      )
      # result will contain:
      # {
      #     'bedgraph_dir': '/path/to/output/bedgraph/sample_001',
      #     'log_out': '/path/to/output/log/sample_001_convert_bed_to_bedgraph.log'
      # }
  """

  bedgraph_dir = os.path.join(out_dir, 'bedgraph', sample)
  if os.path.exists(bedgraph_dir):
    shutil.rmtree(bedgraph_dir)
  os.makedirs(bedgraph_dir, exist_ok=True)

  ref_preparation = ref_prep()
  chrom_sizes = ref_preparation.get_chromsize(ref_genome)

  bed_files = get_files_path(bed_dir, ext = ".bed")
  if not bed_files:
    print(f"No BED files found in {bed_dir}")
    return {'bedgraph_dir': None}

  threads = min(8, get_cpu_core())
  with Pool(processes=threads) as pool:
    async_results = []
    for bed in bed_files:
      prefix = os.path.basename(bed).removesuffix('.bed')
      ar = pool.apply_async(bed_to_bedgraph, args=(bed, prefix, bedgraph_dir, chrom_sizes))
      async_results.append((prefix, ar))

    bedgraph_list = []
    for prefix, ar in async_results:
      try:
        bedgraph_list.append(ar.get())
      except Exception as e:
        print(f"Failed: {prefix} -> {e}")
        pool.terminate()
        return {'bedgraph_dir': None}
  print("Conversion from bed to bedgraph completed successfully!")
  return {'bedgraph_dir': bedgraph_dir}


if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(
      description="Convert BED files to bedgraph format")
  parser.add_argument("--bed_dir", required=True,
                      help="Absolute path to the directory containing BED files")
  parser.add_argument("--sample", required=True, help="Sample name")
  parser.add_argument("--ref_genome", default="hg38",
                      help="Reference genome assembly (default: hg38)")
  parser.add_argument("--out_dir", required=True, help="Base output directory")
  args = parser.parse_args()

  res = convert_bed_to_bedgraph.invoke({
      "bed_dir": args.bed_dir,
      "ref_genome": args.ref_genome,
      "sample": args.sample,
      "out_dir": args.out_dir
  })
  pprint(res)

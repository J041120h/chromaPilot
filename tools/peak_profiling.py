import os
import glob
import shutil
import subprocess
from typing import Dict, List
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_files_path, get_files_prefix

@tool
@auto_log_capture()
def peak_profiling(
    bedgraph_dir: str,
    sample: str, 
    out_dir: str,
    ref_genome: str = "hg38",
    min_cov: float = 2.0,
    auc_top_pct: float = 0.1,
    qvalue_cutoff: float = 0.05,
    fc_cutoff: float = 2.0,
    no_annotation: bool = False,
    no_plot: bool = False,
) -> Dict:
  """Perform peak calling and optional annotation from bedGraph files.

  This tool runs a two-step pipeline:
  1. Peak calling: identifies significant signal blocks from bedGraph coverage
      files and writes results as narrowPeak files.
  2. (Optional) Annotation: summarizes genomic distribution of called peaks,
      runs rGREAT-based MSigDB pathway enrichment, and performs TFBS enrichment analysis.

  Args:
      bedgraph_dir: Absolute path to the directory containing bedGraph files for the specific sample.
      sample: Name of the sample being processed. Used as output filename prefix.
      out_dir: Base output directory where results will be saved.
      ref_genome: Reference genome assembly. Options: "hg38" or "mm10". (default: "hg38")
      min_cov: Minimum mean coverage (auc/length) pre-filter applied to
          candidate peak blocks before statistical testing. (default: 2.0)
      auc_top_pct: Top percentile of blocks by AUC retained before statistical
          testing. Must be in (0, 1]. (default: 0.1)
      qvalue_cutoff: BH-adjusted q-value threshold for peak filtering. (default: 0.05)
      fc_cutoff: Fold-change threshold for peak filtering. (default: 2.0)
      no_annotation: If True, disable annotation steps. (default: False) 
      no_plot: If True, suppress all plot outputs (heatmaps and summary bar charts). (default: False)

  Returns:
      Dict: A dictionary of output paths.

        Always included:
            - "peak_dir" (str):
                Absolute path to the directory `<out_dir>/peak/<sample>/`, containing one narrowPeak file per input bedGraph file with names `{bedgraph_prefix}_peaks.narrowPeak`.

            - "log_out" (str):
                Path to the log file containing all messages generated during function execution.

        Included only when `no_annotation` is False and `no_plot` is False:
            - "TFBS_enrichment_heatmap" (str):
                Absolute path to the sample-level TFBS enrichment heatmap PDF under `<out_dir>/peak/<sample>/TFBS_enrichment/`.
            - "pathway_annotation_bubbleplot" (str):
                Absolute path to the sample-level pathway enrichment bubble plot PDF under `<out_dir>/peak/<sample>/pathway_annotation/`.
            - "distribution_plots" (List[str]):
                List of genomic distribution plot PDFs found under `<out_dir>/peak/<sample>/genomic_distribution/`.
  """
  # Create output directory
  peak_dir = os.path.join(out_dir, 'peak', sample)
  if os.path.exists(peak_dir):
    shutil.rmtree(peak_dir)
  os.makedirs(peak_dir, exist_ok=True)

  # Build result dict upfront
  result = {}
  result["peak_dir"] = None

  # Get bedgraph files
  bg_ext = [".bedGraph", ".bedgraph", ".bg", ".bdg"]
  bedgraph_files = get_files_path(bedgraph_dir, ext = bg_ext)
  if not bedgraph_files:
    print(f"No bedGraph files found in: {bedgraph_dir}")
    return result
  
  prefix_list = get_files_prefix(bedgraph_files, ext=bg_ext)
  if prefix_list is None:
     print(f"Failed to extract prefixes from bedgraph files in: {bedgraph_dir}")
     return result
  
  # Run R script
  current_dir = os.path.dirname(os.path.abspath(__file__))
  r_script_path = os.path.join(current_dir, "R_scripts", "peak_profiling_cli.R")

  cmd = [
      "Rscript", r_script_path,
      "--bedgraph_paths", ",".join(bedgraph_files),
      "--out_dir", peak_dir,
      "--ref_genome", ref_genome,
      "--min_cov", str(min_cov),
      "--auc_top_pct", str(auc_top_pct),
      "--qvalue_cutoff", str(qvalue_cutoff),
      "--fc_cutoff", str(fc_cutoff),
  ]
  if no_annotation:
      cmd.append("--no_annotation")
  if no_plot:
      cmd.append("--no_plot")

  try:
    subprocess.run(cmd, check=True, cwd=os.path.dirname(r_script_path))
    result["peak_dir"] = peak_dir
    if not no_annotation and not no_plot:
        result["pathway_annotation_bubbleplot"] = os.path.join(peak_dir, "pathway_annotation", f"pathway_annotation.pdf")
        result["TFBS_enrichment_heatmap"] = os.path.join(peak_dir, "TFBS_enrichment", f"TFBS_enrichment.pdf")
        dist_dir = os.path.join(peak_dir, "genomic_distribution")
        result["distribution_plots"] = sorted(glob.glob(os.path.join(dist_dir, "*.pdf")))
    return result
  
  except Exception as e:
    print(e)
    for key in result:
        result[key] = None
    return result

if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(description="Peak calling and pathway annotation")
  parser.add_argument("--bedgraph_dir", required=True,
                      help="Absolute path to the directory containing bedGraph files")
  parser.add_argument("--sample", required=True, help="Sample name")
  parser.add_argument("--out_dir", required=True, help="Output directory")
  parser.add_argument("--ref_genome", default="hg38")
  parser.add_argument("--min_cov", type=float, default=2.0)
  parser.add_argument("--auc_top_pct", type=float, default=0.1)
  parser.add_argument("--qvalue_cutoff", type=float, default=0.05)
  parser.add_argument("--fc_cutoff", type=float, default=2.0)
  parser.add_argument("--no_annotation", action="store_true", default=False)
  parser.add_argument("--no_plot", action="store_true", default=False)
  args = parser.parse_args()

  res = peak_profiling.invoke({
      "bedgraph_dir": args.bedgraph_dir,
      "sample": args.sample,
      "out_dir": args.out_dir,
      "ref_genome": args.ref_genome,
      "min_cov": args.min_cov,
      "auc_top_pct": args.auc_top_pct,
      "qvalue_cutoff": args.qvalue_cutoff,
      "fc_cutoff": args.fc_cutoff,
      "no_annotation": args.no_annotation,
      "no_plot": args.no_plot,
  })
  pprint(res)
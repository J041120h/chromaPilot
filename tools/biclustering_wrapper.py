import os
import glob
import subprocess
from typing import Dict, List
from langchain.tools import tool
from .LogCapture import auto_log_capture

@tool
@auto_log_capture()
def biclustering_wrapper(
  count_matrix_files: List[str],
  out_dir: str,
  group_tag: str,
  ref_genome: str = "hg38",
  filter_method: str = "top_pct",
  no_annotation: bool = False,
  no_plot: bool = False,
  row_km: int = 15,
  col_km: int = 4
) -> Dict:
  """Perform biclustering analysis on count matrices.

  This tool performs biclustering analysis on count matrices, including optional filtering,
  k-means clustering, and gene annotation of genomic regions. The process involves:
  1. Load one or more count matrices in `.feather` format (first column must be `pos`).
        If multiple matrices are provided, they are merged before downstream processing.
  2. (Optional) Filter for HVR (recommended for genome-wide equal-sized bins; usually
        unnecessary for user-specified targeted intervals).
  3. Run biclustering (k-means) on rows (genomic regions) and columns (features such as CRF pairs).
  4. (Optional) Downstream annotation on biclustered regions:
            - Genomic distribution summaries (e.g., genic / cCRE / CpG / repeat)
            - TFBS enrichment analysis
  5. (Optional) Generate plots/figures for diagnostics and publication-ready outputs

  Args:
      count_matrix_files: List of bsolute path(s) to input count matrix files (.feather).
      out_dir: Base output directory where results will be saved.
      group_tag: Name of this group of input samples, or the sample name if only one sample is provided. Used as a subdirectory name under `out_dir/bicluster/` to organize outputs per group/sample. (required)
      ref_genome: Reference genome assembly. Options: "hg38" or "mm10". (default: "hg38")
      filter_method: Region filtering strategy applied before biclustering. (default: "top_pct")
                - Must be one of: "top_pct", "hvr", "none"
                - "top_pct": percentile-based union filtering across pairs (recommended default).
                - "hvr": highly-variable-region filtering.
                - "none": skip filtering (recommended when using user-supplied specific genomic intervals).
      no_annotation: If True, disable annotation steps. (default: False) 
                - Set to False (apply annotation) when the count matrix was built from genome bins.
                - Set to True (skip annotation) when the count matrix was built from user-supplied specific genomic intervals.
      no_plot: If True, disable plotting. (default: False) 
      row_km: Number of k-means clusters for rows (regions). (default: 15) 
      col_km: Number of k-means clusters for columns (CRF pairs). (default: 4)

  Returns:
      Dict: A dictionary of output paths. Keys included depend on `no_annotation` and `no_plot`.

        Always included
                - "region_cluster" (str):
                        Path to `<out_dir>/bicluster/<group_tag>/row_table.tsv`, region-to-row-cluster assignments.
                        Columns: region (`chr_start_end`), cluster label (A, B, C, ...).
                - "pair_cluster" (str):
                        Path to `<out_dir>/bicluster/<group_tag>/col_table.tsv`, pair-to-col-cluster assignments.
                        Columns: pair name, cluster label (1, 2, 3, ...).
                - 'log_out':
                        Path to the log file containing all captured logs

        Included only when `no_plot` is False
                - "heatmap" (str):
                        Path to `<out_dir>/bicluster/<group_tag>/biclustering_heatmap.pdf`.

        Included only when `no_annotation` is False AND `no_plot` is False
                - "TFBS_enrichment_heatmap" (str):
                        Path to `<out_dir>/bicluster/<group_tag>/TFBS_enrichment/TFBS_enrichment.pdf`.
                - "distribution_plots" (List[str]):
                        List of stacked barplot PDFs found under `<out_dir>/bicluster/<group_tag>/genomic_distribution`, one per genomic annotation layer
  """

  # Create output directory
  bicluster_dir = os.path.join(out_dir, 'bicluster', group_tag)
  os.makedirs(bicluster_dir, exist_ok=True)

  # Build result dict upfront
  result = {'region_cluster': None, 'pair_cluster': None}

  # Get R script path
  current_dir = os.path.dirname(os.path.abspath(__file__))
  r_script_path = os.path.join(
      current_dir, "R_scripts", "biclustering_wrapper_cli.R")

  # Convert cm_paths to comma-separated string
  cm_paths_str = ",".join(count_matrix_files)

  # Build command
  cmd = [
      "Rscript", r_script_path,
      "--cm_paths", cm_paths_str,
      "--out_dir", bicluster_dir,
      "--ref_genome", ref_genome,
      "--filter_method", filter_method,
      "--row_km", str(row_km),
      "--col_km", str(col_km)
  ]

  if no_annotation:
    cmd.append("--no_annotation")
  if no_plot:
    cmd.append("--no_plot")

  try:
    subprocess.run(cmd, check=True, cwd=os.path.dirname(r_script_path))
  except Exception as e:
    print(e)
    return result

  # Add annotation outputs to result if applicable
  try:
    result['region_cluster'] = os.path.join(bicluster_dir, 'row_table.tsv')
    result['pair_cluster'] = os.path.join(bicluster_dir, 'col_table.tsv')
    if not no_plot:
      hm = os.path.join(bicluster_dir, 'biclustering_heatmap.pdf')
      result['heatmap'] = hm

      if not no_annotation:
        result['TFBS_enrichment_heatmap'] = os.path.join(bicluster_dir, "TFBS_enrichment", "TFBS_enrichment.pdf")
        dist_dir = os.path.join(bicluster_dir, "genomic_distribution")
        result['distribution_plots'] = sorted(glob.glob(os.path.join(dist_dir, "*_distribution.pdf")))
        
    return result
  except Exception as e:
    print(e)
    return result

if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(
      description="Perform biclustering analysis on count matrices")
  parser.add_argument("--count_matrix_files", required=True,
                      help="Comma-separated list of paths to input count matrix files (.feather)")
  parser.add_argument("--out_dir", required=True,
                      help="Base output directory")
  parser.add_argument("--group_tag", default="",
                      help="Name of this group of input samples, or the sample name if only one sample (default: '')")
  parser.add_argument("--filter_method", default="top_pct",
                      choices=["top_pct", "hvr", "none"],
                      help="Region filtering method: top_pct, hvr, or none (default: top_pct)")
  parser.add_argument("--no_annotation", action="store_true",
                      default=False, help="Disable annotation steps")
  parser.add_argument("--no_plot", action="store_true",
                      default=False, help="Disable plotting")
  parser.add_argument("--ref_genome", default="hg38",
                      help="Reference genome assembly (default: hg38)")
  parser.add_argument("--row_km", type=int, default=15,
                      help="Number of k-means clusters for rows (default: 15)")
  parser.add_argument("--col_km", type=int, default=4,
                      help="Number of k-means clusters for columns (default: 4)")
  args = parser.parse_args()

  res = biclustering_wrapper.invoke({
      "count_matrix_files": args.count_matrix_files.split(","),
      "group_tag": args.group_tag,
      "out_dir": args.out_dir,
      "filter_method": args.filter_method,
      "no_annotation": args.no_annotation,
      "no_plot": args.no_plot,
      "ref_genome": args.ref_genome,
      "row_km": args.row_km,
      "col_km": args.col_km
  })
  pprint(res)

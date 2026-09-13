#!/usr/bin/env Rscript
# CLI for differential analysis module
suppressPackageStartupMessages({
  library(argparse)
  library(multiEpiCore)
})

parser <- ArgumentParser(
  prog = "differential_analysis_cli.R",
  description = "Differential accessibility analysis from per-sample count matrices"
)

# Required arguments
parser$add_argument("--cm_paths", required = TRUE,
                    help = "Comma-separated paths to per-sample .feather count matrix files")
parser$add_argument("--conditions", required = TRUE,
                    help = "Comma-separated condition labels, one per sample (same order as --cm_paths)")
parser$add_argument("--out_dir", required = TRUE,
                    help = "Root output directory for all results")

# Optional arguments
parser$add_argument("--sample_names", default = NULL,
                    help = "Comma-separated sample names (optional); must be unique and same length as --conditions")
parser$add_argument("--col_cluster_file_path", default = NULL,
                    help = "Path to TSV file mapping CRF pairs to column clusters (pair, cluster columns)")
parser$add_argument("--ref_genome", default = "hg38",
                    help = "Reference genome: hg38 or mm10 (default: hg38)")
parser$add_argument("--min_support", type = "integer", default = 2,
                    help = "Minimum number of samples a peak must appear in (default: 2)")
parser$add_argument("--lfc_threshold", type = "double", default = 1.0,
                    help = "Log2 fold-change threshold (default: 1.0)")
parser$add_argument("--p_threshold", type = "double", default = 0.05,
                    help = "P-value threshold (default: 0.05)")
parser$add_argument("--p_type", default = "fdr",
                    help = "P-value type: fdr, nominal, or bonferroni (default: fdr)")
parser$add_argument("--no_annotation", action = "store_true", default = FALSE,
                    help = "Skip GREAT-based pathway enrichment annotation")
parser$add_argument("--no_plot", action = "store_true", default = FALSE,
                    help = "Suppress all plot outputs (heatmaps and bubble plots)")

args <- parser$parse_args()

tryCatch({

  cm_paths     <- trimws(strsplit(args$cm_paths, ",")[[1]])
  conditions   <- trimws(strsplit(args$conditions, ",")[[1]])
  sample_names <- if (!is.null(args$sample_names)) trimws(strsplit(args$sample_names, ",")[[1]]) else NULL

  differential_analysis(
    cm_path               = cm_paths,
    conditions            = conditions,
    sample_names          = sample_names,
    out_dir               = args$out_dir,
    col_cluster_file_path = args$col_cluster_file_path,
    ref_genome            = args$ref_genome,
    min_support   = args$min_support,
    lfc_threshold = args$lfc_threshold,
    p_threshold   = args$p_threshold,
    p_type        = args$p_type,
    apply_annotation      = !args$no_annotation,
    plot                  = !args$no_plot
  )

}, error = function(e) {
  message("ERROR: ", conditionMessage(e))
  quit(status = 1)
})
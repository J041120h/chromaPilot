#!/usr/bin/env Rscript
suppressPackageStartupMessages({
  library(argparse)
  library(multiEpiCore)
})

parser <- ArgumentParser(
  prog = "peak_profiling_cli.R",
  description = "Peak calling and optional pathway annotation from bedGraph files"
)

parser$add_argument("--bedgraph_paths", required = TRUE,
                    help = "Comma-separated path(s) to input bedGraph files")
parser$add_argument("--out_dir", required = TRUE,
                    help = "Output directory")

parser$add_argument("--ref_genome", default = "hg38",
                    help = "Reference genome: hg38 or mm10 (default: hg38)")
parser$add_argument("--min_cov", type = "double", default = 2,
                    help = "Minimum mean coverage pre-filter for candidate peak blocks (default: 2)")
parser$add_argument("--auc_top_pct", type = "double", default = 0.1,
                    help = "AUC top percentile pre-filter (default: 0.1)")
parser$add_argument("--qvalue_cutoff", type = "double", default = 0.05,
                    help = "Q-value cutoff for peak filtering (default: 0.05)")
parser$add_argument("--fc_cutoff", type = "double", default = 2,
                    help = "Fold-change cutoff for peak filtering (default: 2)")

parser$add_argument("--no_annotation", action = "store_true", default = FALSE,
                    help = "Disable all downstream annotation (sets apply_annotation=FALSE)")
parser$add_argument("--no_plot", action = "store_true", default = FALSE,
                    help = "Disable plotting (sets plot=FALSE)")

args <- parser$parse_args()

tryCatch({
  bedgraph_paths <- trimws(strsplit(args$bedgraph_paths, ",")[[1]])
  apply_annotation <- !isTRUE(args$no_annotation)
  plot <- !isTRUE(args$no_plot)

  peak_profiling(
    bedgraph_path    = bedgraph_paths,
    out_dir          = args$out_dir,
    ref_genome       = args$ref_genome,
    min_cov          = args$min_cov,
    auc_top_pct      = args$auc_top_pct,
    qvalue_cutoff    = args$qvalue_cutoff,
    fc_cutoff        = args$fc_cutoff,
    apply_annotation = apply_annotation,
    plot             = plot
  )
}, error = function(e) {
  message("ERROR: ", conditionMessage(e))
  quit(status = 1)
})
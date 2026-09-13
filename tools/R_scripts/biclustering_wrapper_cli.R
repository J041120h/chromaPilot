#!/usr/bin/env Rscript
# CLI for biclustering wrapper module

suppressPackageStartupMessages({
  library(argparse)
  library(multiEpiCore)
})

parser <- ArgumentParser(
  prog = "biclustering_wrapper_cli.R",
  description = "Performs biclustering analysis on chromatin accessibility count matrices,
  include optional filtering, k-means clustering, and gene annotation of genomic regions"
)

# Required arguments
parser$add_argument("--cm_paths", required = TRUE,
                    help = "Comma-separated .feather count-matrix path(s)")
parser$add_argument("--out_dir", required = TRUE,
                    help = "Output directory")

# Optional arguments
parser$add_argument("--no_annotation", action = "store_true", default = FALSE,
                    help = "Disable annotation/TFBS steps (sets apply_annotation=FALSE)")
parser$add_argument("--no_plot", action = "store_true", default = FALSE,
                    help = "Disable plotting (sets plot=FALSE)")

parser$add_argument("--filter_method", default = "top_pct",
                    choices = c("top_pct", "hvr", "none"),
                    help = "Region filtering method before biclustering: top_pct, hvr, or none (default: top_pct)")
parser$add_argument("--ref_genome", default = "hg38",
                    help = "Reference genome: hg38 or mm10 (default: hg38)")

parser$add_argument("--row_km", type = "integer", default = 15,
                    help = "Number of k-means clusters for rows (default: 15)")
parser$add_argument("--col_km", type = "integer", default = 4,
                    help = "Number of k-means clusters for cols (default: 4)")

args <- parser$parse_args()

tryCatch({

  # Parse cm paths
  cm_paths <- trimws(strsplit(args$cm_paths, ",")[[1]])
  if (length(cm_paths) == 1L) cm_paths <- cm_paths[[1]]

  # Map negative flags to function booleans
  apply_annotation <- !isTRUE(args$no_annotation)
  plot <- !isTRUE(args$no_plot)

  biclustering_wrapper(
    cm_path = cm_paths,
    out_dir = args$out_dir,
    filter_method = args$filter_method,
    row_km = args$row_km,
    col_km = args$col_km,
    apply_annotation = apply_annotation,
    ref_genome = args$ref_genome,
    plot = plot
  )

}, error = function(e) {
  message("ERROR: ", conditionMessage(e))
  quit(status = 1)
})
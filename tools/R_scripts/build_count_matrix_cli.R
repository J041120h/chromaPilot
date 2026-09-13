#!/usr/bin/env Rscript
# CLI for count-matrix module
suppressPackageStartupMessages({
  library(argparse)
  library(multiEpiCore)
})

# Argument parser
parser <- ArgumentParser(
  prog = "build_count_matrix_cli.R",
  description = "Generate count matrix from BAM files with optional QC and transformations"
)

# Required arguments
parser$add_argument("--bam_paths", required = TRUE,
                    help = "Comma-separated BAM file paths")
parser$add_argument("--regions", required = TRUE,
                    help = "Bin size (integer) OR region file path (.bed/.tsv/.csv)")
parser$add_argument("--out_dir", required = TRUE,
                    help = "Output directory")

# Optional arguments
parser$add_argument("--ref_genome", default = "hg38",
                    help = "Reference genome: hg38 or mm10 (default: hg38)")
parser$add_argument("--sample_name", default = NULL,
                    help = "Sample name prefix for output file")
parser$add_argument("--force_chr_coord", action = "store_true", default = FALSE,
                    help = "Force CHR_start_end format in output")

args <- parser$parse_args()

# Parse arguments
tryCatch({

  # Parse BAM paths
  bam_paths <- trimws(strsplit(args$bam_paths, ",")[[1]])

  # Parse regions (either integer or file paths)
  regions_int <- suppressWarnings(as.integer(args$regions))
  if (!any(is.na(regions_int)) && length(regions_int) == 1L) {
    regions <- regions_int
  } else {
    regions <- args$regions
  }

  # Call function
  build_count_matrix(
    bam_path = bam_paths,
    regions = regions,
    out_dir = args$out_dir,
    ref_genome = args$ref_genome,
    sample_name = args$sample_name,
    force_chr_coord = args$force_chr_coord
  )

}, error = function(e) {
  message("ERROR: ", conditionMessage(e))
  quit(status = 1)
})
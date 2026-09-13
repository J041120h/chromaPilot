#!/usr/bin/env Rscript
suppressPackageStartupMessages({
  library(argparse)
  library(multiEpiCore)
})

parser <- ArgumentParser(
  prog = "peak_differential_analysis_cli.R",
  description = "Consensus peak calling and differential accessibility analysis from BAM and narrowPeak files"
)

# Required arguments
parser$add_argument("--peak_dirs", required = TRUE,
                    help = "Comma-separated per-sample directories containing narrowPeak files")
parser$add_argument("--bam_dirs", required = TRUE,
                    help = "Comma-separated per-sample directories containing BAM files")
parser$add_argument("--conditions", required = TRUE,
                    help = "Comma-separated condition labels (one per sample, matching peak_dirs/bam_dirs order)")
parser$add_argument("--out_dir", required = TRUE,
                    help = "Output directory")

# Optional arguments
parser$add_argument("--sample_names", default = NULL,
                    help = "Comma-separated sample names (optional; auto-generated if omitted)")
parser$add_argument("--ref_genome", default = "hg38",
                    help = "Reference genome: hg38 or mm10 (default: hg38)")
parser$add_argument("--bam_pattern",  default = "\\.bam$", 
                    help = "Regex pattern to match bam filenames (default: \\.bam$)")
parser$add_argument("--peak_pattern", default = "_peaks\\.narrowPeak$",
                    help = "Regex pattern to match narrowPeak filenames (default: _peaks\\.narrowPeak$)")
parser$add_argument("--window_size", type = "integer", default = NULL,
                    help = "Window size for peak merging (default: NULL, auto)")
parser$add_argument("--min_support", type = "integer", default = 2,
                    help = "Minimum number of samples a peak must appear in (default: 2)")
parser$add_argument("--lfc_threshold", type = "double", default = 1.0,
                    help = "Log2 fold-change threshold (default: 1.0)")
parser$add_argument("--p_threshold", type = "double", default = 0.05,
                    help = "P-value threshold (default: 0.05)")
parser$add_argument("--p_type", default = "fdr",
                    help = "P-value type: fdr, nominal, or bonferroni (default: fdr)")

args <- parser$parse_args()

tryCatch({
  peak_dirs  <- trimws(strsplit(args$peak_dirs,  ",")[[1]])
  bam_dirs   <- trimws(strsplit(args$bam_dirs,   ",")[[1]])
  conditions <- trimws(strsplit(args$conditions, ",")[[1]])

  sample_names <- if (!is.null(args$sample_names))
    trimws(strsplit(args$sample_names, ",")[[1]]) else NULL

  peak_differential_analysis(
    peak_dirs     = peak_dirs,
    bam_dirs      = bam_dirs,
    conditions    = conditions,
    sample_names  = sample_names,
    ref_genome    = args$ref_genome,
    out_dir       = args$out_dir,
    bam_pattern   = args$bam_pattern,
    peak_pattern  = args$peak_pattern,
    window_size   = args$window_size,
    min_support   = args$min_support,
    lfc_threshold = args$lfc_threshold,
    p_threshold   = args$p_threshold,
    p_type        = args$p_type
  )
}, error = function(e) {
  message("ERROR: ", conditionMessage(e))
  quit(status = 1)
})
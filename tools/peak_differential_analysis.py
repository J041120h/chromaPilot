import os
import glob
import shutil
import subprocess
from collections import defaultdict
from itertools import combinations
from typing import Dict, List, Optional
from langchain.tools import tool
from .LogCapture import auto_log_capture


@tool
@auto_log_capture()
def peak_differential_analysis(
    peak_dirs: List[str],
    bam_dirs: List[str],
    conditions: List[str],
    out_dir: str,
    sample_names: Optional[List[str]] = None,
    ref_genome: str = "hg38",
    bam_pattern:  str = "\\.bam$",
    peak_pattern: str = "_peaks\\.narrowPeak$",
    window_size: Optional[int] = None,
    min_support: int = 2,
    lfc_threshold: float = 0.5,
    p_threshold: float = 0.05,
    p_type: str = "fdr"
) -> Dict:
    """Perform consensus peak building and differential accessibility analysis.

    This tool runs a two-step pipeline per CRF pair:
    1. Consensus peak set: merges per-sample narrowPeak files into a unified
       reference region BED file using a minimum support threshold.
    2. Differential analysis: counts BAM fragments over consensus regions and
       runs limma-voom to identify differentially accessible regions.

    Args:
        peak_dirs: Per-sample directories containing narrowPeak files
            (one directory per sample, order must match conditions).
        bam_dirs: Per-sample directories containing BAM files
            (one directory per sample, order must match conditions).
        conditions: Condition labels, one per sample.
        out_dir: Base output directory.
        sample_names: Optional sample name list. Auto-generated if None.
        ref_genome: Reference genome assembly. Options: "hg38" or "mm10". (default: "hg38")
        bam_pattern: Regex peak_pattern used to match bam filenames. (default: "\\.bam$")
        peak_pattern: Regex peak_pattern used to match narrowPeak filenames. (default: "_peaks\\.narrowPeak$")
        window_size: Window size for peak merging. None uses automatic sizing. (default: None)
        min_support: Minimum number of samples a peak must appear in to be
            retained in the consensus set. (default: 2)
        lfc_threshold: Log2 fold-change threshold for significance. (default: 0.5)
        p_threshold: P-value threshold for significance. (default: 0.05)
        p_type: Type of P-value adjustment. Options: "fdr", "nominal", or "bonferroni". (default: "fdr")

    Returns:
        Dict: A dictionary of output paths.

            Always included:
                - "sig_regions" (defaultdict[str, List[str]]):
                    Per-pair significant region TSVs, keyed by pair name,
                    under `<out_dir>/peak/differential/<cmp_tag>/`.
                - "sig_summary" (List[str]):
                    Per-comparison summary TSVs (`summary.tsv`) under
                    `<out_dir>/peak/differential/<cmp_tag>/`.
                - "log_out" (str):
                    Path to the log file containing all captured logs.
    """
    os.makedirs(out_dir, exist_ok=True)
    da_dir = os.path.join(out_dir, "peak", "differential")
    if os.path.exists(da_dir):
        shutil.rmtree(da_dir)
    os.makedirs(da_dir, exist_ok=True)
    peak_set_dir = os.path.join(out_dir, "peak", "peak_sets")
    if os.path.exists(peak_set_dir):
        shutil.rmtree(peak_set_dir)
    os.makedirs(peak_set_dir, exist_ok=True)

    conds    = sorted(set(conditions))
    cmp_tags = [f"{b}_vs_{a}" for a, b in combinations(conds, 2)]

    # Build result dict upfront
    result = {}
    result["sig_regions"] = defaultdict(list)
    result["sig_summary"] = []

    current_dir  = os.path.dirname(os.path.abspath(__file__))
    r_script_path = os.path.join(current_dir, "R_scripts", "peak_differential_analysis_cli.R")

    cmd = [
        "Rscript", r_script_path,
        "--peak_dirs",     ",".join(peak_dirs),
        "--bam_dirs",      ",".join(bam_dirs),
        "--conditions",    ",".join(conditions),
        "--out_dir",       os.path.join(out_dir, "peak"),
        "--ref_genome",    ref_genome,
        "--bam_pattern",  bam_pattern,
        "--peak_pattern",  peak_pattern,
        "--min_support",   str(min_support),
        "--lfc_threshold", str(lfc_threshold),
        "--p_threshold",   str(p_threshold),
        "--p_type",        p_type,
    ]
    if sample_names is not None:
        cmd += ["--sample_names", ",".join(sample_names)]
    if window_size is not None:
        cmd += ["--window_size", str(window_size)]

    try:
        subprocess.run(cmd, check=True, cwd=os.path.dirname(r_script_path))

        # Collect DA results per cmp_tag
        for cmp_tag in cmp_tags:
            result["sig_summary"].append(
                os.path.join(da_dir, cmp_tag, "summary.tsv")
            )
            for sig_file in glob.glob(os.path.join(da_dir, cmp_tag, "*_sig.tsv")):
                result["sig_regions"][cmp_tag].append(sig_file)

        return result

    except Exception as e:
        print(e)
        for key in result:
            result[key] = None
        return result


if __name__ == "__main__":
    import argparse
    from pprint import pprint

    parser = argparse.ArgumentParser(
        description="Consensus peak building and differential accessibility analysis"
    )
    parser.add_argument("--peak_dirs",     required=True)
    parser.add_argument("--bam_dirs",      required=True)
    parser.add_argument("--conditions",    required=True)
    parser.add_argument("--out_dir",       required=True)
    parser.add_argument("--sample_names",  default=None)
    parser.add_argument("--ref_genome",    default="hg38")
    parser.add_argument("--bam_pattern", default="\\.bam$")
    parser.add_argument("--peak_pattern",       default="_peaks\\.narrowPeak$")
    parser.add_argument("--window_size",   type=int, default=None)
    parser.add_argument("--min_support",   type=int,   default=2)
    parser.add_argument("--lfc_threshold", type=float, default=0.5)
    parser.add_argument("--p_threshold",   type=float, default=0.05)
    parser.add_argument("--p_type",        type=str,   default="fdr")
    args = parser.parse_args()

    res = peak_differential_analysis.invoke({
        "peak_dirs":     args.peak_dirs.split(","),
        "bam_dirs":      args.bam_dirs.split(","),
        "conditions":    args.conditions.split(","),
        "out_dir":       args.out_dir,
        "sample_names":  args.sample_names.split(",") if args.sample_names else None,
        "ref_genome":    args.ref_genome,
        "bam_pattern":   args.bam_pattern,
        "peak_pattern":  args.peak_pattern,
        "window_size":   args.window_size,
        "min_support":   args.min_support,
        "lfc_threshold": args.lfc_threshold,
        "p_threshold":   args.p_threshold,
        "p_type":        args.p_type,
    })
    pprint(res)
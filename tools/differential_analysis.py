import os
import glob
import shutil
import subprocess
import pandas as pd
from collections import defaultdict
from itertools import combinations
from typing import Dict, Optional, List
from langchain.tools import tool
from .LogCapture import auto_log_capture

def extract_group_names(da_dir, cmp_tags):
    group_names = []
    for cmp_tag in cmp_tags:
        cmp_dir = os.path.join(da_dir, cmp_tag)
        all_tsvs = glob.glob(os.path.join(cmp_dir, "*_all.tsv"))
        groups = {os.path.basename(f)[:-len("_all.tsv")] for f in all_tsvs}
        if groups:
            group_names.append(groups)
    if not group_names:
        return []
    return sorted(set.union(*group_names))

@tool
@auto_log_capture()
def differential_analysis(
    count_matrix_files: List[str],
    conditions: List[str],
    out_dir: str,
    sample_names: Optional[List[str]] = None,
    col_cluster_file_path: Optional[str] = None,
    ref_genome: str = "hg38",
    min_support: int = 2,
    lfc_threshold: float = 0.5,
    p_threshold: float = 0.05,
    p_type: str = "fdr",
    no_annotation: bool = False,
    no_plot: bool = False
) -> Dict:
    """Run differential accessibility analysis across conditions from per-sample count matrices.

    This tool identifies differentially accessible genomic regions between conditions by:
    1. Loading per-sample count matrices (.feather format)
    2. Running limma-voom differential region testing for all pairwise condition comparisons
    3. Optionally generating log2-count heatmaps for significant regions
    4. Optionally performing GREAT-based pathway enrichment annotation on significant regions

    Args:
        count_matrix_files: List of absolute paths to per-sample count matrix files in .feather format.
                  Must be the same length as `conditions` and in the exact same order —
                  count_matrix_files[i] belongs to conditions[i]. Each file must contain a `pos` column and one column per CRF pair.
        conditions: Condition label for each sample, in the same order as `count_matrix_files`.
                    - conditions[i] is the group label for count_matrix_files[i]. 
                    - Each unique condition must appear at least twice (≥2 replicates). 
                    - All pairwise comparisons between distinct conditions are tested (sorted alphabetically, in the form `<test>_vs_<ref>`).
        out_dir: Root output directory. A `differential/` subdirectory is created here,
                 within which one subdirectory per comparison is created automatically.
        sample_names: Optional display names for each sample, in the same order as `count_matrix_files`and `conditions`. (default: None)
                - If None, names are auto-generated as <condition><replicate_index>
        col_cluster_file_path: Optional path to a TSV file with columns `pair` and `cluster`,mapping CRF pairs to column clusters. (default: None)
                - When provided, counts are summed within each cluster before testing and results are reported per cluster. 
                - When None (per-pair mode), each CRF pair is tested independently.
        ref_genome: Reference genome assembly. Options: "hg38" (human) or "mm10" (mouse).  (default: "hg38")
        min_support: Minimum number of samples a peak must appear in to be retained in the consensus set. (default: 2)
        lfc_threshold: Log2 fold-change threshold for significance. (default: 0.5)
        p_threshold: P-value threshold for significance. (default: 0.05)
        p_type: Type of P-value adjustment. Options: "fdr", "nominal", or "bonferroni". (default: "fdr")
        no_annotation: If True, skip GREAT-based pathway enrichment annotation. (default: False)
        no_plot: If True, suppress all plot outputs (heatmaps and summary bar charts). (default: False)

    Returns:
        Dict: A dictionary of output paths. Keys included depend on `no_annotation` and `no_plot`.

        Always included:
            - 'sig_regions' (Dict[str, List[str]]):
                    Per-group lists of significant region tables, one file per comparison.
                    Keys are group names (cluster1/cluster2/... or CRF pair names).
                    Each file contains only regions passing FDR and |logFC| thresholds.
                    Only files that actually exist (≥1 significant region) are included.
                    Path format: `<out_dir>/differential/<comparison>/<group>_sig.tsv`
                    Columns: pos, logFC, AveExpr, t, P.Value, adj.P.Val, B
            - 'sig_summary' (List[str]):
                    Per-comparison summary tables, one file per comparison, listing
                    filtering statistics and significant region counts per group.
                    Path format: `<out_dir>/differential/<comparison>/summary.tsv`
                    Columns: group, n_rows_before, n_rows_after_nonzero, n_rows_after_mean,
                             n_sig, n_up, n_down
            - 'log_out' (str):
                    Path to the log file containing all messages generated during execution.

        Included only when `no_plot` is False:
            - 'sig_summary_barplot' (List[str]):
                    Per-comparison mirror bar chart PDFs (up-regulated in red above x-axis,
                    down-regulated in blue below).
                    Path format: `<out_dir>/differential/<comparison>/summary.pdf`

        Included only when `no_annotation` is False AND `no_plot` is False:
            - 'pathway_annotation_bubbleplots' (List[str]):
                    Per-comparison bubble plot PDFs summarising enrichment across all groups
                    (rows = pathways by best padj; bubble size = log2 fold enrichment;
                    color = -log10(padj)).
                    Path format: `<out_dir>/differential/<comparison>/pathway_annotation/pathway_annotation.pdf`

    Example:
        result = differential_analysis(
            count_matrix_files=["output/count_matrix/C1/C1_Count_Matrix_800.feather",
                      "output/count_matrix/C2/C2_Count_Matrix_800.feather",
                      "output/count_matrix/T1/T1_Count_Matrix_800.feather",
                      "output/count_matrix/T2/T2_Count_Matrix_800.feather"],
            conditions=["C", "C", "T", "T"],
            out_dir="output",
            col_cluster_file_path="output/bicluster/col_table.tsv",
        )
        # Returns:
        # {
        #   'sig_regions': {
        #       'cluster2': ['output/differential/T_vs_C/cluster2_sig.tsv'],
        #       'cluster4': ['output/differential/T_vs_C/cluster4_sig.tsv'],
        #   },
        #   'sig_summary':        ['output/differential/T_vs_C/summary.tsv'],
        #   'sig_summary_barplot': ['output/differential/T_vs_C/summary.pdf'],
        #   'pathway_annotation_bubbleplots': ['output/differential/T_vs_C/pathway_annotation/pathway_annotation.pdf'],
        #   'log_out': 'output/log/differential_analysis.log',
        # }
    """
    # input validation
    if len(count_matrix_files) != len(conditions):
        return {
            "success": False,
            "error": f"count_matrix_files (n={len(count_matrix_files)}) and conditions (n={len(conditions)}) must be the same length."
        }

    if sample_names is not None and len(sample_names) != len(conditions):
        return {
            "success": False,
            "error": f"sample_names (n={len(sample_names)}) must be the same length as conditions (n={len(conditions)})."
        }

    # Create output dir
    da_dir = os.path.join(out_dir, 'differential')
    if os.path.exists(da_dir):
        shutil.rmtree(da_dir)
    os.makedirs(da_dir, exist_ok=True)

    # Build cmp tages
    result = {}
    result["sig_regions"] = defaultdict(list)
    result["sig_summary"] = []
    if not no_annotation:
        result["pathway_annotation_tables"] = defaultdict(list)
    if not no_plot:
        result["sig_summary_barplot"] = []
    if not no_annotation and not no_plot:
        result["pathway_annotation_bubbleplots"] = []
    
    conds = sorted(set(conditions))
    cmp_tags = [f"{b}_vs_{a}" for a, b in combinations(conds, 2)]

    # Get R script path
    current_dir = os.path.dirname(os.path.abspath(__file__))
    r_script_path = os.path.join(current_dir, "R_scripts", "differential_analysis_cli.R")

    cmd = [
        "Rscript", r_script_path,
        "--cm_paths",   ",".join(count_matrix_files),
        "--conditions", ",".join(conditions),
        "--out_dir",    da_dir,
        "--ref_genome", ref_genome,
        "--min_support",   str(min_support),
        "--lfc_threshold", str(lfc_threshold),
        "--p_threshold",   str(p_threshold),
        "--p_type",        p_type,
    ]

    if sample_names is not None:
        cmd += ["--sample_names", ",".join(sample_names)]
    if col_cluster_file_path is not None:
        cmd += ["--col_cluster_file_path", col_cluster_file_path]
    if no_annotation:
        cmd.append("--no_annotation")
    if no_plot:
        cmd.append("--no_plot")

    try:
        subprocess.run(cmd, check=True, cwd=os.path.dirname(r_script_path))
        group_names = extract_group_names(da_dir, cmp_tags)

        for cmp_tag in cmp_tags:
            result["sig_summary"].append(os.path.join(da_dir, cmp_tag, f"summary.tsv"))
            for grp_name in group_names:
                sig_files = glob.glob(os.path.join(da_dir, cmp_tag, f"{grp_name}_sig.tsv"))
                if sig_files:
                    result["sig_regions"][grp_name].append(sig_files[0])

            if not no_plot:
                result["sig_summary_barplot"].append(os.path.join(da_dir, cmp_tag, f"summary.pdf"))
                if not no_annotation:
                    result["pathway_annotation_bubbleplots"].append(os.path.join(da_dir, cmp_tag, "pathway_annotation", f"pathway_annotation.pdf"))
        return result
    
    except Exception as e:
        print(e)
        for key in result.keys():
            result[key] = None
        return result

if __name__ == "__main__":
    import argparse
    from pprint import pprint

    parser = argparse.ArgumentParser(
        description="Differential accessibility analysis from per-sample count matrices")
    parser.add_argument("--count_matrix_files", required=True,
                        help="Comma-separated paths to .feather count matrix files")
    parser.add_argument("--conditions", required=True,
                        help="Comma-separated condition labels, one per sample")
    parser.add_argument("--out_dir", required=True,
                        help="Root output directory")
    parser.add_argument("--sample_names", default=None,
                        help="Comma-separated sample names (optional)")
    parser.add_argument("--col_cluster_file_path", default=None,
                        help="Path to TSV file mapping CRF pairs to column clusters")
    parser.add_argument("--ref_genome", default="hg38",
                        help="Reference genome assembly (default: hg38)")
    parser.add_argument("--min_support",   type=int,   default=2)
    parser.add_argument("--lfc_threshold", type=float, default=0.5)
    parser.add_argument("--p_threshold",   type=float, default=0.05)
    parser.add_argument("--p_type",        type=str,   default="fdr",
                        help="P-value type: fdr, nominal, or bonferroni (default: fdr)")
    parser.add_argument("--no_annotation", action="store_true", default=False,
                        help="Skip pathway enrichment annotation")
    parser.add_argument("--no_plot", action="store_true", default=False,
                        help="Suppress all plot outputs")
    args = parser.parse_args()

    res = differential_analysis.invoke({
        "count_matrix_files": args.count_matrix_files.split(","),
        "conditions": args.conditions.split(","),
        "out_dir": args.out_dir,
        "sample_names": args.sample_names.split(",") if args.sample_names else None,
        "col_cluster_file_path": args.col_cluster_file_path,
        "ref_genome": args.ref_genome,
        "min_support":   args.min_support,
        "lfc_threshold": args.lfc_threshold,
        "p_threshold":   args.p_threshold,
        "p_type":        args.p_type,
        "no_annotation": args.no_annotation,
        "no_plot": args.no_plot,
    })
    pprint(res)
import os
import subprocess
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from typing import Dict, List
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_files_prefix

# calculate_mapping_rate
#     - Input:
#       1. <alignment_files>: List of absolute paths to BAM files for this specific sample
#       2. <sample>: Name of the single sample being analyzed
#       3. <output_dict>: Base output directory where reports will be saved
#     - Output:
#       1. A dictionary containing:
#             - 'report_files': List of generated report file absolute paths including:
#                 - CSV file with detailed alignment statistics
#                 - PDF heatmaps for total reads (log10), duplication rate, and mapped unique reads (log10)
#                 or None if the analysis failed
#             - 'log_out': (see Global Output Parameters)
#     - Function: Generate alignment statistics and visualization reports for a single sample's BAM files, and user must specify the sample name.


def parse_flagstat_output(bam_file, flagstat_file, prefix):
  try:
    stats = {
        'name': prefix,
        'total_reads': 0,
        'mapped_reads': 0,
        'properly_paired_reads': 0,
        'duplicate_reads': 0,
        'mapped_unique_reads': 0
    }
    subprocess.run(
        ["bash", "-c", f"samtools flagstat {bam_file} > {flagstat_file}"], check=True)

    with open(flagstat_file, 'r') as f:
      lines = f.readlines()

    for line in lines:
      line = line.strip()
      if 'in total' in line:
        stats['total_reads'] = int(line.split()[0])
      elif 'mapped (' in line and 'primary' not in line:
        stats['mapped_reads'] = int(line.split()[0])
      elif 'properly paired' in line:
        stats['properly_paired_reads'] = int(line.split()[0])
      elif 'duplicates' in line:
        stats['duplicate_reads'] = int(line.split()[0])

    mapped_unique_file = f"{flagstat_file}.mapped_unique"
    subprocess.run(
        ["bash", "-c", f"samtools view -c -F 1028 {bam_file} > {mapped_unique_file}"], check=True)
    with open(mapped_unique_file, 'r') as f:
      stats['mapped_unique_reads'] = int(f.read().strip())

    os.remove(flagstat_file)
    os.remove(mapped_unique_file)

    # Use total_reads to calculate the ratio
    if 'total_reads' in stats and stats['total_reads'] > 0:
      stats['mapping_rate'] = stats.get(
          'mapped_reads', 0) / stats['total_reads']
      stats['properly_paired_rate'] = stats.get(
          'properly_paired_reads', 0) / stats['total_reads']
      stats['duplication_rate'] = stats.get(
          'duplicate_reads', 0) / stats['total_reads']
      stats['mapped_unique_rate'] = stats.get(
          'mapped_unique_reads', 0) / stats['total_reads']
      return stats

    else:
      print("Error parsing flagstat output: fail to capture total_reads (The bam file seems empty or wrong)")
      return None

  except Exception as e:
    print(f"Error parsing flagstat output: {e}")
    return None


def create_log10_heatmap(stats_df, log_dict):
  heatmap_data = stats_df.set_index(
      'name')[['total_reads', 'duplication_rate', 'mapped_unique_reads']].copy()
  heatmap_data['total_reads'] = np.log10(heatmap_data['total_reads'] + 1)
  heatmap_data['mapped_unique_reads'] = np.log10(
      heatmap_data['mapped_unique_reads'] + 1)
  col_names = ['Log10(Total Reads + 1)', 'Duplication Rate',
               'Log10(Mapped Unique Reads + 1)']
  pdf_paths = []
  for idx, col in enumerate(heatmap_data.columns):
    fig, ax = plt.subplots(figsize=(max(8, len(stats_df) * 0.8), 4))
    sns.heatmap(
        heatmap_data[[col]].T,
        annot=True,
        fmt='.3f',
        cmap='viridis',
            cbar_kws={'label': col_names[idx]},
        ax=ax
    )
    ax.set_title(f'{col_names[idx]}', fontsize=14, fontweight='bold')
    fig.tight_layout()
    pdf_path = os.path.join(log_dict, f'{col}_heatmap.pdf')
    fig.savefig(pdf_path, bbox_inches='tight', dpi=300, format='pdf')
    plt.close(fig)
    pdf_paths.append(pdf_path)
  return pdf_paths


@tool
@auto_log_capture()
def calculate_mapping_rate(
        alignment_files: List,
        sample: str,
        output_dict: str
) -> Dict:
  """Generate alignment statistics and visualization reports for a single sample's BAM files.

  This tool analyzes BAM alignment files from one specific sample to calculate comprehensive
  mapping statistics and create visual reports. It processes multiple BAM files (e.g., different
  barcode combinations) within the sample and generates both tabular and heatmap visualizations
  of key metrics. For multiple samples, call this function separately for each sample.

  The analysis includes:
  - Total read counts
  - Mapping rates
  - Properly paired rates
  - Duplication rates
  - Uniquely mapped reads
  - Log-transformed heatmap visualizations

  Args:
          alignment_files: List of absolute paths to BAM files for this specific sample
          sample: Name of the single sample being analyzed
          output_dict: Base output directory where reports will be saved

  Returns:
          Dict: Dictionary containing:
                  - 'report_files': List of generated report file absolute paths including:
                          - CSV file with detailed alignment statistics
                          - PDF heatmaps for total reads (log10), duplication rate, and mapped unique reads (log10)
                          or None if the analysis failed
                  - 'log_out': String containing all captured logs from processing this sample,
                                          including samtools flagstat outputs and any error messages

  Example:
          result = get_mapping_rate(
                  alignment_files=["/data/sample_001/barcode1.bam", 
                                                "/data/sample_001/barcode2.bam"],
                  sample="sample_001",
                  output_dict="/path/to/output"
          )
          # result will contain:
          # {
          #     'report_files': ['/path/to/output/log/sample_001/sample_001_alignment_stats.csv',
          #                      '/path/to/output/log/sample_001/total_reads_heatmap.pdf',
          #                      '/path/to/output/log/sample_001/duplication_rate_heatmap.pdf',
          #                      '/path/to/output/log/sample_001/mapped_unique_reads_heatmap.pdf'],
          #     'log_out': '[SUBPROCESS] Executing: samtools flagstat...\n12345678 + 0 in total\n...'
          # }

  Note:
          - Analyzes files for one sample per function call
          - For multiple samples, call this function separately for each sample
          - Requires samtools to be installed and accessible in PATH
          - Generates three separate heatmap PDFs for better visualization
          - Heatmap width scales with the number of BAM files analyzed
          - Log10 transformation is applied to read counts for better visualization
          - Temporary flagstat files are cleaned up after processing
          - All reports are saved in: {output_dict}/log/{sample}/
  """

  log_dict = os.path.join(output_dict, 'log', sample)
  os.makedirs(log_dict, exist_ok=True)
  csv_path = os.path.join(log_dict, f'{sample}_alignment_stats.csv')

  file_prefixes = get_files_prefix(alignment_files, '.bam')
  stats_df = pd.DataFrame()
  try:
    for bam_file, prefix in zip(alignment_files, file_prefixes, prefix):
      flagstat_file = os.path.join(log_dict, f'{prefix}_alignment.txt')
      stats = parse_flagstat_output(bam_file, flagstat_file)
      stats_df.loc[prefix] = stats
    stats_df.to_csv(csv_path)
    pdf_paths = create_log10_heatmap(stats_df, log_dict)
    report_files = [csv_path] + pdf_paths
    return {'report_files': report_files}
  except:
    return {'report_files': None}

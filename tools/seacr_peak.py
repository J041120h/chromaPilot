import os
import re
import shutil
import subprocess
import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import gaussian_kde
from typing import Dict, List
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_files_path, get_files_prefix


def is_bedgraph_header(line):
  line = line.strip()
  if not line or line.startswith('#') or line.startswith('track') or line.startswith('browser'):
    return True
  try:
    parts = line.split('\t')
    if len(parts) < 4:
      return True
    start, end = int(parts[1]), int(parts[2])
    if end <= start:
      return True
    return False
  except:
    return True


def create_auc_file(bedgraph_file, seacr_dict, prefix):
  try:
    print("Creating experimental AUC file")
    auc_bed = os.path.join(seacr_dict, f"{prefix}.auc.bed")
    auc_summary_file = os.path.join(seacr_dict, f"{prefix}.auc")

    with open(bedgraph_file, 'r') as f_in, open(auc_bed, 'w') as f_out:
      lines = f_in.readlines()
      state = 1
      header = True
      chr_name = start = stop = max_signal = coord = auc = num = None
      for line_idx, line in enumerate(lines):
        # Skipping anotation lines
        if header and is_bedgraph_header(line):
          print(f"Skipping header line {line_idx}: {line}")
          continue
        else:
          header = False
        # Parsing data lines
        parts = line.strip().split('\t')
        signal = float(parts[3])
        if signal <= 0:
          continue
        # Process the first valid data line
        if state == 1:
          chr_name = parts[0]
          start = int(parts[1])
          stop = int(parts[2])
          max_signal = signal
          coord = f"{parts[0]}:{parts[1]}-{parts[2]}"
          auc = signal * (int(parts[2]) - int(parts[1]))
          num = 1
          state = 2
        else:
          # Merge consecutive intervals
          if chr_name == parts[0] and int(parts[1]) == stop:
            num += 1
            stop = int(parts[2])
            auc += signal * (int(parts[2]) - int(parts[1]))
            if signal > max_signal:
              max_signal = signal
              coord = f"{parts[0]}:{parts[1]}-{parts[2]}"
            elif signal == max_signal:
              coord_parts = coord.split('-')
              coord = f"{coord_parts[0]}-{parts[2]}"
          # Start a new interval
          else:
            f_out.write(
                f"{chr_name}\t{start}\t{stop}\t{auc}\t{max_signal}\t{coord}\t{num}\n")
            chr_name = parts[0]
            start = int(parts[1])
            stop = int(parts[2])
            max_signal = signal
            coord = f"{parts[0]}:{parts[1]}-{parts[2]}"
            auc = signal * (int(parts[2]) - int(parts[1]))
            num = 1
      # Add the last interval
      if chr_name is not None:
        f_out.write(
            f"{chr_name}\t{start}\t{stop}\t{auc}\t{max_signal}\t{coord}\t{num}\n")

    subprocess.run(
        ['bash', '-c', f'cut -f 4,7 {auc_bed} > {auc_summary_file}'], check=True)
    return auc_bed, auc_summary_file

  except Exception as e:
    print(f"Error creating AUC file: {e}")
    return None, None


def dist2d(a, b, c):
  v1 = b - c
  v2 = a - b
  m = np.array([v1, v2])
  d = np.linalg.det(m) / np.sqrt(np.sum(v1 * v1))
  return d


def calculate_density_peak(data):
  if len(data) <= 1:
    return data[0] if len(data) == 1 else None
  if np.all(data == data[0]):
    return data[0]

  try:
    kde = gaussian_kde(data)
    bw = 1.06 * np.std(data) * len(data)**(-1/5)
    kde.set_bandwidth(bw / np.std(data))

    x_min, x_max = np.min(data), np.max(data)
    if x_min == x_max:
      return x_min

    x_grid = np.linspace(x_min, x_max, 512)
    density_vals = kde(x_grid)

    peak_idx = np.argmax(density_vals)
    return x_grid[peak_idx]
  except:
    return np.median(data)


def calculate_ecdf(x, eval_points):
  ecdf_vals = []
  for point in eval_points:
    count = np.sum(x <= point)
    ecdf_vals.append(count / len(x))
  return np.array(ecdf_vals)


def calculate_normalization_factor(exp_vec, ctrl_vec):
  try:
    exp_vec_sorted = np.sort(exp_vec)[::-1]
    exp_count = np.linspace(1, 0, num=len(exp_vec))
    exp_quant = exp_vec_sorted / np.max(exp_vec)
    exp_diff = np.abs(exp_count - exp_quant)

    if np.max(exp_diff) == 0:
      exp_mask = np.ones_like(exp_diff, dtype=bool)
    else:
      exp_mask = exp_diff > 0.9 * np.max(exp_diff)

    ctrl_vec_sorted = np.sort(ctrl_vec)[::-1]
    ctrl_count = np.linspace(1, 0, num=len(ctrl_vec))
    ctrl_quant = ctrl_vec_sorted / np.max(ctrl_vec)
    ctrl_diff = np.abs(ctrl_count - ctrl_quant)

    if np.max(ctrl_diff) == 0:
      ctrl_mask = np.ones_like(ctrl_diff, dtype=bool)
    else:
      ctrl_mask = ctrl_diff > 0.9 * np.max(ctrl_diff)

    if not np.any(exp_mask) or not np.any(ctrl_mask):
      return None

    exp_distances = []
    for i in range(len(exp_count)):
      if exp_mask[i]:
        dist = dist2d(np.array([exp_count[i], exp_quant[i]]),
                      np.array([0, 0]), np.array([1, 1]))
        exp_distances.append((dist, exp_vec_sorted[i]))

    ctrl_distances = []
    for i in range(len(ctrl_count)):
      if ctrl_mask[i]:
        dist = dist2d(np.array([ctrl_count[i], ctrl_quant[i]]),
                      np.array([0, 0]), np.array([1, 1]))
        ctrl_distances.append((dist, ctrl_vec_sorted[i]))

    if not exp_distances or not ctrl_distances:
      return None

    exp_max_dist = max(exp_distances, key=lambda x: x[0])
    ctrl_max_dist = max(ctrl_distances, key=lambda x: x[0])
    exp_90th = np.sort(exp_vec)[int(0.9 * len(exp_vec))]
    ctrl_90th = np.sort(ctrl_vec)[int(0.9 * len(ctrl_vec))]

    exp_value = max(exp_max_dist[1], exp_90th)
    ctrl_value = max(ctrl_max_dist[1], ctrl_90th)
    exp_subset = exp_vec[exp_vec <= exp_value]
    ctrl_subset = ctrl_vec[ctrl_vec <= ctrl_value]

    if len(exp_subset) <= 1 or len(ctrl_subset) <= 1:
      return None

    exp_peak = calculate_density_peak(exp_subset)
    ctrl_peak = calculate_density_peak(ctrl_subset)

    if exp_peak is None or ctrl_peak is None or ctrl_peak == 0:
      return None

    constant = exp_peak / ctrl_peak
    return constant

  except Exception as e:
    print(f"normalization calculation failed: {e}")
    return None


def pct_remain_func(x, exp_vec, both):
  exp_above = len(exp_vec) - np.sum(exp_vec <= x)
  both_above = len(both) - np.sum(both <= x)
  if both_above == 0:
    return 0
  return exp_above / both_above


def pct_remain_func_2(x, exp_num, ctrl_num):
  exp_cdf = np.sum(exp_num <= x) / len(exp_num)
  ctrl_cdf = np.sum(ctrl_num <= x) / len(ctrl_num)
  return 1 - (exp_cdf - ctrl_cdf)


def calculate_threshold(exp_auc, is_ctrl_file, ctrl_auc, normalized, seacr_dict, prefix):
  try:
    exp_pd = pd.read_csv(exp_auc, header=None, sep='\t')
    exp_vec, exp_num = exp_pd[0].to_numpy(), exp_pd[1].to_numpy()

    if is_ctrl_file:
      ctrl_pd = pd.read_csv(ctrl_auc, header=None, sep='\t')
      ctrl_vec, ctrl_num = ctrl_pd[0].to_numpy(), ctrl_pd[1].to_numpy()

      if normalized:
        constant = calculate_normalization_factor(exp_vec, ctrl_vec)
        ctrl_vec = ctrl_vec * constant
        ctrl_num = ctrl_num * constant

      both = np.concatenate([exp_vec, ctrl_vec])
      x = np.sort(np.unique(both))
      pct_values = [pct_remain_func(xi, exp_vec, both) for xi in x]
      valid_idx = [i for i, p in enumerate(
          pct_values) if p < 1 and not np.isnan(p)]

      if valid_idx:
        max_pct_idx = valid_idx[np.argmax([pct_values[i] for i in valid_idx])]
        x0 = x[max_pct_idx]

        # Find z values
        z_mask = x <= x0
        z = x[z_mask]
        z_pct = [pct_values[i] for i, val in enumerate(x) if val in z]

        if len(z) > 1:
          target_pct = (pct_values[max_pct_idx] + min(z_pct)) / 2
          z2_idx = np.argmin([abs(p - target_pct) for p in z_pct])
          z2 = z[z2_idx]

          if x0 != z2:
            z_filtered = z[z > z2]
            if len(z_filtered) > 0:
              mid_val = np.max(z_filtered) - \
                  (np.max(z_filtered) - np.min(z_filtered)) / 2
              z0 = z_filtered[np.argmin(np.abs(z_filtered - mid_val))]
            else:
              z0 = x0
          else:
            z0 = x0
        else:
          z0 = x0
      else:
        x0 = np.median(x)
        z0 = x0

      if len(x) > 1:
        pct_diff = np.abs(np.diff(pct_values))
        if len(pct_diff) > 0:
          frame = pd.DataFrame({
              'thresh': x[:-1],
              'pct': pct_values[:-1],
              'diff': pct_diff
          })
          frame = frame.dropna()

          if len(frame) > 0:
            # Find high percentile threshold for differences
            i = 2
            output = 0
            while output == 0:
              test3 = float('0.' + '9' * i)
              if len(frame['diff']) > 0:
                output = np.percentile(frame['diff'], test3 * 100)
              i += 1

            if output > 0:
              a_mask = (frame['diff'] != 0) & (frame['diff'] < output)
              a = frame.loc[a_mask, 'thresh'].values

              if len(a) > 0:
                a_pct = [pct_remain_func(ai, exp_vec, both) for ai in a]
                valid_a = [i for i, p in enumerate(
                    a_pct) if p < 1 and not np.isnan(p)]

                if valid_a:
                  max_a_idx = valid_a[np.argmax([a_pct[i] for i in valid_a])]
                  a0 = a[max_a_idx]

                  # Similar calculation for b values
                  b_mask = a <= a0
                  b = a[b_mask]
                  b_pct = [a_pct[i] for i, val in enumerate(a) if val in b]

                  if len(b) > 1:
                    target_b_pct = (a_pct[max_a_idx] + min(b_pct)) / 2
                    b2_idx = np.argmin([abs(p - target_b_pct) for p in b_pct])
                    b2 = b[b2_idx]

                    if a0 != b2:
                      b_filtered = b[b > b2]
                      if len(b_filtered) > 0:
                        mid_b = np.max(
                            b_filtered) - (np.max(b_filtered) - np.min(b_filtered)) / 2
                        b0 = b_filtered[np.argmin(np.abs(b_filtered - mid_b))]
                      else:
                        b0 = a0
                    else:
                      b0 = a0
                  else:
                    b0 = a0

                  # Check if refined threshold is acceptable
                  max_a_pct = max(
                      [p for i, p in enumerate(a_pct) if not np.isnan(p)] or [0])
                  max_x_pct = max([p for p in pct_values if p <
                                  1 and not np.isnan(p)] or [0])

                  if max_x_pct > 0 and max_a_pct / max_x_pct > 0.95:
                    x0 = a0
                    z0 = b0

      # Calculate d0 for num values
      both2 = np.concatenate([exp_num, ctrl_num])
      d = np.sort(np.unique(both2))

      d_pct2 = [pct_remain_func_2(di, exp_num, ctrl_num) for di in d]
      d_above_1 = d[np.array(d_pct2) > 1]
      d0 = np.min(d_above_1) if len(d_above_1) > 0 else 1

      # Calculate FDR
      fdr = [1 - pct_remain_func(x0, exp_vec, both),
             1 - pct_remain_func(z0, exp_vec, both)]

    else:
      exp_percentiles = 1 - \
          stats.rankdata(exp_vec, method='ordinal') / len(exp_vec)
      expnum_percentiles = 1 - \
          stats.rankdata(exp_num, method='ordinal') / len(exp_num)
      x0_mask = exp_percentiles <= ctrl_auc
      z0_mask = expnum_percentiles <= ctrl_auc

      x0 = np.min(exp_vec[x0_mask]) if np.any(x0_mask) else np.min(exp_vec)
      z0 = np.min(exp_num[z0_mask]) if np.any(z0_mask) else np.min(exp_num)
      d0 = 0
      fdr = ctrl_auc

    threshold_file = os.path.join(seacr_dict, f"{prefix}.threshold.txt")
    fdr_file = os.path.join(seacr_dict, f"{prefix}.fdr.txt")
    with open(threshold_file, 'w') as f:
      f.write(f"{x0}\n{z0}\n{d0}\n")
    with open(fdr_file, 'w') as f:
      if isinstance(fdr, list):
        for f_val in fdr:
          f.write(f"{f_val}\n")
      else:
        f.write(f"{fdr}\n")

    norm_file = os.path.join(seacr_dict, f"{prefix}.norm.txt")
    with open(norm_file, 'w') as f:
      if normalized and is_ctrl_file:
        f.write(f"{constant}\n")

    return threshold_file, fdr_file, norm_file
  except Exception as e:
    print(f"AUG threshold calculation failed: {e}")
    return None, None, None


def apply_threshold_filter(exp_auc_bed, seacr_dict, prefix, thresh, thresh3, mode):
  exp_threshold_bed = os.path.join(seacr_dict, f"{prefix}_threshold.bed")
  with open(exp_auc_bed, 'r') as f_in, open(exp_threshold_bed, 'w') as f_out:
    for line in f_in:
      parts = line.strip().split('\t')
      if len(parts) >= 7:
        auc_val = float(parts[3])
        num_val = float(parts[6])
        if auc_val > thresh and num_val > thresh3:
          f_out.write('\t'.join(parts[:6]) + '\n')
  return exp_threshold_bed


def normalize_control_file(ctrl_auc_bed, norm_constant):
  temp_file = ctrl_auc_bed + '.tmp'
  with open(ctrl_auc_bed, 'r') as infile, open(temp_file, 'w') as outfile:
    for line in infile:
      parts = line.strip().split('\t')
      if len(parts) >= 6:
        parts[3] = str(float(parts[3]) * norm_constant)
        outfile.write('\t'.join(parts) + '\n')
  os.replace(temp_file, ctrl_auc_bed)
  return ctrl_auc_bed


def calculate_merge_distance(threshold_bed_file):
  total_length = 0
  count = 0
  with open(threshold_bed_file, 'r') as infile:
    for line in infile:
      parts = line.strip().split('\t')
      if len(parts) >= 3:
        length = int(parts[2]) - int(parts[1])
        total_length += length
        count += 1
  if count == 0:
    return 0
  return (total_length / count) / 10


def awk_like_numeric(value):
  if isinstance(value, str):
    try:
      if '.' not in value:
        return int(value)
      else:
        float_val = float(value)
        if float_val == int(float_val):
          return int(float_val)
        return float_val
    except ValueError:
      return value
  return value


def awk_like_calculation(val1, val2, operation='multiply'):
  num1 = awk_like_numeric(val1)
  num2 = awk_like_numeric(val2)

  if operation == 'multiply':
    result = num1 * num2
  elif operation == 'add':
    result = num1 + num2
  else:
    result = num1

  if isinstance(result, float) and result == int(result):
    return int(result)
  return result


def format_number(num):
  if isinstance(num, float) and num == int(num):
    return str(int(num))
  return str(num)


def merge_regions(threshold_bed_file, seacr_dict, prefix, merge_distance, ctrl_threshold_bed=None):
  final_output = os.path.join(seacr_dict, f"{prefix}.auc.threshold.merge.bed")
  try:
    temp_merged = final_output + '.temp'
    with open(threshold_bed_file, 'r') as infile, open(temp_merged, 'w') as outfile:
      lines = infile.readlines()
      if not lines:
        return final_output

      state = 1
      chr_name = start = stop = auc = max_signal = coord = None

      for line in lines:
        line = line.strip()
        if not line:
          continue

        parts = line.split('\t')
        if len(parts) < 6:
          continue

        curr_chr = parts[0]
        curr_start = int(parts[1])
        curr_stop = int(parts[2])
        curr_auc = awk_like_numeric(parts[3])
        curr_max = awk_like_numeric(parts[4])
        curr_coord = parts[5]

        if state == 1:
          chr_name = curr_chr
          start = curr_start
          stop = curr_stop
          auc = curr_auc
          max_signal = curr_max
          coord = curr_coord
          state = 2
        else:
          if curr_chr == chr_name and curr_start < stop + merge_distance:
            stop = curr_stop
            auc = awk_like_calculation(auc, curr_auc, 'add')
            if curr_max > max_signal:
              max_signal = curr_max
              coord = curr_coord
            elif curr_max == max_signal:
              coord_parts = coord.split('-')
              curr_coord_parts = curr_coord.split('-')
              coord = f"{coord_parts[0]}-{curr_coord_parts[1]}"
          else:
            outfile.write(
                f"{chr_name}\t{start}\t{stop}\t{format_number(auc)}\t{format_number(max_signal)}\t{coord}\n")
            chr_name = curr_chr
            start = curr_start
            stop = curr_stop
            auc = curr_auc
            max_signal = curr_max
            coord = curr_coord

      if chr_name is not None:
        outfile.write(
            f"{chr_name}\t{start}\t{stop}\t{format_number(auc)}\t{format_number(max_signal)}\t{coord}\n")

    if ctrl_threshold_bed and os.path.exists(ctrl_threshold_bed):
      cmd = ['bedtools', 'intersect', '-wa', '-v',
             '-a', temp_merged, '-b', ctrl_threshold_bed]
      with open(final_output, 'w') as outfile:
        subprocess.run(cmd, stdout=outfile, check=True)
      os.remove(temp_merged)
    else:
      os.replace(temp_merged, final_output)

    return final_output

  except Exception as e:
    print(f"Merging regions failed: {e}")
    return None


@tool
@auto_log_capture()
def seacr_peak(
    bedgraph_files: List,
    sample: str,
    output_dict: str,
    control_bedgraph_or_threshold: str | float = 0.05,
    mode: str = "stringent",
    normalized: bool = True
) -> Dict:
  """Call peaks from bedGraph files for a single sample using the SEACR algorithm implemented in Python.

  This tool performs peak calling on bedGraph files from one specific sample using a Python
  implementation of SEACR (Sparse Enrichment Analysis for CUT&RUN and Hi-Plex technology). It processes multiple 
  bedGraph files (e.g., different barcode combinations) within the sample and identifies 
  enriched regions. The algorithm is optimized for sparse chromatin profiling data from 
  CUT&RUN and CUT&Tag experiments. For multiple samples, call this function separately 
  for each sample.

  SEACR algorithm features:
  - AUC (Area Under Curve) calculation for signal enrichment
  - Adaptive threshold determination based on signal distribution
  - Optional control-based peak calling with normalization
  - Stringent and relaxed modes for different specificity levels
  - Region merging for consolidated peak calls

  Args:
          bedgraph_files: List of absolute paths to bedGraph files for this specific sample
          sample: Name of the single sample being processed
          output_dict: Base output directory where peak files will be saved
          control_bedgraph_or_threshold: (default: 0.05) Either an absolute path to control bedGraph file for differential
                                                                      peak calling, or a numeric threshold (0-1) for FDR-based
                                                                      peak calling without control
          mode: Peak calling stringency - "stringent" or "relaxed" (default: "stringent")
          normalized: Whether to normalize control signal when using control file (default: True)

  Returns:
          Dict: Dictionary containing:
                  - 'peak_files': List of absolute paths to peak BED files for this sample,
                                                one for each input bedGraph file, or None if peak calling fails
                  - 'log_out': String containing all captured logs from processing this sample,
                                          including AUC calculations, threshold determinations, and FDR values

  Example:
          # Using FDR threshold (no control)
          result1 = seacr_peak(
                  bedgraph_files=["/data/sample_001/barcode1.bedGraph",
                                                "/data/sample_001/barcode2.bedGraph"],
                  sample="sample_001",
                  output_dict="/path/to/output",
                  control_bedgraph_or_threshold=0.01,
                  mode="stringent"
          )

          # Using control file
          result2 = seacr_peak(
                  bedgraph_files=["/data/treatment/sample.bedGraph"],
                  sample="treatment_sample",
                  output_dict="/path/to/output",
                  control_bedgraph_or_threshold="/data/control/IgG.bedGraph",
                  mode="relaxed",
                  normalized=True
          )

          # result will contain:
          # {
          #     'peak_files': ['/path/to/output/seacr/sample_001/barcode1.stringent.bed',
          #                    '/path/to/output/seacr/sample_001/barcode2.stringent.bed'],
          #     'log_out': 'Creating experimental AUC file...\nEmpirical false discovery rate = 0.0087\n...'
          # }

  Note:
          - Processes files for one sample per function call
          - For multiple samples, call this function separately for each sample
          - Accepts either numeric threshold (0-1) or absolute path to control bedGraph file
          - When using control, normalization helps account for sequencing depth differences
          - Stringent mode provides higher specificity, relaxed mode higher sensitivity
          - Peak files are named: {barcode_combination}.{mode}.bed
          - Output files are organized by sample: {output_dict}/seacr/{sample}/
          - Temporary files are automatically cleaned up after processing
          - Requires bedtools for final peak filtering when using control
  """

  seacr_dict = os.path.join(output_dict, 'seacr', sample)
  if os.path.exists(seacr_dict):
    shutil.rmtree(seacr_dict)
  os.makedirs(seacr_dict, exist_ok=True)

  patterns = [r'^[0-9]*\.?[0-9]+$', r'^[0-9]+$']
  is_control_file = not any(re.match(pattern, str(control_bedgraph_or_threshold))
                            for pattern in patterns)
  if is_control_file:
    print(
        f'Creating control AUC file based on {control_bedgraph_or_threshold}')
    ctrl_auc_bed, ctrl_auc = create_auc_file(
        control_bedgraph_or_threshold, seacr_dict, 'control')
  else:
    ctrl_auc = control_bedgraph_or_threshold

  file_prefixes = get_files_prefix(bedgraph_files, '.bedGraph')
  try:
    for bedgraph_file, prefix in zip(bedgraph_files, file_prefixes):
      print(f'Creating experimental AUC file based on {bedgraph_file}')
      exp_auc_bed, exp_auc = create_auc_file(bedgraph_file, seacr_dict, prefix)

      threshold_file, fdr_file, norm_file = calculate_threshold(
          exp_auc, is_control_file, ctrl_auc, normalized, seacr_dict, prefix)

      with open(fdr_file, 'r') as f:
        fdr_lines = f.readlines()
        fdr = float(fdr_lines[0].strip())
        fdr2 = float(fdr_lines[1].strip()) if len(fdr_lines) > 1 else fdr
      with open(threshold_file, 'r') as f:
        thresh_lines = f.readlines()
        thresh = float(thresh_lines[0].strip())
        thresh2 = float(thresh_lines[1].strip()) if len(
            thresh_lines) > 1 else thresh
        thresh3 = float(thresh_lines[2].strip()) if len(
            thresh_lines) > 2 else 0

      if mode == "relaxed":
        selected_thresh = thresh2
        selected_fdr = fdr2
        print(
            f"Using relaxed threshold. Empirical false discovery rate = {selected_fdr}")
      else:  # stringent
        selected_thresh = thresh
        selected_fdr = fdr
        print(
            f"Using stringent threshold. Empirical false discovery rate = {selected_fdr}")
      exp_threshold_bed = apply_threshold_filter(
          exp_auc_bed, seacr_dict, prefix, selected_thresh, thresh3, mode)

      ctrl_threshold_bed = None
      if is_control_file:
        if normalized:
          with open(norm_file, 'r') as f:
            norm_constant = float(f.readline().strip())
          ctrl_auc_bed = normalize_control_file(ctrl_auc_bed, norm_constant)
        ctrl_threshold_bed = apply_threshold_filter(
            ctrl_auc_bed, seacr_dict, f"{prefix}_control", selected_thresh, thresh3, mode)

      merge_distance = calculate_merge_distance(exp_threshold_bed)
      final_output = merge_regions(
          exp_threshold_bed, seacr_dict, prefix, merge_distance, ctrl_threshold_bed)
      mode_output = os.path.join(seacr_dict, f"{prefix}.{mode}.bed")
      with open(final_output, 'r') as src, open(mode_output, 'w') as dst:
        dst.write(src.read())
      print("SEACR peak calling completed successfully")

      temp_files = [exp_auc_bed, exp_auc, threshold_file, fdr_file,
                    norm_file, final_output, exp_threshold_bed, ctrl_threshold_bed]
      for temp_file in temp_files:
        if temp_file and os.path.exists(temp_file):
          os.remove(temp_file)

    if is_control_file:
      os.remove(ctrl_auc_bed)
      os.remove(ctrl_auc)
    return {'peak_files': get_files_path(seacr_dict)}

  except Exception as e:
    print(f"SEACR peak calling failed with {e}")
    return {'peak_files': None}


def main(sample, output_dict):
  from pprint import pprint
  bedgraph_files = get_files_path(os.path.join(
      os.path.join(output_dict, "bedgraph", sample)))
  peak_out = seacr_peak(
      bedgraph_files=bedgraph_files,
      sample=sample,
      output_dict=output_dict
  )
  pprint(peak_out)


if __name__ == '__main__':
  sample = ""
  output_dict = ""
  main(sample, output_dict)

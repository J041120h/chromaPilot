import pandas as pd
import os
import re
import shutil
from typing import Dict, Tuple
from langchain.tools import tool
from .LogCapture import auto_log_capture


def detect_cols(df: pd.DataFrame) -> Tuple[str, str]:
  df.columns = [col.lower().strip() for col in df.columns]
  target_col, barcode_col = None, None

  target_cols = ["target", "crf"]
  barcode_cols = ["bc", "barcode", "sequence", "index"]
  for col in df.columns:
    if target_col is None and col in target_cols:
      target_col = col
    if barcode_col is None and col in barcode_cols:
      barcode_col = col
    if target_col is not None and barcode_col is not None:
      break

  if target_col is None or barcode_col is None:
    print(f"Warning: Could not find required columns in barcode input file. Attempting to use first two columns.")
    pat = re.compile(r"^[ACGT]+$", re.IGNORECASE)
    for i in range(min(2, len(df.columns))):
      colname = df.columns[i]
      col_series = df.iloc[:, i].apply(
          lambda x: str(x).strip() if pd.notnull(x) else "")
      is_seq = col_series.apply(lambda x: bool(pat.fullmatch(x))).all()
      lengths = df.iloc[:, i].apply(lambda x: len(
          str(x).strip()) if pd.notnull(x) else pd.NA).dropna()
      mode_len = int(lengths.mode().iloc[0]) if not lengths.mode().empty else 0
      equal_len = (lengths == mode_len).all() if mode_len > 0 else False
      if is_seq and equal_len and barcode_col is None:
        barcode_col = colname
      else:
        if target_col is None:
          target_col = colname

  if barcode_col is None:
    raise ValueError("Could not identify barcode column in input file.")
  return target_col, barcode_col


@tool
@auto_log_capture()
def create_barcode_fasta(input: str, out_dir: str) -> Dict:
  """Create forward and reverse barcode FASTA files from a spreadsheet for demultiplexing.

  Processes a spreadsheet (Excel or CSV) containing barcode sequences and generates paired FASTA files for downstream demultiplexing. 
  - Column detection is case-insensitive, recognizing 'target'/'crf' as target headers and 'bc'/'barcode'/'sequence'/'index' as barcode headers.
  - Target names are sanitized before output: spaces, dots, and slashes replaced with '_', hyphens removed. 
  - Both output FASTA files contain identical content.

  Args:
    input: Absolute path to input file containing barcode information (.xlsx/.xls or .csv/.tsv format)
    out_dir: Base output directory (absolute path required) where barcode FASTA files will be saved

  Returns:
    Dict: Dictionary containing:
      - 'barcode_fwd_fasta': Absolute path to forward barcode FASTA file
      - 'barcode_rev_fasta': Absolute path to reverse barcode FASTA file
      - 'log_out': Path to the log file containing all messages generated during function execution

  Example:
    result = create_barcode_fasta(
      input="/path/to/barcodes.xlsx",
      out_dir="/path/to/output"
    )
    # result will contain:
    # {
    #     'barcode_fwd_fasta': '/path/to/output/barcode/barcode_fwd.fasta',
    #     'barcode_rev_fasta': '/path/to/output/barcode/barcode_rev.fasta.fasta',
    #     'log_out': '/path/to/output/log/create_barcode_fasta.log'
    # }
  """
  fasta_dict = os.path.join(out_dir, 'barcode')
  if os.path.exists(fasta_dict):
    shutil.rmtree(fasta_dict)
  os.makedirs(fasta_dict, exist_ok=True)

  if input.endswith(('.xlsx', '.xls')):
    barcode_df = pd.read_excel(input)
  elif input.endswith(('.tsv')):
    barcode_df = pd.read_csv(input, sep='\t')
  elif input.endswith(('.csv')):
    barcode_df = pd.read_csv(input)
  else:
    raise ValueError(
        "Unsupported file format. Please provide an Excel (.xlsx/.xls) or TSV (.tsv) file.")

  try:
    target_col, barcode_col = detect_cols(barcode_df)
    print(
        f"Detected target column: {target_col}, barcode column: {barcode_col}")

    # Clean target names
    barcode_df[target_col] = barcode_df[target_col].str.replace(
        r'[ /.]', '_', regex=True).str.replace('-', '', regex=False)

    # Creat fasta context
    fasta_lines = []
    for _, row in barcode_df.iterrows():
      fasta_lines.append(f">{row[target_col]}")
      fasta_lines.append(row[barcode_col])
    fasta_content = '\n'.join(fasta_lines)
    print(f"Generated FASTA with {len(fasta_lines)//2} barcodes")

    # Same content for both forward and reverse
    output_names = [
        "barcode_fwd_fasta",
        "barcode_rev_fasta"
    ]
    output_files = [
        "barcode_fwd.fasta",
        "barcode_rev.fasta"
    ]
    output_paths = [os.path.join(fasta_dict, filename)
                    for filename in output_files]
    for filepath in output_paths:
      with open(filepath, 'w') as f:
        f.write(fasta_content)

    print("Barcode FASTQ creation completed successfully!")
    return dict(zip(output_names, output_paths))
  except Exception as e:
    print(f"Error: Barcode FASTQ creation failed with {e}")
    return {"barcode_fwd_fasta": None, "barcode_rev_fasta": None}


if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(description="Create barcode FASTA file")
  parser.add_argument("-i", "--input", required=True, help="Barcode input")
  parser.add_argument("-o", "--out_dir", required=True,
                      help="Base output directory")
  args = parser.parse_args()

  res = create_barcode_fasta.invoke({
      "input": args.input,
      "out_dir": args.out_dir
  })
  pprint(res)

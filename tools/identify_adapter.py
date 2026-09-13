import os
import re
import gzip
import math
import shutil
import subprocess
from typing import List, Dict, Optional
from collections import deque
from multiprocessing import Pool
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .utils import get_fastq_prefix, get_cpu_core, get_files_path

LABEL_TO_SAMTAG = {"CB": "CB", "UMI": "UB"}

class AdapterConfig:
  def __init__(self, tag_list, label_list, error_rate, laxity, not_found_symbol="[Not Found]", bases="ATCGN"):
    self.tag_list = tag_list
    self.label_list = label_list
    self.error_rate = error_rate
    self.laxity = laxity
    self.not_found_symbol = not_found_symbol
    self.bases = bases
    self.config_list = self.process_tag()

  def _hamming_mismatches(self, s1: str, s2: str) -> int:
    return sum(c1 != c2 for c1, c2 in zip(s1, s2))
  
  def check_ambiguity(self, config: Dict[str, int]):
    seqs = list(config.keys())
    for i, seq1 in enumerate(seqs[:-1]):
      mm1 = config[seq1]
      for seq2 in seqs[i+1:]:
        if len(seq1) != len(seq2):
          continue
        mm2 = config[seq2]
        dist = self._hamming_mismatches(seq1, seq2)
        if dist <= mm1 + mm2:
          print(
            f"Warning: potential ambiguity detected between candidate "
            f"sequences:\n"
            f"  - seq1: '{seq1}' (length={len(seq1)}, max_mismatch={mm1})\n"
            f"  - seq2: '{seq2}' (length={len(seq2)}, max_mismatch={mm2})\n"
            f"  - Hamming distance: {dist}\n"
            f"  - Overlap threshold (mm1 + mm2): {mm1 + mm2}\n"
            f"  Interpretation: because distance <= allowed mismatch budget sum, "
            f"these two candidates may match overlapping observed sequences.\n"
            f"  Consequence: read assignment may depend on candidate order in the "
            f"matching loop.\n"
            f"  Suggestion: reduce error_rate, remove one of the candidates, or "
            f"use a more specific sequence set."
          )

  def process_tag(self):
    config_list = []
    for tag in self.tag_list:
      if isinstance(tag, int):
        config_list.append(tag)
        continue
      elif isinstance(tag, str):
        tag = [tag]

      config = {}
      seen = set()
      for t in tag:
        if t in seen:
          print(
            f"Warning: duplicate candidate sequence detected: '{t}'. "
            f"This sequence appears more than once in the candidate list."
          )
        else:
          seen.add(t)
          config[t] = math.floor(len(t) * self.error_rate)

      self.check_ambiguity(config)
      config_list.append(config)
    return config_list
  
  def check_read_for_tag(self, read: str):
    queue = deque()
    queue.append((0, 0, 0, ""))
    read_len = len(read)

    while queue:
      offset, config_idx, advance_used, out = queue.popleft()

      if config_idx >= len(self.config_list):
        return out

      if offset >= read_len:
        continue

      if advance_used > self.laxity:
        continue

      config = self.config_list[config_idx]
      matched = False

      if isinstance(config, int):
        if offset + config <= read_len:
          captured = read[offset:offset + config]
          new_out = f"{out}-[{captured}]"
          queue.append((offset + config, config_idx + 1, 0, new_out))
          matched = True
      else:
        # exact match first
        for seq, max_mm in config.items():
          if read.startswith(seq, offset):
            new_out = f"{out}-[{seq}]"
            queue.append((offset + len(seq), config_idx + 1, 0, new_out))
            matched = True
            break

        # fuzzy match if exact failed
        if not matched:
          for seq, max_mm in config.items():
            if max_mm <= 0:
              continue
            end = offset + len(seq)
            if end > read_len:
              continue
            sub = read[offset:end]
            mm = self._hamming_mismatches(sub, seq)
            if mm <= max_mm:
              # keep the actually observed sequence segment, for downstream trim / annotation
              new_out = f"{out}-[{sub}]"
              queue.append((end, config_idx + 1, 0, new_out))
              matched = True
              break

      if not matched:
        new_out = f"{out}-{read[offset]}"
        queue.append((offset + 1, config_idx, advance_used + 1, new_out))

    return self.not_found_symbol
  
def run_adapter_identification(r1_in: str, r2_in: str, r1_out: str, r2_out: str, config: AdapterConfig, record_idx: Optional[List[int]] = None, trim: bool = True, concat_paired_tags: bool = False, buffer_size = 8 * 1024 * 1024):

  def open_text_maybe_gz(path):
    if str(path).endswith(".gz"):
      return gzip.open(path, "rt", encoding="ascii", newline="")
    return open(path, "rt", encoding="ascii", newline="")

  def open_write_gz(path):
    outfile = open(path, 'wb')
    p = subprocess.Popen(
        ["pigz", "-p", "1", "-c", "-1"],
        stdin=subprocess.PIPE,
        stdout=outfile
    )
    outfile.close()
    return p

  def read_fastq_records(path):
    with open_text_maybe_gz(path) as fi:
      while True:
        n = fi.readline().rstrip("\n")
        if not n:
          break
        s = fi.readline().rstrip("\n")
        p = fi.readline().rstrip("\n")
        q = fi.readline().rstrip("\n")
        if not s or not p or not q:
          raise ValueError(f"Incomplete FASTQ record in {path}")
        yield n, s, p, q

  def trim_adapter_from_read(s, q, adapter_str):
    trim_len = sum(c.isalpha() for c in adapter_str)
    return s[trim_len:], q[trim_len:]

  def build_sam_tag_str(tags: Dict[str, str]) -> str:
    fields = []
    for label, value in tags.items():
      sam_tag = LABEL_TO_SAMTAG.get(label, label)
      if "\t" in value or "\n" in value or " " in value:
        raise ValueError(f"Invalid character in tag value for {label}: {value!r}")
      fields.append(f"{sam_tag}:Z:{value}")
    return "\t".join(fields)

  def add_tags_to_name(n, tag_str):
    nc = n.split(" ", 1)[0]
    return f"{nc} {tag_str}" if tag_str else nc

  def process_paired_tag_record(n1, s1, q1, n2, s2, q2):
    """Paired-tag mode: identify adapter on BOTH R1 and R2, trim both."""
    adapter_str_1 = config.check_read_for_tag(s1).lstrip("-")
    adapter_str_2 = config.check_read_for_tag(s2).lstrip("-")
    if adapter_str_1 == config.not_found_symbol or adapter_str_2 == config.not_found_symbol:
      return None

    if record_idx is not None:
      tags_1 = re.findall(r'\[([^\]]+)\]', adapter_str_1)
      tags_2 = re.findall(r'\[([^\]]+)\]', adapter_str_2)
      tag_str_dict = {
          name: f"{tag1}-{tag2}"
          for i, (tag1, tag2, name) in enumerate(zip(tags_1, tags_2, config.label_list))
          if i in record_idx
      }
      tag_str = build_sam_tag_str(tag_str_dict)
    else:
      tag_str = ""

    new_n1 = add_tags_to_name(n1, tag_str)
    new_n2 = add_tags_to_name(n2, tag_str)
    if trim:
      s1, q1 = trim_adapter_from_read(s1, q1, adapter_str_1)
      s2, q2 = trim_adapter_from_read(s2, q2, adapter_str_2)
    return new_n1, s1, q1, new_n2, s2, q2

  def process_single_tag_record(n1, s1, q1, n2, s2, q2):
    """Single-tag mode: identify adapter on R1 only, pass R2 through unchanged."""
    adapter_str = config.check_read_for_tag(s1).lstrip("-")
    if adapter_str == config.not_found_symbol:
      return None

    if record_idx is not None:
      tags = re.findall(r'\[([^\]]+)\]', adapter_str)
      tag_str_dict = {
          name: tag
          for i, (tag, name) in enumerate(zip(tags, config.label_list))
          if i in record_idx
      }
      tag_str = build_sam_tag_str(tag_str_dict)
    else:
      tag_str = ""

    new_n1 = add_tags_to_name(n1, tag_str)
    new_n2 = add_tags_to_name(n2, tag_str)
    if trim:
      s1, q1 = trim_adapter_from_read(s1, q1, adapter_str)
    # R2 sequence/qual untouched in single-tag mode
    return new_n1, s1, q1, new_n2, s2, q2

  process_record = process_paired_tag_record if concat_paired_tags else process_single_tag_record

  logs = []
  read_count, fail_count = 0, 0
  prefix = os.path.basename(r1_in)
  proc1 = open_write_gz(r1_out)
  proc2 = open_write_gz(r2_out)

  try:
    buf1, buf2 = [], []
    size1 = 0
    for (n1, s1, p1, q1), (n2, s2, p2, q2) in zip(read_fastq_records(r1_in), read_fastq_records(r2_in)):
      read_count += 1
      result = process_record(n1, s1, q1, n2, s2, q2)
      if result is None:
        fail_count += 1
        continue
      new_n1, new_s1, new_q1, new_n2, new_s2, new_q2 = result
      rec1 = f"{new_n1}\n{new_s1}\n{p1}\n{new_q1}\n"
      rec2 = f"{new_n2}\n{new_s2}\n{p2}\n{new_q2}\n"
      buf1.append(rec1)
      buf2.append(rec2)

      size1 += len(rec1)
      if size1 > buffer_size:
        proc1.stdin.write("".join(buf1).encode())
        proc2.stdin.write("".join(buf2).encode())
        buf1.clear()
        buf2.clear()
        size1 = 0

    if buf1:
      proc1.stdin.write("".join(buf1).encode())
      proc2.stdin.write("".join(buf2).encode())

    proc1.stdin.close()
    proc2.stdin.close()

  finally:
    if proc1.stdin and not proc1.stdin.closed:
      proc1.stdin.close()
    if proc2.stdin and not proc2.stdin.closed:
      proc2.stdin.close()
    rc1 = proc1.wait()
    rc2 = proc2.wait()
    if rc1 != 0:
        raise RuntimeError(f"pigz failed for {r1_out} with exit code {rc1}")
    if rc2 != 0:
        raise RuntimeError(f"pigz failed for {r2_out} with exit code {rc2}")

  msg = f"  [{prefix}] Done: {read_count:,} reads"
  if read_count == 0:
    msg += ", no reads found."
  elif fail_count > 0:
    msg += f", failed: {fail_count:,} ({fail_count/read_count:.2%})"
  else:
    msg += ", all reads identified."
  logs.append(msg)

  return r1_out, r2_out, logs

@tool
@auto_log_capture()
def identify_adapter(
  fastq_dir: str,
  sample: str,
  out_dir: str,
  cb: Optional[int | str | List[str]] = None,
  sp: Optional[int | str | List[str]] = None,
  umi: Optional[int | str | List[str]] = None,
  linker: Optional[int | str | List[str]] = ["GCGATCGAGGACGGCAGATGTGTATAAGAGACAG","CACCGTCTCCGCCTCAGATGTGTATAAGAGACAG"],
  error_rate: float = 0.1,
  laxity: int = 0,
  exclude: str = "unknown, IgG_control"
):
  """Identify adapter structures in paired-end FASTQ files and annotate read names with parsed tag information.

  This function parses the adapter structure from R1 (and optionally R2) reads using a configurable tag schema, annotates each read name with the identified adapter string and selected tag values (CB, UMI), and writes trimmed output FASTQs. Multiple input FASTQ pairs are processed in parallel.

  Read comment annotation format:
    CB and UMI values (if present in the adapter structure) are written as
    SAM-compliant optional fields in the read comment (the portion of the
    FASTQ header after the first space), separate from the read name itself:

        @READ_NAME CB:Z:<value>\tUB:Z:<value>

    This allows downstream alignment (bowtie2 --sam-append-comment) to carry
    these fields directly into BAM tags without further parsing.

  Args:
      fastq_dir: Absolute path to the directory containing paired-end FASTQ.gz files for this sample.
      sample: Sample name, used to organize output into a subdirectory.
      out_dir: Base output directory. Trimmed FASTQs are written to <out_dir>/trim/<sample>/.
      cb: Cell barcode definition. int for fixed length, str for fixed sequence,
          List[str] for a set of candidate sequences (default: None).
      sp: Spacer definition. Same type options as cb (default: None).
      umi: UMI definition. Same type options as cb (default: None).
      linker: Linker sequence definition. Same type options as cb
          (default: ["GCGATCGAGGACGGCAGATGTGTATAAGAGACAG","CACCGTCTCCGCCTCAGATGTGTATAAGAGACAG"]).
      error_rate: Maximum mismatch rate used to compute allowed mismatches per tag (default: 0.1).
      laxity: Number of bases allowed to skip when searching for the next tag (default: 0).
      exclude: Comma-separated string specifying FASTQ prefixes to exclude before adapter identification (default: "unknown, IgG_control")
        - For dash-separated pairs (e.g. "H3K27ac-IgG"), exclusion is triggered if any component matches
        - For simple names, exact matching is used
        - For mixed naming styles, substring matching is applied

  Returns:
      Dict with:
          - 'trimmed_dir': Absolute path to the output directory containing trimmed FASTQ files with adapter information annotated in read names, or None if processing fails.
          - 'log_out': Path to the log file containing all captured logs from processing this sample

  Example:
      result = identify_adapter(
          fastq_dir="./demux",
          sample="sample_001",
          out_dir="/path/to/output",
          cb=["AAAA", "CCCC", "GGGG", "TTTT"],
          umi=8,
          linker="GCGATCGAGGACGGCAGATGTGTATAAGAGACAG"
      )
      # result will contain:
      # {
      #     'trimmed_dir': '/path/to/output/trim/sample_001',
      #     'log_out': '/path/to/output/log/sample_001_identify_adapter.log'
      # }
  """
  # Basic info
  label_list = []
  tag_list = []
  for label, value in [
      ("CB", cb),
      ("SPACER", sp),
      ("UMI", umi),
      ("LINKER", linker)
  ]:
    if value is None:
      continue
    label_list.append(label)

    if isinstance(value, int):
      tag_list.append(value)
      print(f"{label}: fixed length = {value}")
    elif isinstance(value, str):
      tag_list.append(value)
      print(f"{label}: fixed sequence = {value}")
    elif isinstance(value, list) and all(isinstance(x, str) for x in value):
      val_dedup = list(dict.fromkeys(value)) # Remove duplicates
      tag_list.append(val_dedup)
      print(f"{label}: candidate sequences = {','.join(val_dedup)}")

  if not label_list:
    raise ValueError("No adapter structure information provided.")
  label_str = '-'.join(f"[{name}]" for name in label_list)
  print(f"Adapter structure: {label_str}")

  # Determine which tag indices correspond to recordable SAM tags (CB, UMI) for downstream use.
  record_idx = [i for i, label in enumerate(label_list) if label in LABEL_TO_SAMTAG]
  record_idx = record_idx if record_idx else None

  # Create output dir
  trim_dir = os.path.join(out_dir, "trim", sample)
  if os.path.exists(trim_dir):
    shutil.rmtree(trim_dir)
  os.makedirs(trim_dir, exist_ok=True)

  # Get .fastq.gz prefices
  fastq_files = get_files_path(fastq_dir, ext = ['.fastq.gz', '.fq.gz'])
  prefix_dict = get_fastq_prefix(fastq_files)
  if not prefix_dict:
    raise ValueError("No valid FASTQ pairs were detected from input fastq_dir.")
  
  if exclude is not None:
    exclude = [s.strip() for s in exclude.split(",") if s.strip()]
    keys = list(prefix_dict.keys())
    has_dash = ["-" in prefix for prefix in keys]
    if all(has_dash):
      keys_to_remove = [prefix for prefix in keys if any(p in exclude for p in prefix.split("-"))]
    elif not any(has_dash):
      keys_to_remove = [prefix for prefix in keys if prefix in exclude]
    else:
      keys_to_remove = [prefix for prefix in keys if any(item in prefix for item in exclude)]
    for prefix in keys_to_remove:
      prefix_dict.pop(prefix, None)
  if not prefix_dict:
    raise ValueError("No FASTQ pairs remained after applying exclude filter.")
  
  # === Run ===
  trim = True
  concat_paired_tags = True
  config = AdapterConfig(tag_list, label_list, error_rate, laxity)

  # estimate safe cores
  threads = get_cpu_core()
  core_per_task = 3
  workers = max(1, min(len(prefix_dict), int(threads // core_per_task)))
  print(f"Using workers: {workers}")

  with Pool(processes=workers) as pool:
    trimmed = []
    async_results = []
    for prefix in prefix_dict.keys():
      r1 = prefix_dict[prefix]['fwd']
      r2 = prefix_dict[prefix]['rev']
      r1_out = os.path.join(trim_dir, f"{prefix}_R1.trimmed.fastq.gz")
      r2_out = os.path.join(trim_dir, f"{prefix}_R2.trimmed.fastq.gz")
      ar = pool.apply_async(run_adapter_identification, args=(
          r1, r2, r1_out, r2_out, config, record_idx, trim, concat_paired_tags))
      async_results.append((prefix, ar))

    for prefix, ar in async_results:
      try:
        r1_out, r2_out, logs = ar.get()
        for msg in logs:
          print(msg, flush=True)
        trimmed.extend([r1_out, r2_out])
      except Exception as e:
        print(f"Failed: {prefix} -> {e}")
        return {'trimmed_dir': None}
  return {'trimmed_dir': trim_dir}


if __name__ == "__main__":
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(
      description="Identify and process adapter structures in paired-end FASTQ files")
  parser.add_argument("--fastq_dir", required=True,
                      help="Absolute path to the directory containing paired-end FASTQ.gz files")
  parser.add_argument("--sample", required=True, help="Sample name")
  parser.add_argument("--out_dir", required=True, help="Base output directory")

  parser.add_argument("--cb", default=None, help="Barcode specification")
  parser.add_argument("--sp", default=None, help="Spacer specification")
  parser.add_argument("--umi", default=None, help="UMI specification")
  parser.add_argument(
      "--linker", default="GCGATCGAGGACGGCAGATGTGTATAAGAGACAG", help="Linker specification")

  parser.add_argument("--error-rate", metavar="FLOAT", type=float, default=0.1,
                      help="Mismatch tolerance (0.0-1.0). Default: 0.1")
  parser.add_argument("--laxity", metavar="N", type=int, default=0,
                      help="Max unmatched bases between tags. Default: 0")
  parser.add_argument("--exclude", default="unknown,IgG_control",
                      help="Comma-separated FASTQ prefixes to exclude")
  args = parser.parse_args()

  # Validate numeric ranges
  if not (0.0 <= args.error_rate <= 1.0):
    parser.error(
        f"--error-rate must be between 0.0 and 1.0, got {args.error_rate}")
  if args.laxity < 0:
    parser.error(f"--laxity must be a non-negative integer, got {args.laxity}")

  def parse_tag_arg(value: str) -> "int | str | List[str] | None":
    """
    Unified parser for adapter component arguments.
    None       -> None       (component not provided)
    "8"        -> int 8      (fixed length)
    "@file"    -> List[str]  (sequences read from file, one per line)
    "A,B,C"    -> List[str]  (inline comma-separated sequences)
    "ATCG"     -> str        (single fixed sequence)
    """
    if value is None:
      return None
    if value.isdigit():
      return int(value)
    if value.startswith("@"):
      path = value[1:]
      if not os.path.isfile(path):
        parser.error(f"Sequence file not found: {path}")
      with open(path, "r") as f:
        seqs = [line.strip() for line in f if line.strip()]
      if not seqs:
        parser.error(f"Sequence file is empty: {path}")
      return seqs
    if "," in value:
      seqs = [s.strip() for s in value.split(",") if s.strip()]
      if not seqs:
        parser.error(f"Could not parse any sequences from: {value}")
      return seqs
    return value  # Single fixed sequence

  cb = parse_tag_arg(args.cb)
  sp = parse_tag_arg(args.sp)
  umi = parse_tag_arg(args.umi)
  linker = parse_tag_arg(args.linker)

  res = identify_adapter.invoke({
      "fastq_dir": args.fastq_dir,
      "sample": args.sample,
      "out_dir": args.out_dir,
      "cb": cb,
      "sp": sp,
      "umi": umi,
      "linker": linker,
      "error_rate": args.error_rate,
      "laxity": args.laxity,
      "exclude": args.exclude
  })
  pprint(res)

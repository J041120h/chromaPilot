import os
import re
import sys
import gzip
import shutil
import psutil
import pickle
import subprocess
from collections import deque, Counter

def get_cpu_core():
  """Get the number of CPU cores available on the system"""
  def _to_int(value):
    if value is None:
      return None
    try:
      return int(value)
    except Exception:
      return None

  # 1) SLURM: prefer explicit allocation
  cores = _to_int(os.environ.get("SLURM_CPUS_PER_TASK"))
  # 2) SLURM: check job allocation
  if cores is None:
    job_id = os.environ.get("SLURM_JOB_ID")
    if job_id and shutil.which("scontrol"):
      try:
        out = subprocess.check_output(
            ["scontrol", "show", "job", "-o", job_id],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        m = re.search(r"\bCPUs/Task=(\d+)\b", out)
        cores = _to_int(m.group(1)) if m else None
      except Exception:
        cores = None
  # 3) Fallback to os.cpu_count()
  if cores is None:
    local = os.cpu_count()
    # reserve 1 core
    cores = local - 1 if isinstance(local, int) and local > 1 else 1

  cores = max(1, cores)
  print(f"Using {cores} cores for processing.")
  return cores


def get_memory():
  """Get memory available on the system"""
  def _normalize_mem(mem):
    if mem is None:
      return None
    s = str(mem).strip().lower()

    if re.fullmatch(r"\d+", s):
      gb = int(s) / 1024.0
      return int(gb)

    m = re.fullmatch(r"(\d+(?:\.\d+)?)([a-z]+)", s)
    if not m:
      return None

    val = float(m.group(1))
    unit = m.group(2)
    unit = {"gb": "g", "mb": "m", "kb": "k", "tb": "t"}.get(unit, unit)
    if unit == "g":
      gb = val
    elif unit == "m":
      gb = val / 1024.0
    elif unit == "k":
      gb = val / (1024.0 * 1024.0)
    elif unit == "t":
      gb = val * 1024.0
    else:
      return None
    return int(gb)

  # 1) SLURM: prefer explicit allocation
  mem = _normalize_mem(os.environ.get("SLURM_MEM_PER_NODE"))
  # 2) SLURM: check job allocation
  if mem is None:
    job_id = os.environ.get("SLURM_JOB_ID")
    if job_id and shutil.which("scontrol"):
      try:
        out = subprocess.check_output(
            ["scontrol", "show", "job", "-o", job_id],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        mtres = re.search(r"\bTRES=[^ ]*", out)
        if mtres:
          mmem = re.search(r"\bmem=([^, ]+)", mtres.group(0))
          if mmem:
            mem_raw = mmem.group(1)
            mem = _normalize_mem(mem_raw)
      except Exception:
        mem = None
  # 3) Fallback to psutil
  if mem is None:
    try:
      svmem = psutil.virtual_memory()
      mem = int(svmem.available // (1024 * 1024 * 1024))  # in GB
    except Exception:
      mem = None

  if mem is None:
    print("Warning: Unable to determine system memory, defaulting to 8g")
    return "8g"
  else:
    usable = max(1, int(mem*0.6))
    print(f"Using {usable}g memory for processing.")
    return f"{usable}g"

def get_size_of(obj, seen=None):
  if seen is None:
    seen =set()
  
  obj_id = id(obj)
  if obj_id in seen:
    return 0
  seen.add(obj_id)
  size = sys.getsizeof(obj)

  if isinstance(obj, dict):
    size += sum(get_size_of(k, seen) + get_size_of(v, seen) for k, v in obj.items())
  elif isinstance(obj, (list, tuple, set, frozenset, deque)):
    size += sum(get_size_of(i, seen) for i in obj)

  return size


def drop_empty_files(paths, removed=True):
  def is_empty(p):
    if not os.path.exists(p):
      return True
    if p.endswith(".gz"):
      try:
        with gzip.open(p, "rb") as f:
          return f.read(1) == b""
      except:
        return False
    else:
      return os.path.getsize(p) == 0

  kept, empty = [], []
  for p in paths:
    if is_empty(p):
      empty.append(p)
    else:
      kept.append(p)
  if removed and len(empty) > 0:
    print("Warning: The following files are empty and have been excluded:")
    for p in empty:
      print(f"  {p}")
      if os.path.exists(p):
        os.remove(p)
  return kept


def get_files_path(folder_path, ext=None):
  """Get the absolute path of all files in the current directory"""
  if isinstance(ext, str):
    ext = [ext]
  
  file_paths = []
  for f in os.listdir(folder_path):
    full_path = os.path.join(folder_path, f)
    if os.path.isfile(full_path):
      if ext is None or any(f.endswith(e) for e in ext):
        file_paths.append(os.path.abspath(full_path))
  return file_paths


def get_files_prefix(file_paths, ext=None):
  """Get the prefix of all files in the input list."""
  if isinstance(ext, list):
      ext = tuple(ext)

  # Deduplicate while preserving order
  unique_paths = list(dict.fromkeys(file_paths))

  # Validate all paths exist
  missing = [p for p in unique_paths if not os.path.isfile(p)]
  if missing:
      print("[Error]: The following paths are not valid files and will be skipped: %s", missing)
  valid_paths = [p for p in unique_paths if os.path.isfile(p)]

  # Check extension compliance
  if ext is not None:
    invalid = [p for p in valid_paths if not os.path.basename(p).endswith(ext)]
    if invalid:
      print(f"[Error]: Some files do not match the specified extension {ext}")
      return None

  # Extract prefixes
  prefixes = [os.path.splitext(os.path.basename(p))[0] for p in valid_paths]

  # Check for duplicate prefixes
  counts = Counter(prefixes)
  duplicates = {
    prefix: [p for p in valid_paths if os.path.splitext(os.path.basename(p))[0] == prefix]
    for prefix, count in counts.items() if count > 1
  }
  if duplicates:
    dup_info = ", ".join(f"'{k}': {v}" for k, v in duplicates.items())
    print(f"[Error]: Duplicate file prefixes detected:\n{dup_info} ")
    return None

  return prefixes

def get_fastq_prefix(file_paths):
  KNOWN_EXTS = [".trimmed.fastq.gz", ".fastq.gz", ".fq.gz"]
  FASTQ_RE = re.compile(r"""
  ^
  (?P<prefix>.+?)
  (?:[._](?P<lane>L\d{3}))?                # (Optional) _L001 or .L001
  [._](?:R(?P<read1>[12])|(?P<read2>[12])) # _R1/_R2 or _1/_2
  (?:[._](?P<chunk>\d{3}))?                # (Optional) _001
  $
  """, re.VERBOSE)

  groups = {}
  bad = []

  file_paths = list(dict.fromkeys(file_paths))
  for p in file_paths:
    b = os.path.basename(p)
    matched_ext = None
    if matched_ext is None:
      matched_ext = next((e for e in KNOWN_EXTS if b.endswith(e)), None)
    if matched_ext is None or not b.endswith(matched_ext):
      bad.append((b, f"doesn't match any known suffix"))
      continue

    core = b[:-len(matched_ext)]
    m = FASTQ_RE.match(core)
    if not m:
      bad.append((b, "doesn't conform to the FASTQ naming convention"))
      continue
    prefix = m.group("prefix")
    read = m.group("read1") or m.group("read2")
    if prefix not in groups:
      groups[prefix] = {"1": [], "2": []}
    groups[prefix][read].append(p)

  if bad:
    print("Warning: The following input files fail:")
    for p, message in bad:
      print(f"  {p} {message}")

  out = {}
  for prefix, d in groups.items():
    r1, r2 = d["1"], d["2"]
    if len(r1) != 1 or len(r2) != 1:
      print(
          f"ERROR: Multiple forward or reverse reads found for prefix '{prefix}'")
      print(f"  forward: {', '.join(r1)}")
      print(f"  reverse: {', '.join(r2)}")
      return None
    out[prefix] = {"fwd": r1[0], "rev": r2[0]}
  return out


def get_picard_jar_path():
  """Capture the jar path from current conda environment"""
  picard_script = shutil.which('picard')
  if not picard_script or not os.path.exists(picard_script):
    print("Error: picard is not installed or not in PATH")
    return None

  source = picard_script
  while os.path.islink(source):
    current_dir = os.path.dirname(os.path.abspath(source))
    link_target = os.readlink(source)
    if not os.path.isabs(link_target):
      source = os.path.join(current_dir, link_target)
    else:
      source = link_target
  jar_dir = os.path.dirname(os.path.abspath(source))
  jar_path = os.path.join(jar_dir, 'picard.jar')
  if os.path.exists(jar_path):
    return jar_path
  else:
    print(
        f"Error: jar file does not exist in the expected location {jar_path}")
    return None

import os
import re
import pysam
import shutil
import shlex
import subprocess
from collections import defaultdict
from typing import Optional, Tuple, List
from langchain.tools import tool
from .LogCapture import auto_log_capture
from .ref_prep import ref_prep
from .utils import get_picard_jar_path, get_files_path, get_fastq_prefix, get_cpu_core, get_memory

CB_TAG = "CB"
UB_TAG = "UB"

def generate_clean_bam(
  fastq_r1: str,
  fastq_r2: str,
  group: str,
  bam_out: str,
  index_prefix: str,
  min_length: int,
  max_length: int,
  threads: int = 1,
):
  cmd_str = (
    "set -euo pipefail; "
    f"bowtie2 "
    f"-x {shlex.quote(index_prefix)} "
    f"-I {min_length} "
    f"-X {max_length} "
    f"-p {threads} "
    f"-q "
    f"--local "
    f"--very-sensitive-local "
    f"--no-unal "
    f"--no-mixed "
    f"--no-discordant "
    f"--rg-id {group} "
    f"--rg SM:{group} "
    f"--sam-append-comment "
    f"-1 {shlex.quote(fastq_r1)} "
    f"-2 {shlex.quote(fastq_r2)} "
    f"| samtools view -@ {threads} -h -q 10 - "
    r"""| awk 'BEGIN{OFS="\t"} /^@/ {print; next} ($3~/^chr([0-9]+|X|Y)$/) {print}' """
    f"| samtools view -@ {threads} -b - "
    f"> {shlex.quote(bam_out)}"
  )
  subprocess.run(["bash", "-c", cmd_str], check=True)
  return bam_out


def _dedup_bam_by_cb(bam_in: str, bam_out: str, cb_tag: str = "CB"):
  def _extract_primary_pair(
    bucket: List[pysam.AlignedSegment],
  ) -> Tuple[Optional[pysam.AlignedSegment], Optional[pysam.AlignedSegment]]:
    r1 = None
    r2 = None
    for read in bucket:
      if read.is_secondary or read.is_supplementary:
        continue
      if read.is_read1 and r1 is None:
        r1 = read
      elif read.is_read2 and r2 is None:
        r2 = read
    return r1, r2

  n_total, n_written, n_skipped, n_duplicated = 0, 0, 0, 0
  with pysam.AlignmentFile(bam_in, "rb") as fi, \
      pysam.AlignmentFile(bam_out, "wb", header=fi.header) as fo:
    seen = set()
    current_qname = None
    bucket: List[pysam.AlignedSegment] = []

    def _process_bucket(bucket: List[pysam.AlignedSegment]):
      nonlocal n_total, n_written, n_skipped, n_duplicated
      if not bucket:
        return
      n_total += 1
      r1, r2 = _extract_primary_pair(bucket)
      if r1 is None or r2 is None:
        n_skipped += 1
        return
      try:
        cb = r1.get_tag(cb_tag)
      except KeyError:
        n_skipped += 1
        return
      if cb is None:
        n_skipped += 1
        return
      _start = min(r1.reference_start, r2.reference_start)
      _end = max(r1.reference_end, r2.reference_end)
      key = (cb, r1.reference_id, _start, _end)
      if key in seen:
        n_duplicated += 1
        return
      seen.add(key)
      n_written += 1
      fo.write(r1)
      fo.write(r2)

    for read in fi.fetch(until_eof=True):
      qname = read.query_name
      if current_qname is None:
        current_qname = qname
        bucket = [read]
      elif qname == current_qname:
        bucket.append(read)
      else:
        _process_bucket(bucket)
        current_qname = qname
        bucket = [read]
    _process_bucket(bucket)  # final bucket

  print("Read counts:", dict(input=n_total, written=n_written, skipped=n_skipped, duplicated=n_duplicated))


def deduplicate_bam(
  bam: str,
  bam_out: str,
  has_cb: bool = False,
  has_umi: bool = False,
  threads: int = 1,
  java_mem: Optional[str] = None,
  picard_jar: Optional[str] = None,
):
  if has_umi:
    umi_dedup_log = f"{bam_out}.umi_dedup.log"
    if has_cb:
      # (cb-aware, umi-aware) dedup based on chrom coordination, cell barcode and UMI
      subprocess.run([
        "umi_tools", "dedup",
        "--paired",
        "-I", bam,
        "-S", bam_out,
        f"--cell-tag={CB_TAG}",
        f"--umi-tag={UB_TAG}",
        "--log", umi_dedup_log,
      ], check=True)
    else:
        # (umi-aware) dedup based on chrom coordination and UMI
      subprocess.run([
        "umi_tools", "dedup",
        "--paired",
        "-I", bam,
        "-S", bam_out,
        f"--umi-tag={UB_TAG}",
        "--log", umi_dedup_log,
      ], check=True)

  else:
    if has_cb:
      # (cb-aware) dedup based on chrom coordination and cell barcode
      name_sort_bam = f"{bam_out}.namesort.tmp.bam"
      subprocess.run(["samtools", "sort", "-n", "-@", str(threads), "-o", name_sort_bam, bam], check=True)
      _dedup_bam_by_cb(name_sort_bam, bam_out, CB_TAG)
      os.remove(name_sort_bam)
    else:
      # dedup based on only chrom coordination (Picard)
      assert java_mem is not None
      assert picard_jar is not None
      sort_bam = f"{bam_out}.sort.tmp.bam"
      subprocess.run(["samtools", "sort", "-@", str(threads), "-o", sort_bam, bam], check=True)
      metrics = f"{bam_out}.picard_metrics.log"
      subprocess.run([
        "java", f"-Xmx{java_mem}",
        "-jar", picard_jar,
        "MarkDuplicates",
        "--INPUT", sort_bam,
        "--OUTPUT", bam_out,
        "--METRICS_FILE", metrics,
        "--REMOVE_DUPLICATES", "true",
      ], check=True)
      os.remove(sort_bam)

  os.remove(bam)
  return bam_out


def sort_and_index_bam(
    bam: str,
    bam_out: str,
    threads: int = 1,
):
  result = subprocess.run(
    ["samtools", "view", "-H", bam],
    capture_output=True, text=True, check=True,
  )
  is_sorted = any("SO:coordinate" in line for line in result.stdout.splitlines())

  if is_sorted:
    os.replace(bam, bam_out)
  else:
    subprocess.run(["samtools", "sort", "-@", str(threads), "-o", bam_out, bam], check=True)
    os.remove(bam)

  subprocess.run(["samtools", "index", bam_out], check=True)
  return bam_out

def merge_bam_by_crf(
  pair_bams: list[str], 
  out_dir: str, 
  threads: int = 1
) -> list[str]:
  pair_dir = os.path.join(out_dir, "pair")
  os.makedirs(pair_dir, exist_ok=True)

  crf_to_bams = defaultdict(list)
  for bam in pair_bams:
    prefix = os.path.basename(bam).removesuffix('.bam')
    # move to pair subdirectory
    new_bam = os.path.join(pair_dir, os.path.basename(bam))
    cmd = f"mv {shlex.quote(os.path.join(out_dir, prefix))}* {shlex.quote(pair_dir)}/"
    subprocess.run(["bash", "-c", cmd], check=True)
    # 
    crfs = prefix.split('-')
    for crf in crfs:
      crf_to_bams[crf].append(new_bam)
  
  crf_bams = []
  for crf, bams in crf_to_bams.items():
    crf_bam = os.path.join(out_dir, f"{crf}.bam")
    merged_unsorted = f"{crf_bam}.merged.tmp.bam"
    subprocess.run(["samtools", "cat", "-o", merged_unsorted] + bams, check=True)
    subprocess.run(["samtools", "sort", "-@", str(threads), "-o", crf_bam, merged_unsorted], check=True)
    os.remove(merged_unsorted)
    subprocess.run(["samtools", "index", crf_bam], check=True)
    print(f"[merge_crf] {crf}: {len(bams)} input BAM(s) -> {crf_bam}")
    crf_bams.append(crf_bam)

  return crf_bams

def run_alignment(
  fastq_r1: str,
  fastq_r2: str,
  group: str,
  out_prefix: str,
  index_prefix: str,
  min_length: int,
  max_length: int,
  has_cb: bool = False,
  has_umi: bool = False,
  threads: int = 1,
  java_mem: Optional[str] = None,
  picard_jar: Optional[str] = None,
):
  # Step 1: align + filter
  clean_bam = generate_clean_bam(
    fastq_r1=fastq_r1,
    fastq_r2=fastq_r2,
    group=group,
    bam_out=f"{out_prefix}.clean.bam",
    index_prefix=index_prefix,
    min_length=min_length,
    max_length=max_length,
    threads=threads,
  )

  # Step 2: deduplication
  dedup_bam = deduplicate_bam(
    bam=clean_bam,
    bam_out=f"{out_prefix}.dedup.bam",
    has_cb=has_cb,
    has_umi=has_umi,
    threads=threads,
    java_mem=java_mem,
    picard_jar=picard_jar,
  )

  # Step 3: coordinate sort + index
  final_bam = sort_and_index_bam(
    bam=dedup_bam,
    bam_out=f"{out_prefix}.bam",
    threads=threads,
  )

  return final_bam

@tool
@auto_log_capture()
def align_reads(
    fastq_dir: str,
    sample: str,
    out_dir: str,
    ref_genome: str,
    has_cb: bool = False,
    has_umi: bool = False,
    min_length: int = 10,
    max_length: int = 800,
    picard_jar: Optional[str] = None
):
  """Align preprocessed paired-end FASTQ files to a reference genome and generate deduplicated BAM files.

  Processes all FASTQ file pairs through a three-step pipeline: alignment with Bowtie2, deduplication, and coordinate sorting with indexing. 
  Deduplication strategy is determined by the combination of `has_cb` and `has_umi`:
    - has_cb=False, has_umi=False: coordinate-based dedup via Picard MarkDuplicates
    - has_cb=True,  has_umi=False: CB-aware dedup based on genomic coordinates + cell barcode
    - has_cb=False, has_umi=True:  UMI-aware dedup via umi_tools (coordinates + UMI)
    - has_cb=True,  has_umi=True:  CB+UMI-aware dedup via umi_tools

  Args:
    fastq_dir: Absolute path to the directory containing paired-end FASTQ files for this specific sample (expects paired files with R1/R2 naming convention)
    sample: Name of the single sample being processed
    out_dir: Base output directory.
    ref_genome: Reference genome name used to obtain chromosome size information. Supported values: 'hg38' and 'mm10'.
    has_cb: Whether the library includes cell barcodes. (default: False)
      - If True, CB:Z:... is expected in the FASTQ read comment (written by the
        upstream adapt step) and carried into the BAM via bowtie2's
        --sam-append-comment. Deduplication becomes CB-aware.
    has_umi: Whether the library includes UMIs. (default: False)
      - If True, UB:Z:... is expected in the FASTQ read comment (written by the
        upstream adapt step) and carried into the BAM via bowtie2's
        --sam-append-comment. Deduplication uses umi_tools for UMI-aware collapsing.
    min_length: Minimum fragment length for valid alignments (default: 10)
    max_length: Maximum fragment length for valid alignments (default: 800)
    picard_jar: Path to the Picard JAR file. If set to None, the path will be automatically detected from the current conda environment (default: None)

  Returns:
    Dict: Dictionary containing:
      - 'bam_dir': Absolute path to the output directory containing final deduplicated BAM files for this sample, or None if alignment fails
      - 'log_out': Path to the log file containing all captured logs from processing this sample

  Example:
    result = align_reads(
      fastq_dir="/data/sample_001",
      sample="sample_001",
      out_dir="/path/to/output",
      ref_genome="hg38"
    )
    # result will contain:
    # {
    #     'bam_dir': '/path/to/output/bam/sample_001,
    #     'log_out': '/path/to/output/log/sample_001_align_reads.log'
    # }
  """
  if picard_jar is None:
    picard_jar = get_picard_jar_path()
    
  bam_dir = os.path.join(out_dir, "bam", sample)
  if os.path.exists(bam_dir):
    shutil.rmtree(bam_dir)
  os.makedirs(bam_dir, exist_ok=True)

  ref_preparation = ref_prep()
  index_prefix = ref_preparation.get_bowtie2_index(ref_genome)

  # Get resource setting
  threads = get_cpu_core()
  java_mem = get_memory()

  # Run Alignment
  fastq_files = get_files_path(fastq_dir, ext = ['.fastq.gz', '.fq.gz'])
  prefix_dict = get_fastq_prefix(fastq_files)
  if not prefix_dict:
    raise ValueError("No valid FASTQ pairs were detected from input fastq_dir.")
  
  bam_list = []
  for prefix in prefix_dict.keys():
    print(f"Processing combination: {prefix}")

    # Input FASTQ paths
    r1 = prefix_dict[prefix]['fwd']
    r2 = prefix_dict[prefix]['rev']
    # Output directory
    out_prefix = os.path.join(bam_dir, prefix)

    try:
      final_bam = run_alignment(fastq_r1=r1, fastq_r2=r2, group=prefix, out_prefix=out_prefix, index_prefix=index_prefix, min_length=min_length,  max_length=max_length, has_cb=has_cb, has_umi=has_umi, threads=threads, java_mem=java_mem, picard_jar=picard_jar)
      bam_list.append(final_bam)
      print(f"Successfully processed {prefix}")
    except Exception as e:
      print(f"Error processing {prefix}: {str(e)}")
      return {"bam_dir": None}

  print("Alignment completed successfully!")
  return {"bam_dir": bam_dir}


if __name__ == '__main__':
  import argparse
  from pprint import pprint

  parser = argparse.ArgumentParser(description="Align reads using Bowtie2")
  parser.add_argument("--fastq_dir", required=True,
                      help="Absolute path to the directory containing paired-end FASTQ.gz files")
  parser.add_argument("--sample", required=True, help="Sample name")
  parser.add_argument("--out_dir", required=True, help="Base output directory")
  parser.add_argument("--ref_genome", required=True,
                      help="Reference genome name")
  parser.add_argument('--has-cb', action='store_true')
  parser.add_argument('--has-umi', action='store_true')
  parser.add_argument("--min_length", type=int, default=10,
                      help="Minimum fragment length for valid alignments (default: 10)")
  parser.add_argument("--max_length", type=int, default=800,
                      help="Maximum fragment length for valid alignments (default: 800)")
  args = parser.parse_args()

  res = align_reads.invoke({
      "fastq_dir": args.fastq_dir,
      "sample": args.sample,
      "out_dir": args.out_dir,
      "ref_genome": args.ref_genome,
      "has_cb": args.has_cb,
      "has_umi": args.has_umi,
      "min_length": args.min_length,
      "max_length": args.max_length
  })
  pprint(res)

import os
import urllib.request
import gzip
import shutil
import subprocess
import sys
import json
import hashlib
import re


class ref_prep:
  def __init__(self, fixed_directory=None):
    # If no directory specified, use parent directory of current script location
    if fixed_directory is None:
      script_dir = os.path.dirname(os.path.abspath(__file__))
      parent_dir = os.path.dirname(script_dir)
      self.fixed_directory = os.path.join(parent_dir, "ref")
    else:
      self.fixed_directory = fixed_directory

    os.makedirs(self.fixed_directory, exist_ok=True)

    # Initialize ref.json
    self.ref_json_path = os.path.join(self.fixed_directory, "ref.json")
    if os.path.exists(self.ref_json_path):
      with open(self.ref_json_path, 'r') as f:
        self.ref_data = json.load(f)
    else:
      self.ref_data = {}
      with open(self.ref_json_path, 'w') as f:
        json.dump(self.ref_data, f, indent=2)

    # Define download sources for common genomes
    self.genome_sources = {
        "hg38": {
            "url": "https://genome-idx.s3.amazonaws.com/bt/GRCh38_noalt_as.zip",
            "type": "bowtie2_index",
            "compressed": "zip"
        },
        "mm10": {
            "url": "https://genome-idx.s3.amazonaws.com/bt/mm10.zip",
            "type": "bowtie2_index",
            "compressed": "zip"
        },
        # "hg19": {
        #     "url": "https://genome-idx.s3.amazonaws.com/bt/hg19.zip",
        #     "type": "bowtie2_index",
        #     "compressed": "zip"
        # }
    }

    # Define download sources for chromosome size files
    self.chromsize_sources = {
        "hg38": {
            "url": "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.chrom.sizes",
            "type": "chromsize",
            "compressed": None
        },
        "mm10": {
            "url": "https://hgdownload.soe.ucsc.edu/goldenPath/mm10/bigZips/mm10.chrom.sizes",
            "type": "chromsize",
            "compressed": None
        },
        # "hg19": {
        #     "url": "https://hgdownload.soe.ucsc.edu/goldenPath/hg19/bigZips/hg19.chrom.sizes",
        #     "type": "chromsize",
        #     "compressed": None
        # }
    }

    # Define download sources for ENCODE CHIP-seq balcklist
    self.blacklist_sources = {
      "hg38": {
        "url": "https://github.com/Boyle-Lab/Blacklist/raw/master/lists/hg38-blacklist.v2.bed.gz",
        "compressed": "gz"
      },
      "mm10": {
        "url": "https://github.com/Boyle-Lab/Blacklist/raw/master/lists/mm10-blacklist.v2.bed.gz",
        "compressed": "gz"
      }
    }

  def get_checksums(self, name):
    save_dir = os.path.join(self.fixed_directory, name)
    checksums = {}
    for fn in sorted(os.listdir(save_dir)):
      # Calculate MD5 checksum of a file"
      if not os.path.isfile(os.path.join(save_dir, fn)):
        continue
      h = hashlib.md5()
      with open(os.path.join(save_dir, fn), 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
          h.update(chunk)
      checksums[fn] = h.hexdigest()
    return checksums

  def update_ref_json(self, name):
    """Update ref_data to ref.json"""
    checksums = self.get_checksums(name)
    self.ref_data[name] = checksums
    with open(self.ref_json_path, 'w') as f:
      json.dump(self.ref_data, f, indent=2)

  def match_checksums(self, name):
    if name not in self.ref_data:
      return False

    stored = self.ref_data[name]
    current = self.get_checksums(name)
    stored_files = set(stored.keys())
    current_files = set(current.keys())

    extra = sorted(current_files - stored_files)
    missing = sorted(stored_files - current_files)

    mismatched = []
    for fn in stored_files & current_files:
      if stored[fn] != current[fn]:
        mismatched.append(fn)

    passed = (not missing and not extra and not mismatched)
    return passed

  def get_index_prefix(self, name):
    """Get the actual index prefix from the bt2 files"""
    save_dir = os.path.join(self.fixed_directory, name)

    # Find any .bt2 file to determine the actual prefix
    for file in os.listdir(save_dir):
      if file.endswith('.bt2'):
        # Use regex to remove bowtie2 suffixes
        # This pattern matches .rev.1.bt2, .rev.2.bt2, .1.bt2, .2.bt2, etc.
        match = re.match(r'^(.+?)(?:\.rev)?(?:\.[1-6])?\.bt2$', file)
        if match:
          actual_prefix = match.group(1)
          return os.path.join(save_dir, actual_prefix)

    raise FileNotFoundError(f"No .bt2 file found in {save_dir}")

  def download_file(self, url, destination):
    """Download file with progress indicator"""
    print(f"Downloading from {url}...")

    # Track last reported percentage to avoid duplicate outputs
    last_reported = -1

    def download_progress(block_num, block_size, total_size):
      downloaded = block_num * block_size
      percent = min(downloaded * 100 / total_size, 100)

      # Only report progress at 10% intervals
      current_ten_percent = int(percent // 10) * 10
      nonlocal last_reported

      if current_ten_percent != last_reported and current_ten_percent > 0:
        last_reported = current_ten_percent
        sys.stdout.write(f'\rDownload progress: {current_ten_percent}%')
        sys.stdout.flush()

    urllib.request.urlretrieve(url, destination, reporthook=download_progress)
    print("\nDownload complete!")

  def extract_file(self, file_path, extract_to):
    """Extract compressed files"""
    print(f"Extracting {file_path}...")

    if file_path.endswith('.zip'):
      import zipfile
      with zipfile.ZipFile(file_path, 'r') as zip_ref:
        # Extract to a temporary directory first
        temp_extract = os.path.join(extract_to, 'temp_extract')
        zip_ref.extractall(temp_extract)

        # Move all .bt2 files to the parent directory
        for root, dirs, files in os.walk(temp_extract):
          for file in files:
            if file.endswith('.bt2'):
              src = os.path.join(root, file)
              dst = os.path.join(extract_to, file)
              shutil.move(src, dst)

        # Clean up temporary directory
        shutil.rmtree(temp_extract)

    elif file_path.endswith('.tar.gz'):
      import tarfile
      with tarfile.open(file_path, 'r:gz') as tar_ref:
        tar_ref.extractall(extract_to)
    elif file_path.endswith('.gz'):
      # For single gzipped files (like FASTA)
      output_file = os.path.join(extract_to, os.path.basename(file_path[:-3]))
      with gzip.open(file_path, 'rb') as f_in:
        with open(output_file, 'wb') as f_out:
          shutil.copyfileobj(f_in, f_out)
      os.remove(file_path)
      return output_file

    # Clean up compressed file after extraction
    os.remove(file_path)
    print("Extraction complete!")

  def build_bowtie2_index(self, fasta_file, index_name):
    """Build Bowtie2 index from FASTA file"""
    print(f"Building Bowtie2 index for {index_name}...")
    name = f"bowtie2_{index_name}"
    save_dir = os.path.join(self.fixed_directory, name)
    index_prefix = os.path.join(save_dir, index_name)

    cmd = ['bowtie2-build', fasta_file, index_prefix]

    try:
      subprocess.run(cmd, check=True)
      print("Index building complete!")
    except subprocess.CalledProcessError as e:
      print(f"Error building index: {e}")
      raise
    except FileNotFoundError:
      print("Error: bowtie2-build not found. Please ensure Bowtie2 is installed and in PATH.")
      raise

  def get_bowtie2_index(self, index_name):
    """Main function to get existing index or create new one"""
    name = f"bowtie2_{index_name}"
    save_dir = os.path.join(self.fixed_directory, name)

    # Check if index directory exists and verify checksums
    if os.path.exists(save_dir) and bool(os.listdir(save_dir)):
      if self.match_checksums(name):
        index_prefix = self.get_index_prefix(name)
        print(
            f"Bowtie2 index '{index_name}' found and verified at {index_prefix}")
        return index_prefix
      else:
        print(
            f"Checksums mismatch for '{index_name}'. Removing and re-downloading...")
        shutil.rmtree(save_dir)

    os.makedirs(save_dir, exist_ok=True)

    # Check if we have a download source for this genome
    if index_name in self.genome_sources:
      source_info = self.genome_sources[index_name]
      url = source_info["url"]

      # Download file
      temp_file = os.path.join(
          save_dir, f"temp_{index_name}.{source_info['compressed']}")
      self.download_file(url, temp_file)

      # Extract file
      self.extract_file(temp_file, save_dir)

      # If it's a FASTA file, we need to build the index
      if source_info["type"] == "fasta":
        fasta_files = [f for f in os.listdir(
            save_dir) if f.endswith(('.fa', '.fasta', '.fna'))]
        if fasta_files:
          fasta_path = os.path.join(save_dir, fasta_files[0])
          self.build_bowtie2_index(fasta_path, index_name)
        else:
          raise FileNotFoundError("No FASTA file found after extraction")

      # Ensure no-empty
      if not bool(os.listdir(save_dir)):
        raise RuntimeError(
            f"Chromsize download failed, directory is empty: {save_dir}")
    else:
      # If no predefined source, try to find a FASTA file
      print(f"No predefined source for '{index_name}'.")
      print(f"Please place a FASTA file in {save_dir} and run again.")
      raise ValueError(f"No download source defined for index '{index_name}'")

    # Calculate and store checksums only after successful extraction
    try:
      self.update_ref_json(name)
      index_prefix = self.get_index_prefix(name)
      print(f"Bowtie2 index '{index_name}' is ready at {index_prefix}")
      return index_prefix
    except Exception as e:
      # Clean up on failure
      if os.path.exists(save_dir):
        shutil.rmtree(save_dir)
      raise RuntimeError(
          f"Failed to create Bowtie2 index for '{index_name}': {e}")

  def get_chromsize(self, index_name):
    """Download or retrieve chromosome size file"""
    name = f"chromsize_{index_name}"
    save_dir = os.path.join(self.fixed_directory, name)

    # Check if chromsize directory exists and verify checksums
    if os.path.exists(save_dir) and bool(os.listdir(save_dir)):
      if self.match_checksums(name):
        chromsize_files = [f for f in os.listdir(
            save_dir) if os.path.isfile(os.path.join(save_dir, f))]
        if chromsize_files:
          chromsize_path = os.path.join(save_dir, chromsize_files[0])
          print(
              f"Chromosome size file for '{index_name}' found and verified at {chromsize_path}")
          return chromsize_path
      else:
        print(
            f"Checksums mismatch for chromsize '{index_name}'. Removing and re-downloading...")
        shutil.rmtree(save_dir)

    os.makedirs(save_dir, exist_ok=True)

    # Check if we have a download source for this chromsize
    if index_name in self.chromsize_sources:
      source_info = self.chromsize_sources[index_name]
      url = source_info["url"]

      # Download file directly (no compression)
      filename = f"{index_name}.chrom.sizes"
      chromsize_path = os.path.join(save_dir, filename)
      self.download_file(url, chromsize_path)

      # Ensure no-empty
      if not bool(os.listdir(save_dir)):
        raise RuntimeError(
            f"Bowtie2 index download failed, directory is empty: {save_dir}")
    else:
      print(f"No predefined chromosome size source for '{index_name}'.")
      raise ValueError(
          f"No download source defined for chromosome size '{index_name}'")

    # Calculate and store checksums only after successful download
    try:
      self.update_ref_json(name)
      print(f"Chromosome size file for '{name}' is ready at {chromsize_path}")
      return chromsize_path
    except Exception as e:
      # Clean up on failure
      if os.path.exists(save_dir):
        shutil.rmtree(save_dir)
      raise RuntimeError(
          f"Failed to download chromosome size for '{index_name}': {e}")

  def get_blacklist(self, index_name):
    name = f"blacklist_{index_name}"
    save_dir = os.path.join(self.fixed_directory, name)

    if os.path.exists(save_dir) and bool(os.listdir(save_dir)):
        if self.match_checksums(name):
            files = [f for f in os.listdir(save_dir) if os.path.isfile(os.path.join(save_dir, f))]
            if files:
                path = os.path.join(save_dir, files[0])
                print(f"Blacklist for '{index_name}' found and verified at {path}")
                return path
        else:
            print(f"Checksums mismatch for blacklist '{index_name}'. Removing and re-downloading...")
            shutil.rmtree(save_dir)

    os.makedirs(save_dir, exist_ok=True)

    if index_name not in self.blacklist_sources:
        raise ValueError(f"No download source defined for blacklist '{index_name}'")

    source_info = self.blacklist_sources[index_name]
    temp_file = os.path.join(save_dir, f"{index_name}-blacklist.v2.bed.gz")
    self.download_file(source_info["url"], temp_file)
    self.extract_file(temp_file, save_dir)

    blacklist_path = os.path.join(save_dir, f"{index_name}-blacklist.v2.bed")

    try:
        self.update_ref_json(name)
        print(f"Blacklist for '{index_name}' is ready at {blacklist_path}")
        return blacklist_path
    except Exception as e:
        if os.path.exists(save_dir):
            shutil.rmtree(save_dir)
        raise RuntimeError(f"Failed to download blacklist for '{index_name}': {e}")

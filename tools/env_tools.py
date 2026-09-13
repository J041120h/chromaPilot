import shutil
import subprocess


def install_package_tool(
    env_name: str,
    package: str,
    use_conda: bool = True,
    conda_channel: str = "",
    version: str = ""
) -> str:
  """
  Installs a Python or bioinformatics package inside a conda environment.

  Parameters:
  - env_name: name of the conda environment.
  - package: name of the package to install (e.g., 'cutadapt', 'bowtie2').
  - use_conda: whether to try conda first (default: True). If False, uses pip directly.
  - conda_channel: which conda channel to use (e.g., 'bioconda', 'conda-forge').
  - version: optional version string (e.g., '2.10.1'). Leave empty for latest.

  Returns:
  - stdout/stderr of the installation command or error message.
  """

  full_package = f"{package}={version}" if version else package

  if use_conda:
    conda_cmd = [
        "conda", "install", "-n", env_name, f"-c {conda_channel} {full_package}" if conda_channel else full_package, "--freeze-installed", "-y"
    ]
    result = subprocess.run(
        conda_cmd,
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
      return f"[conda] ✅ Installed '{full_package}'"
    else:
      error_info = result.stderr or result.stdout or "Unknown error"
      return f"[conda] ❌ Failed to install '{full_package}' from channel '{conda_channel}': {error_info}"
  else:
    print("Skipping conda and trying pip directly...")

    pip_cmd = [
        "conda", "run", "-n", env_name, "pip", "install", f"{package}=={version}" if version else package
    ]
    result = subprocess.run(
        pip_cmd,
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
      return f"[pip] ✅ Installed '{full_package}'"
    else:
      error_info = result.stderr or result.stdout or "Unknown error"
      return f"[pip] ❌ Failed to install '{package}': {error_info}"


def create_virtual_env(env_name: str) -> str:
  """Creates a new virtual environment using conda or venv."""
  conda_path = shutil.which("conda")
  channels = ["defaults", "bioconda", "conda-forge"]
  if conda_path:
    try:
      subprocess.run(["conda", "create", "--name", env_name,
                     "python=3.10", "-y"], check=True)
      for channel in channels:
        subprocess.run(["conda", "run", "-n", env_name,
                        "conda", "config", "--env", "--add", "channels", channel])
      subprocess.run(["conda", "run", "-n", env_name,
                      "conda", "config", "--env", "--set", "channel_priority", "strict"])
      return f"Conda environment '{env_name}' created."
    except subprocess.CalledProcessError as e:
      return f"Failed to create conda environment '{env_name}': {e}"
  else:
    try:
      subprocess.run(["python3", "-m", "venv", env_name], check=True)
      return f"Venv '{env_name}' created."
    except subprocess.CalledProcessError as e:
      return f"Failed to create venv '{env_name}': {e}"

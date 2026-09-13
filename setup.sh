#!/bin/bash
set -e

env_name="${1:-chromapilot}"

# ===== Create conda environment =====
echo "Setting up conda environment: $env_name"

if conda env list | grep -q "^${env_name}\s"; then
    echo "Conda environment '$env_name' already exists."
    read -p "Do you want to remove and recreate it? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Removing existing environment: $env_name"
        conda env remove -n "$env_name" -y
        echo "Environment removed successfully."
    else
        echo "Setup cancelled."
        exit 0
    fi
fi

echo "Creating conda environment: $env_name"
conda env create -n "$env_name" -f "$(dirname "$0")/environment.yaml"

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "$env_name" || {
    echo "Failed to activate conda environment: $env_name"
    exit 1
}

echo "Conda environment $env_name is built successfully!"


# ===== Write to configuration file =====
if [[ "$OSTYPE" == "darwin"* ]]; then
    SED_INPLACE=(-i '')
else
    SED_INPLACE=(-i)
fi

CONFIG_FILE="$(dirname "$0")/config.yaml"
EXAMPLE_FILE="$(dirname "$0")/config.example.yaml"

if [ -f "$CONFIG_FILE" ]; then
    sed "${SED_INPLACE[@]}" "s|^conda_env:.*|conda_env: '$env_name'|" "$CONFIG_FILE"
    echo "Updated conda_env in existing config file: $CONFIG_FILE"
else
    cp "$EXAMPLE_FILE" "$CONFIG_FILE"
    sed "${SED_INPLACE[@]}" "s|^conda_env:.*|conda_env: '$env_name'|" "$CONFIG_FILE"
    echo "Created config file: $CONFIG_FILE"
    NEEDS_KEY=1
fi

# ===== Configure R environment =====
unset R_LIBS
unset R_LIBS_SITE
unset R_LIBS_USER
unset R_PROFILE
unset R_ENVIRON

export R_LIBS_USER="$CONDA_PREFIX/rlib"
export R_ENVIRON_USER="$CONDA_PREFIX/.Renviron"
export R_PROFILE_USER="$CONDA_PREFIX/.Rprofile"

echo "Configuring isolated R environment..."

USER_RLIB="$CONDA_PREFIX/rlib"
mkdir -p "$USER_RLIB"

RPROFILE_FILE="$CONDA_PREFIX/.Rprofile"
RENVIRON_FILE="$CONDA_PREFIX/.Renviron"
ACTIVATE_FILE="$CONDA_PREFIX/etc/conda/activate.d/r_env.sh"
DEACTIVATE_FILE="$CONDA_PREFIX/etc/conda/deactivate.d/r_env.sh"

touch "$RPROFILE_FILE" "$RENVIRON_FILE"
mkdir -p "$(dirname "$ACTIVATE_FILE")" "$(dirname "$DEACTIVATE_FILE")"

# Force R to use only the conda-specific library
cat > "$RENVIRON_FILE" <<EOF
# ==== Renviron ====
R_LIBS_USER=${CONDA_PREFIX}/rlib
# ==================
EOF

grep -q "libPaths" "$RPROFILE_FILE" 2>/dev/null || cat >> "$RPROFILE_FILE" <<'EOF'
# ==== Rprofile ====
.site <- file.path(Sys.getenv("CONDA_PREFIX"), "lib", "R", "library")
.user <- Sys.getenv("R_LIBS_USER")
.libPaths(unique(c(.user, .site, .libPaths())))
options(repos = c(CRAN = "https://cloud.r-project.org"))
# ===================
EOF

# Apply isolation every time this conda env is activated
grep -q "R_ENVIRON_USER" "$ACTIVATE_FILE" 2>/dev/null || cat >> "$ACTIVATE_FILE" <<'EOF'
# ==== R environment config  ====
export R_ENVIRON_USER="$CONDA_PREFIX/.Renviron"
export R_PROFILE_USER="$CONDA_PREFIX/.Rprofile"
# ===============================
EOF

grep -q "unset R_ENVIRON_USER" "$DEACTIVATE_FILE" 2>/dev/null || cat >> "$DEACTIVATE_FILE" <<'EOF'
# ==== Clean R environment config ====
unset R_ENVIRON_USER
unset R_PROFILE_USER
# ====================================
EOF

echo "R environment configuration completed!"

# ===== Install R packages =====
echo "Installing BiocManager..."
Rscript -e 'if (!requireNamespace("BiocManager", quietly = TRUE)) install.packages("BiocManager")'

echo "Configuring Bioconductor version..."
Rscript -e '
  r_ver <- as.numeric(paste(R.version$major, strsplit(R.version$minor, "\\.")[[1]][1], sep = "."))
  bioc_ver <- if (r_ver >= 4.5) "3.22" else "3.20"
  BiocManager::install(version = bioc_ver, ask = FALSE)
'

echo "Installing preprocessCore (source build, threading disabled)..."
Rscript -e 'BiocManager::install(
              "preprocessCore",
              configure.args = c(preprocessCore = "--disable-threading"),
              type = "source",
              force = TRUE
            )'

echo "Installing Bioconductor annotation package..."
Rscript -e 'BiocManager::install(
    c(
      "rGREAT",
      "TxDb.Hsapiens.UCSC.hg38.knownGene",
      "TxDb.Mmusculus.UCSC.mm10.knownGene",
      "org.Hs.eg.db",
      "org.Mm.eg.db"
    ),
    update = FALSE,
    ask = FALSE
  )
'

echo "Installing remotes..."
Rscript -e 'if (!requireNamespace("remotes", quietly = TRUE)) install.packages("remotes")'

echo "Installing multiEpiCore from GitHub..."
Rscript -e 'options(Ncpus = parallel::detectCores());
            Sys.setenv(MAKEFLAGS = paste0("-j", parallel::detectCores()));
            remotes::install_github(
              "Qingjie-Yu/multiEpiCore",
              dependencies = TRUE,
              upgrade = "never",
              build_vignettes = FALSE
            )'

echo
echo "==============================================================="
echo "  Setup completed successfully."
echo "==============================================================="
echo
echo "Next steps:"
echo "  1. conda activate $env_name"
if [ "${NEEDS_KEY:-0}" = "1" ]; then
echo "  2. Add your LLM API key to  $CONFIG_FILE  (field: llm.api_key)"
else
echo "  2. Check your LLM API key in  $CONFIG_FILE  (field: llm.api_key)"
fi
echo "  3. Start the web interface:"
echo "         cd chromapilot/interface && ./run_interface.sh"
echo "     ...or run a request from the command line:"
echo "         python chromapilot/agent.py -r examples/hiplex_preprocessing.yaml"
echo
echo "Reference genomes are downloaded automatically the first time a run"
echo "needs them (~4 GB per genome, into ./ref). See docs/installation.md."
echo

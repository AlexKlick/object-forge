#!/usr/bin/env bash
set -euo pipefail

# This is a starting point, not a universal one-click installer.
# Heavy providers often need CUDA/PyTorch versions aligned to your machine.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_DIR="${ROOT_DIR}/vendor"
VENV_DIR="${ROOT_DIR}/.venvs"

mkdir -p "${VENDOR_DIR}" "${VENV_DIR}"

clone_if_missing () {
  local repo_url="$1"
  local dest="$2"
  if [[ ! -d "${dest}" ]]; then
    git clone "${repo_url}" "${dest}"
  fi
}

create_venv () {
  local name="$1"
  local path="${VENV_DIR}/${name}"
  if [[ ! -d "${path}" ]]; then
    python3 -m venv "${path}"
  fi
}

echo "Cloning provider repos..."
clone_if_missing https://github.com/IDEA-Research/Grounded-SAM-2.git "${VENDOR_DIR}/Grounded-SAM-2"
clone_if_missing https://github.com/microsoft/TRELLIS.git "${VENDOR_DIR}/TRELLIS"
clone_if_missing https://github.com/microsoft/TRELLIS.2.git "${VENDOR_DIR}/TRELLIS.2"
clone_if_missing https://github.com/wgsxm/PartCrafter.git "${VENDOR_DIR}/PartCrafter"
clone_if_missing https://github.com/VAST-AI-Research/TripoSR.git "${VENDOR_DIR}/TripoSR"
clone_if_missing https://github.com/TencentARC/InstantMesh.git "${VENDOR_DIR}/InstantMesh"

echo "Creating venvs..."
for name in grounded_sam2 trellis trellis2 partcrafter triposr instantmesh; do
  create_venv "${name}"
done

cat <<'EOF'

Next steps:
1. Activate each provider venv separately.
2. Install PyTorch/CUDA for your hardware.
3. Install the corresponding repo into that env.
4. Wire the python_bin and repo_dir paths into configs/app.example.yaml.

Examples:

  source .venvs/trellis2/bin/activate
  pip install --upgrade pip
  pip install torch torchvision
  pip install -e vendor/TRELLIS.2

  source .venvs/grounded_sam2/bin/activate
  pip install --upgrade pip
  pip install torch torchvision torchaudio
  pip install -e vendor/Grounded-SAM-2
  pip install --no-build-isolation -e vendor/Grounded-SAM-2/grounding_dino

EOF

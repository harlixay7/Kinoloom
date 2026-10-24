#!/usr/bin/env bash
# videolab bootstrap: create a venv, install dependencies, optionally wire the
# custom nodes and the downloaded models into an existing ComfyUI install.
#
# Untested on Linux: there is no record of this script being run on Linux.
#
# Usage:
#   ./install.sh
#   ./install.sh --cpu-only
#   ./install.sh --comfy-ui "/path/to/ComfyUI"
#   ./install.sh --comfy-ui "/path/to/ComfyUI" --force
#   ./install.sh --comfy-ui "/path/to/ComfyUI" --write-model-paths
#   ./install.sh --comfy-ui "/path/to/ComfyUI" --write-model-paths --models-dir "/data/videolab/models"
#
# --cpu-only installs only the tooling needed for `videolab.py doctor`,
# `download`, and the offline parts of `selftest` (no PyTorch).
# --comfy-ui copies nodes/ (first-party and vendored third-party, licenses
# included) into <ComfyUI>/custom_nodes and prints the model-wiring options.
# A node pack folder that already exists there is skipped; --force replaces it.
# --write-model-paths writes (only when absent) <ComfyUI>/extra_model_paths.yaml
# so ComfyUI can read the videolab models tree; --models-dir overrides the
# default <repo>/models base path. See README.md, "4b. Point ComfyUI to
# downloaded models".

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
COMFY_UI=""
CPU_ONLY=0
MODELS_DIR=""
WRITE_MODEL_PATHS=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cpu-only|-CpuOnly)
      CPU_ONLY=1
      shift
      ;;
    --comfy-ui|--comfy|-ComfyUI)
      COMFY_UI="$2"
      shift 2
      ;;
    --models-dir)
      MODELS_DIR="$2"
      shift 2
      ;;
    --write-model-paths)
      WRITE_MODEL_PATHS=1
      shift
      ;;
    --force|-Force)
      FORCE=1
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--cpu-only] [--comfy-ui <path>] [--force] [--write-model-paths] [--models-dir <path>]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ${WRITE_MODEL_PATHS} -eq 1 && -z "${COMFY_UI}" ]]; then
  echo "--write-model-paths requires --comfy-ui <path>." >&2
  exit 1
fi

find_python() {
  local candidates=("python3.12" "python3.13" "python3" "python")
  for cand in "${candidates[@]}"; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >/dev/null 2>&1; then
        echo "$cand"
        return 0
      fi
    fi
  done
  echo "Python 3.12+ was not found on PATH. Please install Python 3.12+ and re-run." >&2
  return 1
}

PY_BIN="$(find_python)"
echo -e "\033[0;36m[videolab] using ${PY_BIN}\033[0m"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[videolab] creating venv at ${VENV_DIR}"
  "${PY_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PY="${VENV_DIR}/bin/python"
if [[ ! -f "${VENV_PY}" ]]; then
  # Fallback for Windows-created venvs when executed under WSL/Git-Bash
  VENV_PY="${VENV_DIR}/Scripts/python.exe"
fi

"${VENV_PY}" -m pip install --upgrade pip

if [[ ${CPU_ONLY} -eq 1 ]]; then
  echo "[videolab] installing CPU-only tooling (psutil, Pillow, huggingface_hub)"
  "${VENV_PY}" -m pip install psutil Pillow huggingface_hub
else
  echo "[videolab] installing requirements.txt (torch cu130 + comfy-kitchen included)"
  "${VENV_PY}" -m pip install -r "${ROOT_DIR}/requirements.txt"
fi

if [[ -n "${COMFY_UI}" ]]; then
  if [[ ! -d "${COMFY_UI}" ]]; then
    echo "ComfyUI directory not found: ${COMFY_UI}" >&2
    exit 1
  fi
  CUSTOM_NODES="${COMFY_UI}/custom_nodes"
  if [[ ! -d "${CUSTOM_NODES}" ]]; then
    echo "Not a ComfyUI install (custom_nodes missing): ${COMFY_UI}" >&2
    exit 1
  fi

  for pack_dir in "${ROOT_DIR}/nodes"/*; do
    if [[ -d "${pack_dir}" ]]; then
      pack_name="$(basename "${pack_dir}")"
      if [[ "${pack_name}" != "third_party" && "${pack_name}" != "__pycache__" ]]; then
        dest="${CUSTOM_NODES}/${pack_name}"
        if [[ -e "${dest}" && ${FORCE} -eq 0 ]]; then
          echo "[videolab] ${dest} already exists; skipping it (re-run with --force to replace it)"
          continue
        fi
        echo "[videolab] copying first-party node pack ${pack_name} -> ${dest}"
        rm -rf "${dest}"
        cp -r "${pack_dir}" "${dest}"
      fi
    fi
  done

  if [[ -d "${ROOT_DIR}/nodes/third_party" ]]; then
    for pack_dir in "${ROOT_DIR}/nodes/third_party"/*; do
      if [[ -d "${pack_dir}" ]]; then
        pack_name="$(basename "${pack_dir}")"
        dest="${CUSTOM_NODES}/${pack_name}"
        if [[ -e "${dest}" && ${FORCE} -eq 0 ]]; then
          echo "[videolab] ${dest} already exists; skipping it (re-run with --force to replace it)"
          continue
        fi
        echo "[videolab] copying vendored node pack ${pack_name} -> ${dest}"
        rm -rf "${dest}"
        cp -r "${pack_dir}" "${dest}"
      fi
    done
  fi

  echo ""
  echo -e "\033[0;33m[videolab] Node packs copied (first-party + vendored third-party; licenses included; existing folders skipped unless --force).\033[0m"
  echo "  H3 workflows: ComfyUI v0.36.0 core provides the MiniMax-H3 nodes"
  echo "  (MiniMaxH3ReferenceToVideo / AddGuide / SigmaShift, ModelAttentionBackend are core)."
  echo "  The vendored LBH-123-AI upscaler pack (MIT) supplies MinimaxH3LatentUpscaler3D,"
  echo "  MMH3SpatialSplitParamsV10 and MMH3SplitUpscale."
  echo "  LTX workflows: ComfyUI v0.36.0 core plus the vendored LiconStudio MSR pack v1.2.2"
  echo "  (Apache-2.0; lab-notes commit edec572)."
  echo "  Qwen workflows: the vendored comfyui_qwen21_optimized pack on ComfyUI v0.37.4."
  echo ""
  echo "  Workflow templates to submit are in: ${ROOT_DIR}/workflows"
  echo ""

  if [[ -z "${MODELS_DIR}" ]]; then
    MODELS_DIR="${ROOT_DIR}/models"
  fi
  MODEL_PATHS_SNIPPET="videolab:
  base_path: ${MODELS_DIR}
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
  loras: loras
  latent_upscale_models: latent_upscale_models
  upscale_models: upscale_models
  prompt_generator: prompt_generator"

  if [[ ${WRITE_MODEL_PATHS} -eq 1 ]]; then
    YAML_PATH="${COMFY_UI}/extra_model_paths.yaml"
    if [[ -f "${YAML_PATH}" ]]; then
      echo "[videolab] ${YAML_PATH} already exists; not modifying it. Add this section manually:"
      echo "${MODEL_PATHS_SNIPPET}"
    else
      printf '%s\n' "${MODEL_PATHS_SNIPPET}" > "${YAML_PATH}"
      echo -e "\033[0;36m[videolab] wrote model paths to ${YAML_PATH} (restart ComfyUI to apply)\033[0m"
    fi
  else
    echo "[videolab] ComfyUI must see the downloaded weights. Either:"
    echo "  - set VIDEOLAB_MODELS_DIR to '${COMFY_UI}/models' before running videolab.py download, or"
    echo "  - add this section to ${COMFY_UI}/extra_model_paths.yaml (or re-run with --write-model-paths):"
    echo "${MODEL_PATHS_SNIPPET}"
  fi
fi

echo ""
echo -e "\033[0;36m[videolab] next steps:\033[0m"
echo "  ${VENV_PY} videolab.py doctor"
echo "  ${VENV_PY} videolab.py download --profile smoke"
echo "  ${VENV_PY} videolab.py selftest"
echo "  ${VENV_PY} videolab.py bench --model ltx25 --comfy http://127.0.0.1:8188 --set PROMPT=\"a weathered wooden pier at blue hour, low camera push-in\" --set REF_IMAGE_1=character.png --set REF_IMAGE_2=prop.png --set REF_BACKGROUND=environment.png --set SHOT_ID=demo"

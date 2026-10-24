#!/usr/bin/env bash
# Install into the existing project environment and expose the console command.
set -euo pipefail
subocr_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
subocr_bin="${1:-$HOME/.local/bin}"
subocr_python="${SUBOCR_PYTHON:-$subocr_root/.venv/bin/python}"
if [[ ! -x "$subocr_python" ]]; then
  echo 'Create .venv and install requirements.txt first, or set SUBOCR_PYTHON.' >&2
  exit 1
fi
if [[ -e "$subocr_bin/subocr" || -L "$subocr_bin/subocr" ]]; then
  echo "A command already exists at $subocr_bin/subocr; choose a different directory or update it yourself." >&2
  exit 1
fi
subocr_install_args=(--no-deps)
if [[ "${SUBOCR_OFFLINE_BUILD:-0}" == 1 ]]; then
  subocr_install_args+=(--no-build-isolation)
fi
"$subocr_python" -m pip install "${subocr_install_args[@]}" "$subocr_root"
mkdir -p -- "$subocr_bin"
subocr_env_bin="$(dirname -- "$subocr_python")"
ln -s -- "$subocr_env_bin/subocr" "$subocr_bin/subocr"
echo "Installed $subocr_bin/subocr. Ensure $subocr_bin is on PATH."

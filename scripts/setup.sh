#!/usr/bin/env bash
# Sleeper Cell — one-shot local setup.
#
# Preferred: uv (https://docs.astral.sh/uv/).
#   Install once with `brew install uv`. uv bundles its own Python (no Homebrew
#   Python required, no system-libexpat conflicts), reads pyproject.toml, and
#   installs everything in seconds.
#
# Fallback: system python3 + venv + pip. Only used if uv isn't on PATH.
#
# Usage:  bash scripts/setup.sh

set -euo pipefail

# Move to repo root (the directory containing this script's parent).
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$REPO_ROOT"

echo "Sleeper Cell setup — working dir: $REPO_ROOT"
echo

# ----------- Preferred path: uv -----------
if command -v uv >/dev/null 2>&1; then
  echo "→ Using uv ($(uv --version))"
  echo "→ Syncing project (creates .venv if missing, installs deps from pyproject.toml) ..."
  uv sync
else
  # ----------- Fallback: system Python -----------
  echo "→ uv not found. Install uv for faster/more reliable setup:  brew install uv"
  echo "  Falling back to system Python + venv ..."
  echo

  PYTHON=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      version="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      case "$version" in
        3.11|3.12|3.13) PYTHON="$candidate"; break ;;
        *) [ -z "$PYTHON" ] && PYTHON="$candidate" ;;
      esac
    fi
  done

  if [ -z "$PYTHON" ]; then
    echo "ERROR: no python3 found. Install uv (brew install uv) or Python 3.12+ (brew install python@3.12)."
    exit 1
  fi
  echo "→ Using $PYTHON ($($PYTHON --version))"

  if [ ! -d ".venv" ]; then
    if ! "$PYTHON" -m venv .venv 2>/dev/null; then
      echo "  ensurepip failed — bootstrapping pip via get-pip.py."
      "$PYTHON" -m venv .venv --without-pip
      curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
    fi
  fi
  .venv/bin/python -m pip install --upgrade pip --quiet
  .venv/bin/pip install -r requirements.txt --quiet
fi

# ----------- Common steps -----------

# Copy secrets template if not already done.
if [ ! -f ".streamlit/secrets.toml" ] && [ -f ".streamlit/secrets.toml.example" ]; then
  cp .streamlit/secrets.toml.example .streamlit/secrets.toml
  echo "→ Copied .streamlit/secrets.toml.example → .streamlit/secrets.toml"
fi

cat <<EOF

✓ Setup complete.

To run the app:
    uv run streamlit run app.py             # if you have uv
    source .venv/bin/activate && streamlit run app.py   # otherwise

Optional: pre-warm caches first (~13 MB across Sleeper + DynastyProcess):
    uv run python scripts/refresh_data.py
EOF

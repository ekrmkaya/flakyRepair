#!/usr/bin/env bash
# Install Python dependencies for both pipeline stages.
#
# Usage:
#   bash install.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Installing Stage 1 dependencies (failure_data_collection)..."
pip install -r "$REPO_ROOT/failure_data_collection/requirements.txt"

echo ""
echo "Installing Stage 2 dependencies (openai_patch_evaluation)..."
pip install -r "$REPO_ROOT/openai_patch_evaluation/requirements.txt"

echo ""
echo "Done. All Python dependencies installed."

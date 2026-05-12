#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip
pip install --upgrade setuptools wheel
pip install -e . --no-build-isolation

if [[ ! -f config.toml ]]; then
  telegram-ai-bridge init --config config.toml
  echo "Created config.toml. Edit it before running the service."
fi

echo
echo "Running doctor..."
telegram-ai-bridge doctor --config config.toml || true

echo
echo "Install complete."
echo "Your current shell is not automatically activated after this script exits."
echo "Use one of the following:"
echo "  1) Direct venv binary:"
echo "     ./.venv/bin/telegram-ai-bridge run --config config.toml"
echo "  2) Activate then run:"
echo "     source .venv/bin/activate"
echo "     telegram-ai-bridge run --config config.toml"

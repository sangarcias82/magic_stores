#!/usr/bin/env bash
# Build a clean project archive (no .git / venv / secrets).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/magic-stores.tar.gz}"
cd "$ROOT"
tar --exclude='.git' --exclude='venv' --exclude='.venv' \
    --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='*.db' --exclude='.env' --exclude='*.pyc' \
    --exclude='.DS_Store' \
    -czf "$OUT" \
    app tests requirements.txt README.md LICENSE .gitignore .env.example pytest.ini scripts fixtures
echo "Wrote $OUT"
tar -tzf "$OUT" | head -n 30

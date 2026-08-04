#!/bin/sh
# Install the repo hooks into .git/hooks (which git does not track).
set -e
cd "$(dirname "$0")/../.."
cp tools/hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
echo "installed .git/hooks/pre-commit"

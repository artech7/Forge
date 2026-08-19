#!/usr/bin/env bash
# Gets this folder ready to push to GitHub.
#
#   ./prep-repo.sh
#
# Writes a .gitignore covering everything that shouldn't be uploaded, then
# lists what actually would be. It doesn't commit or push anything, and it
# never deletes your media, database or test files — it just stops them
# being included.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f server/app.py ] || [ ! -f worker/agent.py ]; then
  echo "This doesn't look like the Forge folder."
  echo "Run it from the folder containing server/ and worker/."
  exit 1
fi

echo "Preparing $(pwd)"
echo

# ---------------------------------------------------------------- ignore

cat > .gitignore <<'EOF'
# Python
__pycache__/
*.py[cod]
.venv/
.venv-node/
venv/

# Forge's own data — your libraries, settings and history live here.
# This must never be uploaded: it's yours, and it would overwrite other
# people's settings if they ever pulled it.
server/data/
data/

# Test material and anything Forge produced while running
testmedia/
testlib/
moved/
Movies/
TV/
Originals/
*.mkv
*.mp4
*.m4v
*.avi
*.ts
*.m2ts

# Release archives
forge.zip
*.zip

# Editors and operating systems
.DS_Store
Thumbs.db
.idea/
.vscode/
*.swp
EOF

echo "Wrote .gitignore"

# ------------------------------------------------------------ tidy up

removed=0
for junk in server/__pycache__ worker/__pycache__ __pycache__; do
  if [ -d "$junk" ]; then rm -rf "$junk"; removed=$((removed + 1)); fi
done
find . -name '.DS_Store' -delete 2>/dev/null || true
[ "$removed" -gt 0 ] && echo "Removed $removed cache folder(s)"

# ------------------------------------------------------- what gets sent

echo
echo "These files will be uploaded:"
echo

if command -v git >/dev/null && [ -d .git ]; then
  git add -A --dry-run 2>/dev/null | sed 's/^add /  /' | sort
else
  # Same exclusions as the .gitignore above, for a first-time run before
  # git exists here.
  find . -type f \
    -not -path './.git/*' \
    -not -path './.venv*' \
    -not -path './server/data/*' \
    -not -path './testmedia/*' -not -path './testlib/*' \
    -not -path './moved/*' -not -path './Movies/*' \
    -not -path './TV/*' -not -path './Originals/*' \
    -not -name '*.pyc' -not -path '*__pycache__*' \
    -not -name '*.zip' -not -name '.DS_Store' \
    -not -name '*.mkv' -not -name '*.mp4' -not -name '*.m4v' \
    -not -name '*.avi' -not -name '*.ts' -not -name '*.m2ts' \
    | sed 's|^\./|  |' | sort
fi

echo
echo "Left out (staying on this machine only):"
for path in .venv .venv-node server/data testmedia testlib moved Movies TV Originals; do
  if [ -e "$path" ]; then
    size=$(du -sh "$path" 2>/dev/null | cut -f1)
    echo "  $path  ($size)"
  fi
done
if ls ./*.zip >/dev/null 2>&1; then
  echo "  $(ls ./*.zip | tr '\n' ' ')"
fi

# --------------------------------------------------- sanity before push

echo
problem=0
big=$(find . -type f -size +40M -not -path './.git/*' -not -path './.venv*' \
      -not -path './server/data/*' -not -path './testmedia/*' \
      -not -name '*.zip' 2>/dev/null || true)
if [ -n "$big" ]; then
  echo "Warning — these are large and probably shouldn't be uploaded:"
  echo "$big" | sed 's|^\./|  |'
  problem=1
fi

if [ -f server/data/forge.db ]; then
  echo "Your database (server/data/forge.db) is excluded, as it should be."
fi

echo
if [ "$problem" = "0" ]; then
  echo "Ready. If this is the first time:"
  echo
  echo "  git init"
  echo "  git add ."
  echo "  git commit -m \"Forge\""
  echo "  git branch -M main"
  echo "  git remote add origin https://github.com/YOURNAME/forge.git"
  echo "  git push -u origin main"
  echo
  echo "After that, to send later changes:"
  echo
  echo "  git add -A && git commit -m \"what changed\" && git push"
else
  echo "Look at the warnings above before pushing."
fi

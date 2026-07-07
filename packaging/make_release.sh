#!/bin/zsh
# Build the distributable zip: packaging/make_release.sh
# Output: dist/RawConvert-<version>.zip (+ SHA-256)
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(/usr/bin/python3 -c "import rawconvert; print(rawconvert.__version__)")
NAME="RawConvert-$VERSION"
STAGE="dist/$NAME"

rm -rf "$STAGE" "dist/$NAME.zip"
mkdir -p "$STAGE/gui"

cp rawconvert.py rawconvert_gui.py RawConvert.command \
   README.md FORMATS.md CHANGELOG.md LICENSE "$STAGE/"
cp gui/index.html "$STAGE/gui/"
chmod +x "$STAGE/RawConvert.command"

# ditto preserves the launcher's executable bit (Finder-unzip safe)
ditto -c -k --keepParent "$STAGE" "dist/$NAME.zip"

echo "Built dist/$NAME.zip"
shasum -a 256 "dist/$NAME.zip"

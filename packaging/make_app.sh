#!/bin/zsh
# Build the self-contained RawConvert.app with PyInstaller.
# Usage: packaging/make_app.sh [--sign "Developer ID Application: Name (TEAM)"]
# See packaging/APP_NOTES.md for the full signing + notarization flow.
set -euo pipefail
cd "$(dirname "$0")/.."

SIGN_ID="${2:-}"
VERSION=$(/usr/bin/python3 -c "import rawconvert; print(rawconvert.__version__)")

# Build-machine-only dependency; end users never need Python packages.
if [ ! -x build/venv/bin/pyinstaller ]; then
  /usr/bin/python3 -m venv build/venv
  build/venv/bin/pip -q install --upgrade pip pyinstaller
fi

# Optional: bundle exiftool (redistributable under the Artistic License).
# Place the standalone exiftool directory at packaging/vendor/exiftool
# and it ships inside the app; otherwise the app uses one from PATH.
VENDOR_ARGS=()
if [ -e packaging/vendor/exiftool ]; then
  VENDOR_ARGS=(--add-data "packaging/vendor/exiftool:vendor")
fi

build/venv/bin/pyinstaller --noconfirm --clean --windowed --onedir \
  --name RawConvert \
  --osx-bundle-identifier com.luisjuarez.rawconvert \
  --add-data "gui/index.html:gui" \
  "${VENDOR_ARGS[@]}" \
  rawconvert_gui.py

APP="dist/RawConvert.app"
echo "Built $APP (version $VERSION)"

if [ -n "$SIGN_ID" ]; then
  codesign --deep --force --options runtime --timestamp \
    --sign "$SIGN_ID" "$APP"
  codesign --verify --deep --strict "$APP"
  ditto -c -k --keepParent "$APP" "dist/RawConvert-$VERSION-app.zip"
  echo "Signed. Next: notarize (see packaging/APP_NOTES.md):"
  echo "  xcrun notarytool submit dist/RawConvert-$VERSION-app.zip \\"
  echo "    --keychain-profile rawconvert-notary --wait"
  echo "  xcrun stapler staple $APP"
else
  echo "UNSIGNED build — fine for local testing, not for distribution."
fi

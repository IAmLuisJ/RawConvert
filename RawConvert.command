#!/bin/zsh
# Double-click to open RawConvert in your browser.
cd "$(dirname "$0")"

# One-time setup on a brand-new Mac: python3 needs Apple's free
# Command Line Tools. Explain before macOS shows its installer.
if ! xcode-select -p >/dev/null 2>&1; then
  osascript -e 'display dialog "RawConvert needs a small free Apple component (the Command Line Tools).\n\nmacOS will now offer to install it. Click Install, wait for it to finish, then double-click RawConvert.command again." buttons {"OK"} default button 1 with title "RawConvert — one-time setup"' >/dev/null
  xcode-select --install
  exit 0
fi

exec /usr/bin/python3 rawconvert_gui.py

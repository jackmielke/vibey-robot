#!/bin/zsh
# Build, ad-hoc sign, install to ~/Applications/Vibey.app, and launch.
set -euo pipefail
cd "$(dirname "$0")"
BUILD=build; APP="$BUILD/Vibey.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -o "$APP/Contents/MacOS/Vibey" Sources/*.swift \
  -framework Cocoa -framework WebKit -target arm64-apple-macos13.0
cp Info.plist "$APP/Contents/Info.plist"

if [[ ! -f "$BUILD/AppIcon.icns" ]]; then
  ICONSET="$BUILD/AppIcon.iconset"; mkdir -p "$ICONSET"
  swiftc -o "$BUILD/make_icon" make_icon.swift -framework Cocoa
  for s in 16 32 128 256 512; do
    "$BUILD/make_icon" $s "$ICONSET/icon_${s}x${s}.png"
    "$BUILD/make_icon" $((s*2)) "$ICONSET/icon_${s}x${s}@2x.png"
  done
  iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"
fi
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

codesign --force --deep -s - "$APP"

DEST="$HOME/Applications/Vibey.app"
pkill -x Vibey 2>/dev/null || true
rm -rf "$DEST"; cp -R "$APP" "$DEST"
[[ "${1:-}" == "--no-launch" ]] || open "$DEST"
echo "installed $DEST"

#!/bin/zsh
# Build, ad-hoc sign, install to ~/Applications/Vibey.app, and launch.
set -euo pipefail
cd "$(dirname "$0")"
BUILD=build; APP="$BUILD/Vibey.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -o "$APP/Contents/MacOS/Vibey" Sources/*.swift \
  -framework Cocoa -framework WebKit -target arm64-apple-macos13.0
cp Info.plist "$APP/Contents/Info.plist"

# The icon is Vibey itself (icon/vibey-icon.svg, rendered to icon/icon-1024.png).
ICONSET="$BUILD/AppIcon.iconset"; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s icon/icon-1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) icon/icon-1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

codesign --force --deep -s - "$APP"

DEST="$HOME/Applications/Vibey.app"
pkill -x Vibey 2>/dev/null || true
rm -rf "$DEST"; cp -R "$APP" "$DEST"
[[ "${1:-}" == "--no-launch" ]] || open "$DEST"
echo "installed $DEST"

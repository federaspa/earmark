#!/bin/sh
# Build bin/AudioRec.app (the audiorec capture tool) and sign it ad hoc with a stable identity.
#
# macOS keys privacy permissions to the code signature. An ad-hoc signature's default designated
# requirement is its cdhash, which changes on every build; an explicit requirement on the bundle
# identifier keeps the permissions across rebuilds.
set -eu
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
APP="$ROOT/bin/AudioRec.app"
ID=io.github.earmark.audiorec

mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
xcrun swiftc -O -swift-version 5 -target arm64-apple-macos13.0 \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist \
    -o "$APP/Contents/MacOS/audiorec" main.swift
codesign --force --sign - --identifier "$ID" \
    --requirements "=designated => identifier \"$ID\"" "$APP"

# Fixed CLI entry point.
cat > "$ROOT/bin/audiorec" <<SH
#!/bin/sh
exec "$APP/Contents/MacOS/audiorec" "\$@"
SH
chmod +x "$ROOT/bin/audiorec"
codesign -dr - "$APP" 2>&1 | grep designated
echo "built $APP"

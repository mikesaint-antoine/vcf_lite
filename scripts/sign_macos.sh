#!/usr/bin/env bash
# Sign a macOS app bundle with a Developer ID (hardened runtime), inside out.
#   MACOS_SIGN_IDENTITY="Developer ID Application: …" [KEYCHAIN=path] scripts/sign_macos.sh "dist/VCF Lite.app"
set -euo pipefail
cd "$(dirname "$0")/.."
APP=${1:-"dist/VCF Lite.app"}
: "${MACOS_SIGN_IDENTITY:?set MACOS_SIGN_IDENTITY}"
KC=(); [ -n "${KEYCHAIN:-}" ] && KC=(--keychain "$KEYCHAIN")
sign() { codesign --force --timestamp --options runtime "${KC[@]}" --sign "$MACOS_SIGN_IDENTITY" "$@"; }

# Every Mach-O binary inside the bundle first (libraries, Python, extensions)…
n=0
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then sign "$f"; n=$((n + 1)); fi
done < <(find "$APP/Contents" -type f ! -path "*/Contents/MacOS/*" -print0)
echo "signed $n embedded binaries"
# …then the app itself (this also signs the main executable) with entitlements.
sign --entitlements packaging/macos/entitlements.plist "$APP"
codesign --verify --deep --strict --verbose=1 "$APP"

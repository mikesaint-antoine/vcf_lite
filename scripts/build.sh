#!/usr/bin/env bash
# Build the macOS app into dist/ (Windows uses PyInstaller + Inno Setup and
# Linux uses scripts/build_deb.py; see .github/workflows/build.yml).
#   dist/VCF Lite.app  and  dist/VCF-Lite-<ver>-macOS.dmg
#
# Optional signing / notarization (used by CI; unsigned if not set):
#   MACOS_SIGN_IDENTITY  "Developer ID Application: Name (TEAMID)"  -> sign app + dmg
#   KEYCHAIN             keychain holding that identity (optional)
#   APPLE_ID, APPLE_APP_PASSWORD, APPLE_TEAM_ID  -> notarize + staple app and dmg
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python}
VER=$($PY -c "import vcflite; print(vcflite.__version__)")
$PY -m PyInstaller packaging/vcflite.spec --noconfirm --clean --distpath dist --workpath build
[[ "$(uname)" == "Darwin" ]] || exit 0

APP="dist/VCF Lite.app"
DMG="dist/VCF-Lite-$VER-macOS.dmg"
SIGN=${MACOS_SIGN_IDENTITY:-}
NOTARIZE=""
[[ -n "$SIGN" && -n "${APPLE_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]] && NOTARIZE=1

# Submit a file to Apple's notary service and wait; show Apple's log on failure.
notarize() {
  local out id status
  out=$(xcrun notarytool submit "$1" --apple-id "$APPLE_ID" --password "$APPLE_APP_PASSWORD" \
        --team-id "$APPLE_TEAM_ID" --wait --timeout 60m --output-format json)
  id=$(printf '%s' "$out" | $PY -c "import json,sys; print(json.load(sys.stdin).get('id',''))")
  status=$(printf '%s' "$out" | $PY -c "import json,sys; print(json.load(sys.stdin).get('status',''))")
  echo "notarization of $(basename "$1"): $status (submission $id)"
  if [[ "$status" != "Accepted" ]]; then
    [[ -n "$id" ]] && xcrun notarytool log "$id" --apple-id "$APPLE_ID" \
      --password "$APPLE_APP_PASSWORD" --team-id "$APPLE_TEAM_ID" || true
    exit 1
  fi
}

if [[ -n "$SIGN" ]]; then
  ./scripts/sign_macos.sh "$APP"
  if [[ -n "$NOTARIZE" ]]; then
    # Notarize the app itself and staple the ticket to it, so it also opens
    # cleanly after being copied out of the dmg while offline.
    ditto -c -k --keepParent "$APP" dist/app-for-notary.zip
    notarize dist/app-for-notary.zip
    rm dist/app-for-notary.zip
    xcrun stapler staple "$APP"
  fi
fi

rm -f "$DMG"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "VCF Lite" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

if [[ -n "$SIGN" ]]; then
  KC=(); [[ -n "${KEYCHAIN:-}" ]] && KC=(--keychain "$KEYCHAIN")
  codesign --force --timestamp "${KC[@]}" --sign "$SIGN" "$DMG"
  if [[ -n "$NOTARIZE" ]]; then
    notarize "$DMG"
    xcrun stapler staple "$DMG"
  fi
fi
echo "Built $APP and $DMG${SIGN:+ (signed${NOTARIZE:+ + notarized})}"

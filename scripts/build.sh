#!/usr/bin/env bash
# Build the macOS app into dist/ (Windows uses PyInstaller + Inno Setup and
# Linux uses scripts/build_deb.py; see .github/workflows/build.yml).
#   dist/VCF Lite.app  and  dist/VCF-Lite-<ver>-macOS.dmg
#
# Optional signing / notarization (used by CI; unsigned if not set):
#   MACOS_SIGN_IDENTITY  "Developer ID Application: Name (TEAMID)"  -> sign app + dmg
#   KEYCHAIN             keychain holding that identity (optional)
#   APPLE_ID, APPLE_APP_PASSWORD, APPLE_TEAM_ID  -> notarize + staple the dmg
#   NOTARY_WAIT_MINUTES  how long to wait for Apple, upload included (default 170)
#
# Notarizing the dmg also covers the app inside it (Gatekeeper looks the
# app's ticket up online on first launch), so there is one submission per
# build.  If Apple hasn't finished in time the script exits with code 3,
# leaving the signed dmg and dist/notary-submission.txt behind: once Apple
# accepts it, `xcrun stapler staple <dmg>` finishes the job, no resubmission.
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
NOTARY=(--apple-id "${APPLE_ID:-}" --password "${APPLE_APP_PASSWORD:-}" --team-id "${APPLE_TEAM_ID:-}")
json() { $PY -c "import json,sys; print(json.load(sys.stdin).get('$1',''))" 2>/dev/null || true; }

# Run a command, killing it after $1 seconds (macOS has no `timeout`).
limit() { local secs=$1; shift; perl -e 'alarm shift; exec @ARGV' "$secs" "$@"; }

# Submit to Apple's notary service, then poll its status. A stalled upload
# is killed after 15 minutes and retried; network hiccups while polling are
# retried rather than treated as failures.
notarize() {
  local file=$1 id="" status="" out deadline
  deadline=$(( $(date +%s) + ${NOTARY_WAIT_MINUTES:-170} * 60 ))
  for attempt in 1 2 3; do
    echo "$(date -u +%H:%M:%S) uploading $(basename "$file") to Apple (attempt $attempt)"
    out=$(limit 900 xcrun notarytool submit "$file" "${NOTARY[@]}" --no-wait --output-format json) && id=$(printf '%s' "$out" | json id)
    [[ -n "$id" ]] && break
    echo "submit failed or stalled (attempt $attempt), retrying in 30s"; sleep 30
  done
  [[ -n "$id" ]] || { echo "::error::could not submit $(basename "$file") to Apple"; exit 1; }
  echo "$id" > dist/notary-submission.txt
  echo "submitted $(basename "$file") to Apple: submission $id"
  while :; do
    status=$(limit 120 xcrun notarytool info "$id" "${NOTARY[@]}" --output-format json 2>/dev/null | json status)
    echo "$(date -u +%H:%M:%S) notarization: ${status:-(no answer, will retry)}"
    case "$status" in
      Accepted) return 0 ;;
      Invalid|Rejected)
        xcrun notarytool log "$id" "${NOTARY[@]}" || true
        exit 1 ;;
    esac
    if (( $(date +%s) > deadline )); then
      echo "::warning::Apple is still checking submission $id; the signed dmg is kept so it can be stapled later"
      exit 3
    fi
    sleep 30
  done
}

[[ -n "$SIGN" ]] && ./scripts/sign_macos.sh "$APP"

rm -f "$DMG" dist/notary-submission.txt
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

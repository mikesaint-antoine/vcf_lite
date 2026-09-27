#!/usr/bin/env bash
# Build a standalone app for the current OS into dist/.
#   macOS  -> dist/VCF Lite.app  (+ dist/VCF-Lite-<ver>-macOS.dmg)
#   Windows-> dist/VCF Lite/VCF Lite.exe (zip it or wrap with an installer)
#   Linux  -> dist/vcf-lite/vcf-lite
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python}
VER=$($PY -c "import vcflite; print(vcflite.__version__)")
$PY -m PyInstaller packaging/vcflite.spec --noconfirm --clean --distpath dist --workpath build
if [[ "$(uname)" == "Darwin" ]]; then
  rm -f "dist/VCF-Lite-$VER-macOS.dmg"
  STAGE=$(mktemp -d)
  cp -R "dist/VCF Lite.app" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "VCF Lite" -srcfolder "$STAGE" -ov -format UDZO "dist/VCF-Lite-$VER-macOS.dmg" >/dev/null
  rm -rf "$STAGE"
  echo "Built dist/VCF Lite.app and dist/VCF-Lite-$VER-macOS.dmg"
fi

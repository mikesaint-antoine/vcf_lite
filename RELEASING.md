# Releasing a new version

Everything is built on GitHub; nothing needs to be built locally.

1. Bump the version in `vcflite/__init__.py` (e.g. `__version__ = "0.2.0"`).
2. Commit and push to `main`. Wait for the **build** workflow to go green on the
   Actions tab: it tests the code and builds + self-tests the macOS, Windows and
   Linux apps (about 3 minutes).
3. Tag the commit and push the tag (the tag must match the version, with a `v`):

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

4. The workflow runs again and, because of the tag, signs **and notarizes** the macOS
   app with Apple (ordinary pushes only sign it; Apple's check can take a while),
   then publishes a GitHub Release
   with:
   - `VCF-Lite-macOS-arm64.dmg`, `VCF-Lite-macOS-x64.dmg`
   - `VCF-Lite-Windows-x64-Setup.exe`
   - `VCF-Lite-Linux.deb`
   - `SHA256SUMS.txt`
5. Nothing to do for the website: its download buttons point at
   `releases/latest/download/<file>`, and it reads the version number from the
   latest release.

Optionally edit the release notes on the Releases page (they're pre-filled from
the commits since the last release).

## If a build fails

Open the failed job on the Actions tab. Each platform's "Self-test" step opens
`tests/data/sample.vcf.gz` in the real app; if it fails, the app couldn't start
or show rows on that platform. On Windows the app also writes a log to
`%LOCALAPPDATA%\VCF Lite\log.txt` (macOS: `~/Library/Caches/VCF Lite/log.txt`).

## If Apple's notarization is slow

Each macOS build submits its signed `.dmg` to Apple and waits (up to about 3
hours, retrying through network hiccups). If Apple still hasn't finished, the
job fails but keeps the signed file as an artifact named
`unfinished-macos-<arch>` (the `.dmg` plus `notary-submission.txt` with Apple's
submission id). No need to rebuild or resubmit:

```bash
gh run download <run-id> -R mikesaint-antoine/vcf_lite -n unfinished-macos-arm64 -D unfinished
xcrun notarytool info "$(cat unfinished/notary-submission.txt)" --apple-id YOUR_APPLE_ID --team-id 93J2SRTJ3J
# when it says "Accepted":
xcrun stapler staple unfinished/VCF-Lite-*-macOS.dmg
```

The stapled `.dmg` can then be attached to the release as
`VCF-Lite-macOS-arm64.dmg` (or `-x64`).

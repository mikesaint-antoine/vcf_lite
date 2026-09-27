# Releasing a new version

Everything is built on GitHub; nothing needs to be built locally.

1. Bump the version in `vcflite/__init__.py` (e.g. `__version__ = "0.2.0"`).
2. Commit and push to `main`; the **build** workflow runs the tests. To also
   build and self-test every platform before tagging, start it by hand:
   Actions tab → **build** → **Run workflow**. (Pushes only run the tests while
   the repo is private, to save Actions minutes.)
3. Tag the commit and push the tag (the tag must match the version, with a `v`):

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

4. The workflow runs again and, because of the tag, publishes a GitHub Release
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

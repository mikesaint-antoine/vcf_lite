"""Build the Ubuntu/Debian package: dist/vcf-lite_<version>_all.deb

    python scripts/build_deb.py

Linux doesn't get a frozen (PyInstaller) build: bundling WebKitGTK is
fragile.  Instead the package ships our code plus pywebview's pure-Python
pieces and relies on the system's Python, GTK and WebKit, declared as
dependencies so `apt install ./vcf-lite_*.deb` pulls them in.

Pure Python (tarfile + a tiny ar writer), so it builds on any OS.
Needs pip (to fetch the vendored packages).
"""

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from vcflite import __version__  # noqa: E402

PKG = "vcf-lite"
LIB = f"usr/lib/{PKG}"
HOMEPAGE = "https://github.com/mikesaint-antoine/vcf_lite"
MAINTAINER = f"Mike Saint-Antoine ({HOMEPAGE})"
# Pure-Python runtime deps of pywebview on Linux (the GTK backend comes from
# the system's python3-gi).  Versions pinned for reproducible packages.
VENDOR = ["pywebview==6.2.1", "bottle==0.13.4", "proxy_tools==0.1.0", "typing_extensions==4.16.0"]
DEPENDS = ("python3 (>= 3.9), python3-gi, gir1.2-gtk-3.0, "
           "gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0")

LAUNCHER = f"""#!/bin/sh
# VCF Lite launcher (installed by the {PKG} package)
export PYTHONPATH="/{LIB}:/{LIB}/vendor${{PYTHONPATH:+:$PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 -m vcflite "$@"
"""

DESKTOP = f"""[Desktop Entry]
Type=Application
Name=VCF Lite
GenericName=VCF Viewer
Comment=A small, fast viewer for VCF files
Exec=vcf-lite %f
Icon={PKG}
Terminal=false
Categories=Science;Biology;Viewer;
Keywords=vcf;variant;genomics;bioinformatics;
StartupWMClass=VCF Lite
"""

COPYRIGHT = f"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: VCF Lite
Source: {HOMEPAGE}

Files: *
Copyright: 2026 Mike Saint-Antoine
License: MIT
""" + "\n".join(" " + (l if l.strip() else ".") for l in (ROOT / "LICENSE").read_text().splitlines()[2:]) + "\n"


def vendor(dest: Path):
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
                    "--no-compile", "--target", str(dest), *VENDOR], check=True)
    # Drop what Linux never uses.
    for junk in dest.glob("*.dist-info"):
        shutil.rmtree(junk)
    shutil.rmtree(dest / "bin", ignore_errors=True)
    shutil.rmtree(dest / "webview" / "__pyinstaller", ignore_errors=True)
    shutil.rmtree(dest / "webview" / "lib", ignore_errors=True)  # Windows DLLs, Android jar
    for p in ("cocoa.py", "winforms.py", "edgechromium.py", "mshtml.py", "android.py", "cef.py"):
        (dest / "webview" / "platforms" / p).unlink(missing_ok=True)


def icons(stage: Path):
    from PIL import Image
    src = Image.open(ROOT / "packaging" / "icon-tile.png")
    for s in (48, 64, 128, 256, 512):
        d = stage / f"usr/share/icons/hicolor/{s}x{s}/apps"
        d.mkdir(parents=True, exist_ok=True)
        src.resize((s, s), Image.LANCZOS).save(d / f"{PKG}.png")


def stage_tree(stage: Path):
    lib = stage / LIB
    shutil.copytree(ROOT / "vcflite", lib / "vcflite",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    vendor(lib / "vendor")
    (stage / "usr/bin").mkdir(parents=True)
    (stage / "usr/bin" / PKG).write_text(LAUNCHER)
    (stage / "usr/share/applications").mkdir(parents=True)
    (stage / "usr/share/applications" / f"{PKG}.desktop").write_text(DESKTOP)
    (stage / f"usr/share/doc/{PKG}").mkdir(parents=True)
    (stage / f"usr/share/doc/{PKG}/copyright").write_text(COPYRIGHT)
    icons(stage)


def tar_bytes(root: Path, executables=()) -> bytes:
    """Deterministic gzip'd tar of ``root`` owned by root:root."""
    buf = io.BytesIO()
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        def add(path: Path, arc: str):
            info = tar.gettarinfo(str(path), arcname=arc)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = mtime
            if info.isdir():
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.mode = 0o755 if arc in executables else 0o644
                with open(path, "rb") as f:
                    tar.addfile(info, f)
        add(root, ".")
        for p in sorted(root.rglob("*")):
            add(p, "./" + p.relative_to(root).as_posix())
    return buf.getvalue()


def ar(members) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        hdr = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        assert len(hdr) == 60
        out += hdr + data + (b"\n" if len(data) % 2 else b"")
    return bytes(out)


def main():
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()
        stage_tree(stage)
        size_kb = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file()) // 1024 + 1
        control_dir = Path(tmp) / "control"
        control_dir.mkdir()
        (control_dir / "control").write_text(
            f"Package: {PKG}\n"
            f"Version: {__version__}\n"
            "Architecture: all\n"
            f"Maintainer: {MAINTAINER}\n"
            f"Installed-Size: {size_kb}\n"
            f"Depends: {DEPENDS}\n"
            "Section: science\n"
            "Priority: optional\n"
            f"Homepage: {HOMEPAGE}\n"
            "Description: small, fast desktop viewer for VCF files\n"
            " Open multi-gigabyte .vcf / .vcf.gz files instantly, jump to any\n"
            " position or rsID, and read every record's fields in plain view.\n")
        deb = ar([
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", tar_bytes(control_dir)),
            ("data.tar.gz", tar_bytes(stage, executables={f"./usr/bin/{PKG}"})),
        ])
    out = dist / f"{PKG}_{__version__}_all.deb"
    out.write_bytes(deb)
    print(f"built {out} ({len(deb) // 1024} KB)")


if __name__ == "__main__":
    main()

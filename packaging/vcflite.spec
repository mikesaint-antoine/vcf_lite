# PyInstaller spec - builds "VCF Lite.app" on macOS, a folder with
# "VCF Lite.exe" on Windows, and a folder with "vcf-lite" on Linux.
#
#   pyinstaller packaging/vcflite.spec --noconfirm
import os
import sys

HERE = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from vcflite import __version__  # noqa: E402

block_cipher = None

a = Analysis(
    [os.path.join(HERE, "launcher.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "vcflite", "ui"), os.path.join("vcflite", "ui")),
        (os.path.join(ROOT, "vcflite", "data"), os.path.join("vcflite", "data")),
    ],
    hiddenimports=[],
    excludes=["tkinter", "unittest", "pydoc", "test", "PIL", "numpy", "playwright", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure, cipher=block_cipher)

name = "vcf-lite" if sys.platform.startswith("linux") else "VCF Lite"
icon = os.path.join(HERE, "icon.icns" if sys.platform == "darwin" else "icon.ico")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=name,
    console=False,
    argv_emulation=sys.platform == "darwin",  # turns Finder "Open With" into argv
    icon=icon,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=name, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="VCF Lite.app",
        icon=icon,
        bundle_identifier="org.vcflite.app",
        version=__version__,
        info_plist={
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSRequiresAquaSystemAppearance": False,
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Variant Call Format",
                    "CFBundleTypeRole": "Viewer",
                    "LSHandlerRank": "Alternate",
                    "CFBundleTypeExtensions": ["vcf", "gz", "bgz"],
                    "LSItemContentTypes": ["org.vcflite.vcf", "public.data"],
                }
            ],
            "UTExportedTypeDeclarations": [
                {
                    "UTTypeIdentifier": "org.vcflite.vcf",
                    "UTTypeDescription": "Variant Call Format",
                    "UTTypeConformsTo": ["public.plain-text", "public.data"],
                    "UTTypeTagSpecification": {"public.filename-extension": ["vcf"]},
                }
            ],
        },
    )

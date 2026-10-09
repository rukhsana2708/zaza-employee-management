# PyInstaller spec for ZaZaWorkAgent.exe (one folder, no console).
#
# Built by installer/build.ps1 in a dedicated environment that contains only
# the agent's runtime dependencies. Starts from ONE entry point and excludes
# every upstream DeskMate package and capture library explicitly
# (installer/build_config.py); installer/audit_package.py then inspects the
# result and fails the build on anything prohibited.
#
#   ZAZA_BUILD_FLAVOR=production   (default) no console window
#   ZAZA_BUILD_FLAVOR=development  console window + debug logging
import os
import sys
from pathlib import Path

HERE = Path(SPECPATH)
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from build_config import EXCLUDES, product_version, version_file_text  # noqa: E402

FLAVOR = os.environ.get("ZAZA_BUILD_FLAVOR", "production")
if FLAVOR not in ("production", "development"):
    raise SystemExit("ZAZA_BUILD_FLAVOR must be production or development")
VERSION = product_version()

work = Path(workpath)
work.mkdir(parents=True, exist_ok=True)
(work / "build_flavor.txt").write_text(FLAVOR, encoding="utf-8")
(work / "version_info.txt").write_text(version_file_text(VERSION), encoding="utf-8")

a = Analysis(
    [str(HERE / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(work / "build_flavor.txt"), "."), (str(HERE / "assets" / "zaza.ico"), ".")],  # icon of the ZaZa windows
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZaZaWorkAgent",
    debug=False,
    strip=False,
    upx=False,
    console=(FLAVOR == "development"),
    disable_windowed_traceback=False,
    icon=str(HERE / "assets" / "zaza.ico"),
    version=str(work / "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ZaZaWorkAgent")

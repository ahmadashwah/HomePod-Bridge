"""Rebuild dist/HomePodBridge.exe from homepod_bridge.py: python build_exe.py"""

import os
import tempfile

import PyInstaller.__main__

here = os.path.dirname(os.path.abspath(__file__))
work = os.path.join(tempfile.gettempdir(), "hb_build")
icon = os.path.join(here, "homepod.ico")

PyInstaller.__main__.run([
    "--noconfirm", "--onefile", "--windowed",
    "--name", "HomePodBridge",
    "--icon", icon,
    "--add-data", f"{icon};.",
    "--collect-submodules", "pyatv",
    "--collect-data", "pyatv",
    "--collect-submodules", "pycaw",
    "--hidden-import", "comtypes.stream",
    "--hidden-import", "pystray._win32",
    "--workpath", work,
    "--specpath", work,
    "--distpath", os.environ.get("HB_DIST", os.path.join(here, "dist")),
    os.path.join(here, "homepod_bridge.py"),
])


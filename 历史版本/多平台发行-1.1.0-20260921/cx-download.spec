# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格文件 —— 超星文件下载器（onefile / windowed）

用同目录的 build.ps1 调用（推荐），或手动：
    python -m PyInstaller --noconfirm --clean cx-download.spec

这里把「手工敲命令」固化成文件，避免参数散失。三个容易漏的点：
  1. customtkinter 的主题 JSON / 字体 / 图标是**数据文件**不是模块，必须 collect_all；
     漏了会白屏或全黑窗口，而且**不报错**，极难排查。
  2. 本项目是两文件结构（cx_download_gui.py 依赖 cx_download.py），
     pathex 必须含 SPECPATH，否则打包时可能找不到 core。
  3. selftest 是「函数内 import」，静态分析不一定收得到，用 hiddenimports 兜底。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")

# 图标按平台给：Windows 只认 .ico；Linux 的 ELF 没有图标位（忽略即可）；
# macOS 要 .icns，没有就留空。传错平台的文件 PyInstaller 会直接报错。
if sys.platform.startswith("win"):
    _icon = os.path.join(SPECPATH, "app.ico")
elif sys.platform == "darwin":
    _icns = os.path.join(SPECPATH, "app.icns")
    _icon = _icns if os.path.exists(_icns) else None
else:
    _icon = None

a = Analysis(
    ["cx_download_gui.py"],
    pathex=[SPECPATH],  # 同目录的 cx_download.py / selftest.py
    binaries=ctk_binaries,
    datas=ctk_datas,
    hiddenimports=ctk_hidden + ["selftest"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 本机 Python 里装了一堆跟本工具无关的包，PyInstaller 会顺着 hook 把它们
    # 一起收进来 —— numpy 一家就贡献约 17MB（含 OpenBLAS DLL），本地打出来
    # 32.8MB，而干净的 CI 环境只要 14.3MB 且功能完全正常。显式排除，保证两边一致。
    excludes=[
        "numpy", "PIL", "yaml", "charset_normalizer",
        "win32", "pywin32", "pywin32_system32", "pythoncom",
        "setuptools", "pkg_resources", "pip", "wheel",
        "requests", "urllib3", "certifi", "idna",
        "pytest", "_pytest", "unittest", "pydoc", "doctest",
        "lib2to3", "distutils",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="超星文件下载器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,  # 无黑框；要排查启动期报错时临时改 True
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 图标按平台给（见文件头）；相对路径会按 SPECPATH 解析，写错会报 Icon input file not found
    icon=_icon,
)

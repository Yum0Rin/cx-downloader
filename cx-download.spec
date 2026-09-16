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

from PyInstaller.utils.hooks import collect_all

ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")

a = Analysis(
    ["cx_download_gui.py"],
    pathex=[SPECPATH],  # 同目录的 cx_download.py / selftest.py
    binaries=ctk_binaries,
    datas=ctk_datas,
    hiddenimports=ctk_hidden + ["selftest"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    # 图标必须绝对路径：相对路径会按 SPECPATH 解析，写错会报 Icon input file not found
    icon=os.path.join(SPECPATH, "app.ico"),
)

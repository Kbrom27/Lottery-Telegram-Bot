# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    "aiosqlite",
    "sqlite3",
    "asyncpg",
    "anthropic",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "sqlalchemy.dialects.postgresql",
    "sqlalchemy.dialects.postgresql.asyncpg",
    "sqlalchemy.sql.default_comparator",
    "pystray",
    "pystray._win32",
    "zxingcpp",
    "openpyxl",
    "pypdf",
    "pytesseract",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageOps",
    "certifi",
    "draw",
    "app.bot",
    "app.config",
    "app.db",
    "app.extract",
    "app.main",
    "app.messages",
    "app.models",
    "app.reports",
    "app.service",
    "app.service_manager",
    "app.sms",
    "app.verify",
    "app.web_ui",
    "app.worker",
]
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("anthropic")
hiddenimports += collect_submodules("openpyxl")
hiddenimports += collect_submodules("pypdf")
hiddenimports += [m for m in collect_submodules("aiogram") if not m.startswith("aiogram.test")]

a = Analysis(
    ["desktop_launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("app/web_dashboard.html", "app"),
        ("app/lottery.ico", "app"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas", "tkinter", "pytest", "IPython", "matplotlib"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LotteryBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["app/lottery.ico"],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LotteryBot",
)

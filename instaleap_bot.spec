# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from pathlib import Path

# Localizar los datos de Playwright (drivers + metadata)
venv_site = Path(".venv/lib").glob("python*/site-packages")
pw_data = []
for sp in venv_site:
    pw_dir = sp / "playwright"
    if pw_dir.exists():
        # Incluir todo el paquete playwright (drivers, metadata, etc.)
        for item in pw_dir.rglob("*"):
            if item.is_file():
                rel = item.relative_to(sp)
                dest = str(rel.parent)
                pw_data.append((str(item), dest))
        break

a = Analysis(
    ["instaleap_bot.py"],
    pathex=["."],
    binaries=[],
    datas=pw_data,
    hiddenimports=[
        "playwright",
        "playwright.async_api",
        "playwright._impl._api_structures",
        "playwright._impl._browser",
        "playwright._impl._browser_context",
        "playwright._impl._page",
        "questionary",
        "questionary.prompts",
        "questionary.prompts.select",
        "questionary.prompts.confirm",
        "prompt_toolkit",
        "rich",
        "rich.console",
        "rich.table",
        "rich.panel",
    ],
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
    name="instaleap_bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

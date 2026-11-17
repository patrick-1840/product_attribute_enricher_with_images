# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller-specificatie voor de Product Image Classifier desktop-app.

Bouwen: dubbelklik de snelkoppeling "Compileer build" (zie build.ps1), of
handmatig: `pyinstaller build.spec --noconfirm`.

Resultaat: dist/ProductImageClassifier/ (een map, geen los .exe-bestand — dat is
bewuste keuze, zie CLAUDE.md "Distributie"). Deel die hele map (bv. als
.zip, gemaakt door build.ps1) met collega's. Zij vullen hun OpenAI API-key
in via Instellingen (bewaard in %APPDATA%/ProductImageClassifier/.env) of
leggen een .env met OPENAI_API_KEY naast ProductImageClassifier.exe.
"""
from PyInstaller.utils.hooks import collect_all

datas = [('favicon', 'favicon')]
binaries = []
hiddenimports = []
for pkg in ("openai",):
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hiddenimports
    except Exception:
        pass

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pandas trekt matplotlib (plotting) en numba (JIT) mee binnen als optionele
    # afhankelijkheid; geen van beide wordt hier gebruikt. Uitsluiten scheelt
    # tientallen MB en aanzienlijke bouwtijd.
    excludes=['matplotlib', 'numba', 'llvmlite', 'tkinter', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ProductImageClassifier',
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
    icon='favicon/favicon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ProductImageClassifier',
)

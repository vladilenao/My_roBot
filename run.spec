# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import t_tech
import os

datas = []
binaries = []
hiddenimports = []
# Имя сборки: robot-v<версия> при выпуске, иначе дефолт 'run'.
# Задаётся через переменную окружения, т.к. PyInstaller не разрешает
# --name вместе со spec-файлом.
NAME = os.environ.get('PYINSTALLER_NAME') or 'run'
# Вшитые дефолты конфигурации (распаковываются в sys._MEIPASS при старте)
datas += [(os.path.join(SPECPATH, 'default.toml'), '.')]
tmp_ret = collect_all('pandas_ta_classic')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('t_tech')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# t_tech — namespace-пакет без __file__; каталог берём из __path__
_ttech_certs = os.path.join(t_tech.__path__[0], 'invest', 'certs')
datas += [(_ttech_certs, 't_tech/invest/certs')]


a = Analysis(
    ['run.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt6', 'PySide6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

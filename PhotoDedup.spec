# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件 exe，无控制台窗口。

    .venv\\Scripts\\pyinstaller --noconfirm PhotoDedup.spec
"""

from pathlib import Path

ROOT = Path(SPECPATH)

EXCLUDES = [
    "tkinter", "unittest", "pydoc", "pdb", "doctest",
    "matplotlib", "pandas", "scipy", "IPython", "notebook", "jupyter",
    "pytest", "PIL.ImageTk", "PIL.ImageQt",
    # Qt 里用不到的重量级模块
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSerialPort",
    "PySide6.QtStateMachine", "PySide6.QtSvgWidgets", "PySide6.QtTextToSpeech",
    "PySide6.QtWebSockets", "PySide6.QtHttpServer",
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[(str(ROOT / "resources"), "resources")],
    hiddenimports=["pillow_heif", "send2trash", "cv2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PhotoDedup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "resources" / "icon.ico"),
)

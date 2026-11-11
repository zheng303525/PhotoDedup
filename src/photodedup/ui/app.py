"""应用入口：初始化 Qt、套主题、打开主窗口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .. import APP_ID, APP_NAME
from . import theme
from .main_window import MainWindow


def resource_path(name: str) -> Path:
    """开发时从项目 resources/ 取，打包后从 _MEIPASS 取。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "resources" / name
    return Path(__file__).resolve().parents[3] / "resources" / name


def main() -> int:
    if os.name == "nt":
        try:  # 让任务栏图标用我们自己的，而不是 python 的
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Victo")
    app.setStyle("Fusion")
    app.setStyleSheet(theme.QSS)

    icon = resource_path("icon.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    win = MainWindow()
    if icon.exists():
        win.setWindowIcon(QIcon(str(icon)))
    win.show()
    return app.exec()

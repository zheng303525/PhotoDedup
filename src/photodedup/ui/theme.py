"""界面样式：深色中性主题，让照片本身成为视觉重点。"""

from __future__ import annotations

BG = "#17181b"
PANEL = "#1f2126"
PANEL_2 = "#26282e"
BORDER = "#33363d"
TEXT = "#e8e9ec"
TEXT_DIM = "#9aa0aa"
ACCENT = "#4a8cf7"
KEEP = "#3fa66a"
DROP = "#d4574e"
WARN = "#d9a03c"

QSS = f"""
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
QLabel#H1 {{ font-size: 22px; font-weight: 500; }}
QLabel#H2 {{ font-size: 16px; font-weight: 500; }}
QLabel#Dim {{ color: {TEXT_DIM}; }}
QLabel#Mono {{ font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; color: {TEXT_DIM}; }}
QLabel#FieldName {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#FieldValue {{ font-size: 13px; }}
QPushButton {{
    background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 7px 16px;
}}
QPushButton:hover {{ background: #2f323a; border-color: #4a4e57; }}
QPushButton:pressed {{ background: #23252b; }}
QPushButton:disabled {{ color: #5b6068; background: #202227; }}
QPushButton#Primary {{
    background: {ACCENT}; border: none; color: #ffffff; font-weight: 500;
}}
QPushButton#Primary:hover {{ background: #5b98f8; }}
QPushButton#Primary:disabled {{ background: #2c3a52; color: #6d7c93; }}
QPushButton#Danger {{ background: {DROP}; border: none; color: #fff; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 10px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{
    background: {PANEL_2}; border: 1px solid {BORDER}; selection-background-color: #333944;
    outline: none;
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px; border: 1px solid {BORDER};
    border-radius: 4px; background: {PANEL_2};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QProgressBar {{
    background: {PANEL_2}; border: none; border-radius: 4px; height: 8px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 12px; margin: -5px 0; border-radius: 6px;
}}
QSplitter::handle {{ background: transparent; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #3a3d45; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{
    background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px;
}}
"""

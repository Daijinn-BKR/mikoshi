"""Aspecto visual. Sigue el modo claro u oscuro de Windows."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette

ICON_PATH = Path(__file__).parent / "assets" / "mikoshi.png"

DARK = {
    "bg": "#16171b", "panel": "#1e2026", "surface": "#2a2d35", "hover": "#343842", "text": "#ebe9e4",
    "muted": "#9c9ea8", "accent": "#e0a458", "on_accent": "#1c1408", "border": "#32353e",
}
LIGHT = {
    "bg": "#f5f4f0", "panel": "#ffffff", "surface": "#eceae4", "hover": "#e1dfd7", "text": "#22232a",
    "muted": "#666872", "accent": "#a8661f", "on_accent": "#ffffff", "border": "#dad8d0",
}

STYLE = """
QMainWindow, QDialog {{ background: {bg}; }}
QToolBar {{ background: {panel}; border: none; border-bottom: 1px solid {border}; padding: 4px 6px; spacing: 2px; }}
QToolButton {{ padding: 5px 9px; border-radius: 5px; }}
QToolButton:hover {{ background: {hover}; }}
QMenuBar {{ background: {panel}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {accent}; color: {on_accent}; }}
QMenu {{ background: {panel}; border: 1px solid {border}; }}
QStatusBar {{ background: {panel}; border-top: 1px solid {border}; color: {muted}; }}
QStatusBar QLabel {{ color: {muted}; }}
QTreeWidget, QListView, QTableView, QListWidget, QScrollArea {{ background: {panel}; border: none; }}
QTreeWidget::item {{ padding: 3px 2px; }}
QTreeWidget::item:selected, QListWidget::item:selected, QTableView::item:selected {{
    background: {accent}; color: {on_accent};
}}
QTreeWidget::item:hover:!selected, QListWidget::item:hover:!selected {{ background: {hover}; }}
QHeaderView::section {{
    background: {panel}; color: {muted}; border: none; border-bottom: 1px solid {border}; padding: 5px;
}}
QSplitter::handle {{ background: {border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QLineEdit, QPlainTextEdit, QComboBox {{
    background: {surface}; border: 1px solid {border}; border-radius: 5px; padding: 4px 6px;
    selection-background-color: {accent}; selection-color: {on_accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {accent}; }}
QComboBox QAbstractItemView {{ background: {panel}; selection-background-color: {accent}; selection-color: {on_accent}; }}
QPushButton {{ background: {surface}; border: 1px solid {border}; border-radius: 5px; padding: 6px 10px; }}
QPushButton:hover {{ background: {hover}; }}
QPushButton:disabled {{ color: {muted}; }}
QPushButton[primary="true"] {{ background: {accent}; color: {on_accent}; border-color: {accent}; font-weight: 600; }}
QPushButton[primary="true"]:disabled {{ background: {surface}; color: {muted}; border-color: {border}; font-weight: 400; }}
QSlider::groove:horizontal {{ background: {surface}; height: 4px; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {accent}; width: 12px; margin: -5px 0; border-radius: 6px; }}
QLabel[muted="true"] {{ color: {muted}; }}
QLabel[title="true"] {{ font-size: 15pt; font-weight: 600; }}
"""


def tokens(app) -> dict:
    return DARK if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else LIGHT


def apply_theme(app) -> None:
    app.setStyle("Fusion")
    colors = tokens(app)
    palette = QPalette()
    for role, name in (
        (QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.WindowText, "text"),
        (QPalette.ColorRole.Base, "panel"), (QPalette.ColorRole.AlternateBase, "surface"),
        (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.Button, "surface"),
        (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent"),
        (QPalette.ColorRole.HighlightedText, "on_accent"), (QPalette.ColorRole.ToolTipBase, "panel"),
        (QPalette.ColorRole.ToolTipText, "text"), (QPalette.ColorRole.PlaceholderText, "muted"),
    ):
        palette.setColor(role, QColor(colors[name]))
    app.setPalette(palette)
    app.setStyleSheet(STYLE.format(**colors))
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

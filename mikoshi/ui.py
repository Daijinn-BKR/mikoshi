"""Interfaz de escritorio."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject, QRect, QRunnable, QSize, Qt, QThread, QThreadPool, Signal,
)
from PySide6.QtGui import (
    QAction, QBrush, QColor, QFont, QIcon, QImage, QIntValidator, QKeySequence, QLinearGradient, QPainter, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSplitter, QStackedWidget, QStyle,
    QStyledItemDelegate, QTableView, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import dialogs, maintenance, metadata, online, scanner, thumbnails
from .db import Catalog, covers_dir, display_title, effective, full_path
from .player import PLAYABLE, PlayerDialog
from .theme import apply_theme

KIND_STYLE = {
    "video": ("#3f6fb5", "VÍDEO"),
    "audio": ("#8a4fb0", "AUDIO"),
    "image": ("#3a9276", "IMAGEN"),
    "document": ("#b5793f", "LIBRO"),
    "game": ("#b54f5c", "JUEGO"),
}
BACKUP_INTERVAL = 24 * 3600
SORT_OPTIONS = [
    ("Título", 0, Qt.SortOrder.AscendingOrder), ("Creador", 2, Qt.SortOrder.AscendingOrder),
    ("Año (recientes)", 3, Qt.SortOrder.DescendingOrder), ("Valoración", 4, Qt.SortOrder.DescendingOrder),
    ("Tamaño", 6, Qt.SortOrder.DescendingOrder), ("Tipo", 1, Qt.SortOrder.AscendingOrder),
]
COLUMNS = ["Título", "Tipo", "Creador", "Año", "Valoración", "Etiquetas", "Tamaño", "Ruta"]


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def creator(item: dict) -> str:
    meta = effective(item)
    return next((meta[k] for k in metadata.CREATOR_KEYS if meta.get(k)), "")


def year(item: dict) -> str:
    meta = effective(item)
    return meta.get("year") or meta.get("date", "")[:4]


def stars(rating: int) -> str:
    return "★" * rating + "☆" * (5 - rating)


def placeholder(kind: str) -> QPixmap:
    color, label = KIND_STYLE[kind]
    pixmap = QPixmap(240, 240)
    gradient = QLinearGradient(0, 0, 240, 240)
    gradient.setColorAt(0, QColor(color))
    gradient.setColorAt(1, QColor(color).darker(170))
    painter = QPainter(pixmap)
    painter.fillRect(pixmap.rect(), QBrush(gradient))
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Segoe UI", 20, QFont.Weight.DemiBold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, label)
    painter.end()
    return pixmap


def open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def reveal_path(path: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        open_path(os.path.dirname(path))


# Trabajos en segundo plano. Cada hilo abre su propia conexión a SQLite.

class ScanWorker(QThread):
    progress = Signal(str)
    done = Signal(list, list)

    def __init__(self, db_path: str, root_ids: list[int], parent=None):
        super().__init__(parent)
        self.db_path, self.root_ids = db_path, root_ids

    def run(self) -> None:
        catalog = Catalog(self.db_path)
        results, offline = [], []
        for root_id in self.root_ids:
            if self.isInterruptionRequested():
                break
            try:
                results.append(scanner.scan_root(
                    catalog, root_id, self.progress.emit, self.isInterruptionRequested))
            except scanner.RootOffline as exc:
                offline.append(str(exc))
            except OSError as exc:
                offline.append(f"{catalog.root(root_id)['path']}: {exc}")
        catalog.close()
        self.done.emit(results, offline)


class WriteWorker(QThread):
    progress = Signal(str)
    done = Signal(int, list)

    def __init__(self, db_path: str, item_ids: list[int], parent=None):
        super().__init__(parent)
        self.db_path, self.item_ids = db_path, item_ids

    def run(self) -> None:
        catalog = Catalog(self.db_path)
        written, errors = 0, []
        for index, item_id in enumerate(self.item_ids, 1):
            if self.isInterruptionRequested():
                break
            item = catalog.get_item(item_id)
            name = Path(item["rel_path"]).name
            self.progress.emit(f"Escribiendo {index}/{len(self.item_ids)} · {name}")
            try:
                rejected = scanner.apply_edits_to_file(catalog, item)
                written += 1
                if rejected:
                    labels = dict(metadata.FIELDS[item["kind"]])
                    errors.append(f"{name}: el formato no conservó {', '.join(labels[k] for k in rejected)}. "
                                  "Revisa el valor (temporada y episodio deben ser números).")
            except (metadata.MetadataError, OSError) as exc:
                errors.append(f"{name}: {exc}")
        catalog.close()
        self.done.emit(written, errors)


class MaintenanceWorker(QThread):
    progress = Signal(str)
    done = Signal(object)

    def __init__(self, db_path: str, function, parent=None):
        super().__init__(parent)
        self.db_path, self.function = db_path, function

    def run(self) -> None:
        catalog = Catalog(self.db_path)
        result = self.function(catalog, progress=self.progress.emit, cancelled=self.isInterruptionRequested)
        catalog.close()
        self.done.emit(result)


class ThumbSignals(QObject):
    ready = Signal(int, str, QImage)


class ThumbJob(QRunnable):
    def __init__(self, item: dict, key: str, signals: ThumbSignals):
        super().__init__()
        self.item, self.key, self.signals = item, key, signals

    def run(self) -> None:
        path = thumbnails.thumbnail_for(self.item)
        self.signals.ready.emit(self.item["id"], self.key, QImage(path) if path else QImage())


# Modelo de datos, compartido por la cuadrícula y la lista

class ItemsModel(QAbstractTableModel):
    MAX_CACHED = 3000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []
        self.sort_column, self.sort_order = 0, Qt.SortOrder.AscendingOrder
        self.natural: str | None = None
        self.placeholders = {kind: placeholder(kind) for kind in KIND_STYLE}
        self.pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self.requested: set[str] = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.signals = ThumbSignals()
        self.signals.ready.connect(self._thumb_ready)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def _value(self, item: dict, column: int) -> str:
        return [
            lambda: display_title(item),
            lambda: metadata.KIND_LABELS[item["kind"]],
            lambda: creator(item),
            lambda: year(item),
            lambda: stars(item["rating"]) if item["rating"] else "",
            lambda: item["tags"] or "",
            lambda: human_size(item["size"]),
            lambda: full_path(item),
        ][column]()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._value(item, index.column())
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            # QIcon se adapta al tamaño de cada vista; un QPixmap se dibujaría entero.
            return QIcon(self._thumbnail(item))
        if role == Qt.ItemDataRole.ForegroundRole and item["status"] == "missing":
            return QColor("#999999")
        if role == Qt.ItemDataRole.ToolTipRole:
            note = "\nNo se encontró en el último escaneo" if item["status"] == "missing" else ""
            return full_path(item) + note
        return None

    @staticmethod
    def _key(item: dict) -> str:
        return f"{item['id']}|{item['mtime']}|{item['cover']}"

    def _thumbnail(self, item: dict) -> QPixmap:
        key = self._key(item)
        cached = self.pixmaps.get(key)
        if cached is not None:
            self.pixmaps.move_to_end(key)
            return cached
        if key not in self.requested and item["status"] == "ok":
            self.requested.add(key)
            self.pool.start(ThumbJob(item, key, self.signals))
        return self.placeholders[item["kind"]]

    def _thumb_ready(self, item_id: int, key: str, image: QImage) -> None:
        if image.isNull():
            return
        self.pixmaps[key] = QPixmap.fromImage(image)
        while len(self.pixmaps) > self.MAX_CACHED:
            self.pixmaps.popitem(last=False)
        for row, item in enumerate(self.items):
            if item["id"] == item_id:
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])
                break

    @staticmethod
    def _number(value) -> int:
        match = re.match(r"\d+", str(value or ""))
        return int(match.group(0)) if match else 10 ** 6

    def _sorted(self, items: list[dict]) -> list[dict]:
        if self.natural:
            # Dentro de una serie o un álbum manda el orden de episodios o pistas.
            def order(item):
                meta = effective(item)
                numbers = ("season", "episode") if self.natural == "series" else ("track",)
                return [self._number(meta.get(n)) for n in numbers] + [display_title(item).casefold()]
            return sorted(items, key=order)
        if self.sort_column == 3:
            def by_year(item):
                value = self._number(year(item))
                return value if value < 10 ** 6 else 0
            return sorted(items, key=by_year, reverse=self.sort_order == Qt.SortOrder.DescendingOrder)
        if self.sort_column == 4:
            key = lambda i: i["rating"]
        elif self.sort_column == 6:
            key = lambda i: i["size"]
        else:
            key = lambda i: self._value(i, self.sort_column).casefold()
        return sorted(items, key=key, reverse=self.sort_order == Qt.SortOrder.DescendingOrder)

    def set_items(self, items: list[dict]) -> None:
        self.pool.clear()
        self.requested -= {k for k in self.requested if k not in self.pixmaps}
        self.beginResetModel()
        self.items = self._sorted(items)
        self.endResetModel()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_column, self.sort_order = column, order
        self.natural = None
        self.set_items(self.items)

    def update_item(self, item: dict) -> None:
        for row, current in enumerate(self.items):
            if current["id"] == item["id"]:
                self.items[row] = item
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
                break


class GridDelegate(QStyledItemDelegate):
    """Celda según el zoom: miniatura centrada, valoración y título en dos líneas como máximo."""

    def sizeHint(self, option, index):
        return self.parent().gridSize() - QSize(4, 4)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = option.state & QStyle.StateFlag.State_Selected
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(option.palette.highlight())
            painter.drawRoundedRect(rect, 6, 6)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        icon_rect = QRect(rect.x() + 4, rect.y() + 4, rect.width() - 8, self.parent().iconSize().height())
        icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)
        rating = self.parent().model().items[index.row()]["rating"]
        if rating:
            badge = QRect(icon_rect.right() - 38, icon_rect.top() + 4, 34, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#f2c14e"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, f"★{rating}")
        text_rect = QRect(rect.x() + 4, icon_rect.bottom() + 3, rect.width() - 8, rect.bottom() - icon_rect.bottom() - 3)
        color = index.data(Qt.ItemDataRole.ForegroundRole)
        if selected:
            painter.setPen(option.palette.highlightedText().color())
        else:
            painter.setPen(color or option.palette.text().color())
        metrics = option.fontMetrics
        text = metrics.elidedText(index.data(), Qt.TextElideMode.ElideRight, int(text_rect.width() * 1.8))
        painter.setFont(option.font)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                         | Qt.TextFlag.TextWordWrap, text)
        painter.restore()


# Panel de detalle

class DetailPanel(QScrollArea):
    saved = Signal(int)
    write_requested = Signal()
    tags_changed = Signal()
    play_requested = Signal()
    bulk_requested = Signal()

    def __init__(self, catalog: Catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.item: dict | None = None
        self.inputs: dict[str, QWidget] = {}
        self.dirty = False
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(340)

        body = QWidget()
        layout = QVBoxLayout(body)
        self.cover = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.cover.setMinimumHeight(200)
        self.heading = QLabel(wordWrap=True)
        self.heading.setStyleSheet("font-weight: 600;")
        self.heading.setMinimumWidth(1)
        self.warning = QLabel(wordWrap=True)
        self.warning.setStyleSheet("color: #c0392b;")
        self.warning.hide()
        layout.addWidget(self.cover)
        layout.addWidget(self.heading)
        layout.addWidget(self.warning)

        self.bulk_button = QPushButton("Editar varios…")
        self.bulk_button.setProperty("primary", True)
        self.bulk_button.clicked.connect(self.bulk_requested)
        self.bulk_button.hide()
        layout.addWidget(self.bulk_button)

        actions = QHBoxLayout()
        self.play_button = QPushButton("Reproducir")
        self.play_button.setProperty("primary", True)
        self.open_button = QPushButton("Abrir fuera")
        self.reveal_button = QPushButton("Ver en carpeta")
        for button in (self.play_button, self.open_button, self.reveal_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        enrich = QHBoxLayout()
        self.online_button = QPushButton("Buscar en internet…")
        self.cover_button = QPushButton("Portada…")
        enrich.addWidget(self.online_button, 2)
        enrich.addWidget(self.cover_button, 1)
        layout.addLayout(enrich)
        self.play_button.clicked.connect(self.play_requested)
        self.open_button.clicked.connect(lambda: self._launch(open_path))
        self.reveal_button.clicked.connect(lambda: self._launch(reveal_path))
        self.cover_button.clicked.connect(self._choose_cover)
        self.online_button.clicked.connect(self._search_online)

        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.form_host)

        common = QFormLayout()
        self.rating = QComboBox()
        self.rating.addItems(["Sin valorar"] + [stars(n) for n in range(1, 6)])
        self.tags = QLineEdit(placeholderText="Separadas por comas")
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(70)
        common.addRow("Valoración", self.rating)
        common.addRow("Etiquetas", self.tags)
        common.addRow("Notas", self.notes)
        layout.addLayout(common)
        self.rating.currentIndexChanged.connect(self._mark_dirty)
        self.tags.textEdited.connect(self._mark_dirty)
        self.notes.textChanged.connect(self._mark_dirty)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("Guardar en catálogo")
        self.write_button = QPushButton("Escribir en archivo…")
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.write_button)
        layout.addLayout(buttons)
        self.save_button.clicked.connect(self.commit)
        self.write_button.clicked.connect(self.write_requested)

        self.write_hint = QLabel(wordWrap=True)
        self.write_hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self.write_hint)
        self.technical = QLabel(wordWrap=True, textInteractionFlags=Qt.TextInteractionFlag.TextSelectableByMouse)
        self.technical.setStyleSheet("color: gray;")
        layout.addWidget(self.technical)
        layout.addStretch()
        self.setWidget(body)
        self.show_item(None)

    def _mark_dirty(self, *_args) -> None:
        if not self._loading and self.item:
            self.dirty = True
            self._refresh_write_state()

    _loading = False

    def _form_edits(self) -> dict:
        edits = {k: v for k, v in self.item["edits"].items() if k not in self.inputs}
        for key, widget in self.inputs.items():
            value = widget.toPlainText() if isinstance(widget, QPlainTextEdit) else widget.text()
            value = value.strip()
            if value != self.item["file_meta"].get(key, ""):
                edits[key] = value
        return edits

    def _launch(self, action) -> None:
        if not self.item:
            return
        path = full_path(self.item)
        if not os.path.exists(path):
            QMessageBox.warning(self, "No disponible",
                                f"No se encuentra:\n{path}\n\n¿Está encendido el servidor?")
            return
        try:
            action(path)
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo abrir", str(exc))

    def _choose_cover(self) -> None:
        if not self.item:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Elegir portada", "", "Imágenes (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not path:
            return
        try:
            self._store_cover(Path(path).read_bytes(), Path(path).suffix.lower())
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo copiar", str(exc))

    def _store_cover(self, data: bytes, suffix: str) -> None:
        # Un nombre nuevo cada vez hace que la miniatura se regenere sola.
        dest = covers_dir() / f"{self.item['id']}-{time.time_ns()}{suffix}"
        dest.write_bytes(data)
        if self.item["cover"]:
            (covers_dir() / Path(self.item["cover"]).name).unlink(missing_ok=True)
        self.commit()
        self.catalog.save_user_data(self.item["id"], cover=str(dest))
        self.saved.emit(self.item["id"])
        self.show_item(self.catalog.get_item(self.item["id"]))

    def _search_query(self) -> str:
        field = lambda key: self.inputs[key].text() if key in self.inputs else ""
        title = field("title") or field("series")
        return f"{title} {field('artist') or field('author')}".strip()

    def _search_online(self) -> None:
        if not self.item:
            return
        dialog = dialogs.OnlineDialog(self.item["kind"], self._search_query(), dialogs.service_keys(self.catalog), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.apply_online(dialog.fields, dialog.cover)

    def apply_online(self, fields: dict, cover: bytes | None) -> None:
        """Rellena el formulario para revisarlo; se guarda como cualquier otra edición."""
        if cover:
            try:
                self._store_cover(cover, ".jpg")
            except OSError as exc:
                QMessageBox.warning(self, "No se pudo guardar la portada", str(exc))
        for key, value in fields.items():
            widget = self.inputs.get(key)
            if isinstance(widget, QPlainTextEdit):
                widget.setPlainText(value)
            elif widget is not None:
                widget.setText(value)
        self.dirty = True
        self._refresh_write_state()
        self.write_hint.setText("Datos de internet sin guardar: revísalos y pulsa «Guardar en catálogo».")

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        self.cover.setPixmap(pixmap.scaled(QSize(240, 240), Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))

    def show_multiple(self, count: int) -> None:
        self.show_item(None)
        self.bulk_button.show()
        self.heading.setText(f"{count} elementos seleccionados.\n"
                             "Usa el clic derecho para etiquetar, valorar o añadir a una colección.")

    def show_item(self, item: dict | None) -> None:
        self._loading = True
        self.item = item
        self.dirty = False
        while self.form.rowCount():
            self.form.removeRow(0)
        self.inputs = {}
        enabled = item is not None
        for widget in (self.open_button, self.reveal_button, self.cover_button, self.rating,
                       self.tags, self.notes, self.save_button):
            widget.setEnabled(enabled)
        self.play_button.setEnabled(bool(item) and item["kind"] in PLAYABLE)
        self.online_button.setEnabled(bool(item) and item["kind"] in online.SOURCES)
        self.bulk_button.hide()
        self.warning.hide()
        if not item:
            self.cover.clear()
            self.heading.setText("Selecciona un elemento")
            self.rating.setCurrentIndex(0)
            self.tags.clear()
            self.notes.clear()
            self.technical.clear()
            self.write_hint.clear()
            self.write_button.setEnabled(False)
            self._loading = False
            return

        path = full_path(item)
        # Espacios de ancho cero: permiten partir en líneas una ruta larga sin espacios.
        self.heading.setText(path.replace("\\", "\\​").replace("/", "/​").replace(".", ".​"))
        self.heading.setToolTip(path)
        if item["status"] == "missing":
            self.warning.setText("No se encontró en el último escaneo. Sus datos siguen guardados.")
            self.warning.show()
        meta = effective(item)
        can_write = metadata.writable_fields(path, item["kind"])
        for key, label in metadata.FIELDS[item["kind"]]:
            if key == "description":
                widget = QPlainTextEdit(meta.get(key, ""))
                widget.setFixedHeight(80)
                widget.textChanged.connect(self._mark_dirty)
            else:
                widget = QLineEdit(meta.get(key, ""))
                widget.textEdited.connect(self._mark_dirty)
            self.inputs[key] = widget
            self.form.addRow(label + (" *" if key in can_write else ""), widget)
        self.rating.setCurrentIndex(item["rating"])
        self.tags.setText(item["tags"] or "")
        self.notes.setPlainText(item["notes"])
        self.technical.setText("\n".join(
            [f"Tamaño: {human_size(item['size'])}"] + [f"{k}: {v}" for k, v in item["technical"].items()]))
        self._refresh_write_state()
        self._loading = False

    def _refresh_write_state(self) -> None:
        path = full_path(self.item)
        can_write = metadata.writable_fields(path, self.item["kind"])
        pending = metadata.pending_edits({**self.item, "edits": self._form_edits()}, path)
        self.write_button.setEnabled(bool(pending))
        if not can_write:
            self.write_hint.setText("Este formato solo guarda los datos en el catálogo.")
        elif pending:
            self.write_hint.setText(f"Sin escribir en el archivo: {len(pending)} campo(s). * = se puede escribir.")
        else:
            self.write_hint.setText("* = se puede escribir en el archivo.")

    def commit(self) -> None:
        """Guarda el formulario en el catálogo. Nunca modifica el archivo."""
        if not self.item or not self.dirty:
            return
        edits = self._form_edits()
        item_id = self.item["id"]
        self.catalog.save_user_data(item_id, edits=edits, rating=self.rating.currentIndex(),
                                    notes=self.notes.toPlainText())
        self.catalog.set_tags(item_id, self.tags.text().split(","))
        self.item = self.catalog.get_item(item_id)
        self.dirty = False
        self._refresh_write_state()
        self.saved.emit(item_id)
        self.tags_changed.emit()


# Gestión de carpetas

class RootsDialog(QDialog):
    def __init__(self, catalog: Catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle("Carpetas de la colección")
        self.resize(640, 320)
        layout = QVBoxLayout(self)
        self.listing = QListWidget()
        layout.addWidget(self.listing)
        row = QHBoxLayout()
        relocate = QPushButton("Cambiar ruta…")
        remove = QPushButton("Quitar del catálogo")
        row.addWidget(relocate)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(QLabel("Quitar una carpeta borra sus datos del catálogo, pero nunca toca tus archivos."))
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.accept)
        layout.addWidget(box)
        relocate.clicked.connect(self._relocate)
        remove.clicked.connect(self._remove)
        self._load()

    def _load(self) -> None:
        self.listing.clear()
        for root in self.catalog.roots():
            online = "conectada" if os.path.isdir(root["path"]) else "NO DISPONIBLE"
            mode = "juegos" if root["mode"] == "games" else "automático"
            entry = QListWidgetItem(f"{root['path']}   ({mode}, {online})")
            entry.setData(Qt.ItemDataRole.UserRole, root["id"])
            self.listing.addItem(entry)

    def _selected(self) -> int | None:
        entry = self.listing.currentItem()
        return entry.data(Qt.ItemDataRole.UserRole) if entry else None

    def _relocate(self) -> None:
        root_id = self._selected()
        if root_id is None:
            return
        path = QFileDialog.getExistingDirectory(self, "Nueva ubicación de esta carpeta", self.catalog.root(root_id)["path"])
        if path:
            self.catalog.relocate_root(root_id, path)
            self._load()

    def _remove(self) -> None:
        root_id = self._selected()
        if root_id is None:
            return
        path = self.catalog.root(root_id)["path"]
        answer = QMessageBox.question(
            self, "Quitar carpeta",
            f"¿Quitar {path} del catálogo?\n\nSe perderán sus etiquetas, valoraciones y ediciones "
            "no escritas. Los archivos no se tocan.")
        if answer == QMessageBox.StandardButton.Yes:
            self.catalog.remove_root(root_id)
            self._load()


# Ventana principal

class MainWindow(QMainWindow):
    GRID, LIST, WELCOME = 0, 1, 2

    def __init__(self, db_path=None):
        super().__init__()
        self.catalog = Catalog(db_path)
        self.worker: QThread | None = None
        self.setWindowTitle("Mikoshi")
        self.resize(1320, 820)

        self.model = ItemsModel(self)
        self.model.signals.ready.connect(self._thumb_arrived)

        self.sidebar = QTreeWidget(headerHidden=True)
        self.sidebar.setMinimumWidth(190)
        self.sidebar.itemSelectionChanged.connect(self.reload)

        self.grid = QListView()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setItemDelegate(GridDelegate(self.grid))
        self.grid.setSpacing(4)
        self.grid.setModel(self.model)
        self._set_zoom(int(self.catalog.setting("zoom", "150")))

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setIconSize(QSize(28, 28))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 300)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.table.setSelectionModel(self.grid.selectionModel())
        for view in (self.grid, self.table):
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(self._context_menu)
            view.doubleClicked.connect(lambda _index: self._activate())
        self.grid.selectionModel().selectionChanged.connect(self._selection_changed)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self._build_welcome())
        self.view_mode = self.GRID

        self.detail = DetailPanel(self.catalog)
        self.detail.saved.connect(self._item_saved)
        self.detail.tags_changed.connect(self._refresh_sidebar)
        self.detail.write_requested.connect(lambda: self._write_files(self._selected_ids()))
        self.detail.play_requested.connect(self._play_current)
        self.detail.bulk_requested.connect(lambda: self._bulk_edit(self._selected_ids()))

        self.sidebar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sidebar.customContextMenuRequested.connect(self._sidebar_menu)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        center_layout.addWidget(self._build_filter_bar())
        center_layout.addWidget(self.stack, 1)

        splitter = QSplitter()
        splitter.addWidget(self.sidebar)
        splitter.addWidget(center)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([210, 750, 360])
        self.setCentralWidget(splitter)

        self._build_actions()
        self._refresh_sidebar()
        self.reload()
        self._report_backup_error()

    # Menús y barra

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()
        title = QLabel("Tu colección empieza aquí", alignment=Qt.AlignmentFlag.AlignCenter)
        title.setProperty("title", True)
        hint = QLabel("Añade una carpeta local o de red. Mikoshi solo la lee: tus archivos no cambian.",
                      alignment=Qt.AlignmentFlag.AlignCenter)
        hint.setProperty("muted", True)
        button = QPushButton("Añadir carpeta…")
        button.setProperty("primary", True)
        button.clicked.connect(lambda: self._add_root("auto"))
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addSpacing(10)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return page

    def _build_filter_bar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        self.sort_box = QComboBox()
        for label, _column, _order in SORT_OPTIONS:
            self.sort_box.addItem(label)
        self.sort_box.currentIndexChanged.connect(self._apply_sort)
        self.rating_filter = QComboBox()
        self.rating_filter.addItems(["Cualquier valoración"] + [f"{'★' * n} o más" for n in range(1, 6)])
        self.genre_filter = QComboBox()
        self.genre_filter.addItem("Cualquier género")
        self.genre_filter.setMinimumWidth(140)
        self.year_from = QLineEdit(placeholderText="Año desde")
        self.year_to = QLineEdit(placeholderText="hasta")
        for edit in (self.year_from, self.year_to):
            edit.setFixedWidth(84)
            edit.setValidator(QIntValidator(0, 9999, edit))
            edit.textChanged.connect(lambda _text: self.reload())
        self.rating_filter.currentIndexChanged.connect(lambda _index: self.reload())
        self.genre_filter.currentIndexChanged.connect(lambda _index: self.reload())
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(90, 260)
        self.zoom.setFixedWidth(110)
        self.zoom.setValue(self.grid.iconSize().width())
        self.zoom.setToolTip("Tamaño de las miniaturas")
        self.zoom.valueChanged.connect(self._set_zoom)
        row.addWidget(QLabel("Ordenar"))
        row.addWidget(self.sort_box)
        row.addSpacing(10)
        row.addWidget(self.rating_filter)
        row.addWidget(self.genre_filter)
        row.addWidget(self.year_from)
        row.addWidget(self.year_to)
        row.addStretch()
        row.addWidget(QLabel("Tamaño"))
        row.addWidget(self.zoom)
        return bar

    def _set_zoom(self, size: int) -> None:
        self.grid.setIconSize(QSize(size, size))
        self.grid.setGridSize(QSize(size + 26, size + 56))

    def _apply_sort(self, index: int) -> None:
        _label, column, order = SORT_OPTIONS[index]
        self.detail.commit()
        self.table.sortByColumn(column, order)

    def _set_view(self, mode: int) -> None:
        self.view_mode = mode
        self._show_view()

    def _show_view(self) -> None:
        empty = not self.catalog.roots()
        self.stack.setCurrentIndex(self.WELCOME if empty else self.view_mode)

    def _build_actions(self) -> None:
        def action(text, slot, shortcut=None):
            act = QAction(text, self)
            act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            return act

        self.add_action = action("Añadir carpeta…", lambda: self._add_root("auto"))
        self.add_games_action = action("Añadir carpeta de juegos…", lambda: self._add_root("games"))
        self.scan_action = action("Escanear", self.scan_all, "F5")
        manage = action("Gestionar carpetas…", self._manage_roots)
        backup = action("Copia del catálogo…", self._backup)
        export = action("Exportar a CSV…", self._export)
        settings = action("Ajustes…", self._open_settings, "Ctrl+,")
        quit_action = action("Salir", self.close, "Ctrl+Q")
        grid_action = action("Cuadrícula", lambda: self._set_view(self.GRID), "Ctrl+1")
        list_action = action("Lista", lambda: self._set_view(self.LIST), "Ctrl+2")
        verify = action("Comprobar integridad…", self._verify)
        duplicates = action("Buscar duplicados…", self._find_duplicates)

        file_menu = self.menuBar().addMenu("&Archivo")
        file_menu.addActions([self.add_action, self.add_games_action, manage])
        file_menu.addSeparator()
        file_menu.addActions([backup, export])
        file_menu.addSeparator()
        file_menu.addAction(settings)
        file_menu.addAction(quit_action)
        view_menu = self.menuBar().addMenu("&Ver")
        view_menu.addActions([grid_action, list_action])
        tools_menu = self.menuBar().addMenu("&Mantenimiento")
        tools_menu.addActions([verify, duplicates])

        bar = QToolBar(movable=False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(bar)
        bar.addActions([self.add_action, self.add_games_action, self.scan_action])
        bar.addSeparator()
        bar.addActions([grid_action, list_action])
        bar.addSeparator()
        self.search = QLineEdit(placeholderText="Buscar título, artista, etiqueta, ruta…", clearButtonEnabled=True)
        self.search.setMaximumWidth(420)
        self.search.textChanged.connect(lambda _text: self.reload())
        bar.addWidget(self.search)
        self.addAction(action("Buscar", self.search.setFocus, "Ctrl+F"))

        self.status_label = QLabel()
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(lambda: self.worker and self.worker.requestInterruption())
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.cancel_button)

    def _refresh_sidebar(self) -> None:
        current = self._current_filter()
        self.sidebar.blockSignals(True)
        self.sidebar.clear()

        def section(title, expanded=True):
            node = QTreeWidgetItem(self.sidebar, [title])
            node.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = node.font(0)
            font.setBold(True)
            node.setFont(0, font)
            node.setExpanded(expanded)
            return node

        def entry(parent, text, data):
            node = QTreeWidgetItem(parent, [text])
            node.setData(0, Qt.ItemDataRole.UserRole, data)
            if data == current:
                node.setSelected(True)
                parent.setExpanded(True)
            return node

        library = section("Biblioteca")
        entry(library, "Todo", ("all", None))
        for kind, label in metadata.KIND_LABELS.items():
            entry(library, label, ("kind", kind))
        state = section("Estado")
        entry(state, "Sin escribir en archivo", ("pending", None))
        entry(state, "Faltantes", ("missing", None))
        collections = self.catalog.collections()
        if collections:
            collection_node = section("Colecciones")
            for collection in collections:
                entry(collection_node, collection["name"], ("collection", collection["id"]))
        for title, field in (("Series", "series"), ("Álbumes", "album")):
            names = self.catalog.group_names(field)
            if names:
                group_node = section(title, expanded=False)
                for name in names:
                    entry(group_node, name, (field, name))
        tags = self.catalog.all_tags()
        if tags:
            tag_node = section("Etiquetas")
            for tag in tags:
                entry(tag_node, tag, ("tag", tag))
        roots = self.catalog.roots()
        if roots:
            root_node = section("Carpetas")
            for root in roots:
                node = entry(root_node, Path(root["path"]).name or root["path"], ("root", root["id"]))
                node.setToolTip(0, root["path"])
        if not self.sidebar.selectedItems():
            self.sidebar.topLevelItem(0).child(0).setSelected(True)
        self.sidebar.blockSignals(False)

    def _current_filter(self) -> tuple:
        selected = self.sidebar.selectedItems()
        return selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else ("all", None)

    # Lista y selección

    def reload(self) -> None:
        self.detail.commit()
        kind, value = self._current_filter()
        text = self.search.text() if hasattr(self, "search") else ""
        args = {"text": text}
        self.model.natural = None
        if kind == "collection":
            args["collection"] = value
        elif kind in ("series", "album"):
            args["group"] = (kind, value)
            self.model.natural = kind
        if kind == "kind":
            args["kind"] = value
        elif kind == "tag":
            args["tag"] = value
        elif kind == "root":
            args["root_id"] = value
        elif kind == "missing":
            args["status"] = "missing"
        items = self.catalog.query_items(**args)
        if kind == "pending":
            items = [i for i in items if metadata.pending_edits(i, full_path(i))]
        self.model.set_items(self._apply_filters(items))
        self.detail.show_item(None)
        self._show_view()
        self._show_count()

    @staticmethod
    def _genres(item: dict) -> list[str]:
        return [g.strip() for g in effective(item).get("genre", "").split(",") if g.strip()]

    def _apply_filters(self, items: list[dict]) -> list[dict]:
        if not hasattr(self, "rating_filter"):
            return items
        # Los géneros se ofrecen según lo que hay antes de filtrar por género.
        selected = self.genre_filter.currentText() if self.genre_filter.currentIndex() > 0 else ""
        genres = sorted({g for item in items for g in self._genres(item)} | ({selected} if selected else set()),
                        key=str.casefold)
        self.genre_filter.blockSignals(True)
        self.genre_filter.clear()
        self.genre_filter.addItem("Cualquier género")
        self.genre_filter.addItems(genres)
        self.genre_filter.setCurrentText(selected or "Cualquier género")
        self.genre_filter.blockSignals(False)

        minimum = self.rating_filter.currentIndex()
        low = int(self.year_from.text()) if self.year_from.text() else None
        high = int(self.year_to.text()) if self.year_to.text() else None

        def keep(item: dict) -> bool:
            if item["rating"] < minimum:
                return False
            if selected and selected not in self._genres(item):
                return False
            if low is None and high is None:
                return True
            match = re.match(r"\d{4}", year(item))
            if not match:
                return False
            value = int(match.group(0))
            return (low is None or value >= low) and (high is None or value <= high)

        return [item for item in items if keep(item)]

    def _activate(self) -> None:
        item = self.detail.item
        if item and item["kind"] in PLAYABLE:
            self._play_current()
        else:
            self.detail._launch(open_path)

    def _play_current(self) -> None:
        item = self.detail.item
        if not item:
            return
        self.detail.commit()
        # La lista de reproducción es lo que se está viendo, del mismo tipo.
        queue = [i for i in self.model.items if i["kind"] == item["kind"]]
        index = next((n for n, i in enumerate(queue) if i["id"] == item["id"]), None)
        if index is None:
            queue, index = [item], 0
        PlayerDialog(queue, index, open_path, self).exec()

    def _show_count(self) -> None:
        if not (self.worker and self.worker.isRunning()):
            total = sum(i["size"] for i in self.model.items)
            self.status_label.setText(f"{len(self.model.items)} elementos · {human_size(total)}")

    def _selected_ids(self) -> list[int]:
        rows = {index.row() for index in self.grid.selectionModel().selectedIndexes()}
        return [self.model.items[row]["id"] for row in sorted(rows)]

    def _selection_changed(self, *_args) -> None:
        self.detail.commit()
        ids = self._selected_ids()
        if len(ids) == 1:
            self.detail.show_item(self.catalog.get_item(ids[0]))
            self._update_detail_cover()
        elif ids:
            self.detail.show_multiple(len(ids))
        else:
            self.detail.show_item(None)

    def _update_detail_cover(self) -> None:
        item = self.detail.item
        if item:
            self.detail.set_thumbnail(self.model._thumbnail(item))

    def _thumb_arrived(self, item_id: int, _key: str, _image: QImage) -> None:
        if self.detail.item and self.detail.item["id"] == item_id:
            self._update_detail_cover()

    def _item_saved(self, item_id: int) -> None:
        item = self.catalog.get_item(item_id)
        if item:
            self.model.update_item(item)
            self.status_label.setText(f"Guardado en el catálogo: {display_title(item)}")

    def _context_menu(self, position) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        view = self.stack.currentWidget()
        menu = QMenu(self)
        paths = [full_path(self.catalog.get_item(i)) for i in ids]
        menu.addAction("Copiar ruta", lambda: QApplication.clipboard().setText("\n".join(paths)))
        if len(ids) > 1:
            menu.addAction("Editar varios…", lambda: self._bulk_edit(ids))
        menu.addAction("Añadir etiqueta…", lambda: self._bulk_tag(ids))
        collection_menu = menu.addMenu("Añadir a colección")
        for collection in self.catalog.collections():
            collection_menu.addAction(collection["name"], lambda c=collection["id"]: self._add_to_collection(c, ids))
        collection_menu.addSeparator()
        collection_menu.addAction("Nueva colección…", lambda: self._add_to_collection(None, ids))
        kind, value = self._current_filter()
        if kind == "collection":
            menu.addAction("Quitar de esta colección", lambda: self._remove_from_collection(value, ids))
        rating = menu.addMenu("Valorar")
        for value in range(6):
            rating.addAction(stars(value) if value else "Sin valorar",
                             lambda v=value: self._bulk_rating(ids, v))
        menu.addSeparator()
        menu.addAction("Escribir ediciones en archivo…", lambda: self._write_files(ids))
        menu.addAction("Quitar del catálogo…", lambda: self._remove_items(ids))
        menu.exec(view.viewport().mapToGlobal(position))

    def _bulk_edit(self, ids) -> None:
        self.detail.commit()
        items = [self.catalog.get_item(i) for i in ids]
        dialog = dialogs.BulkEditDialog(items, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.values():
            return
        self.catalog.bulk_edit(ids, dialog.values())
        self._refresh_sidebar()
        self.reload()
        self.status_label.setText(f"Editados {len(ids)} elementos en el catálogo.")

    def _add_to_collection(self, collection_id, ids) -> None:
        self.detail.commit()
        if collection_id is None:
            name, ok = QInputDialog.getText(self, "Nueva colección", "Nombre:")
            if not ok or not name.strip():
                return
            collection_id = self.catalog.create_collection(name)
            self._refresh_sidebar()
        self.catalog.add_to_collection(collection_id, ids)
        self.status_label.setText(f"Añadidos {len(ids)} elementos a la colección.")

    def _remove_from_collection(self, collection_id, ids) -> None:
        self.detail.commit()
        self.catalog.remove_from_collection(collection_id, ids)
        self.reload()

    def _sidebar_menu(self, position) -> None:
        node = self.sidebar.itemAt(position)
        data = node.data(0, Qt.ItemDataRole.UserRole) if node else None
        if not data or data[0] != "collection":
            return
        menu = QMenu(self)
        menu.addAction("Borrar colección…", lambda: self._delete_collection(data[1], node.text(0)))
        menu.exec(self.sidebar.viewport().mapToGlobal(position))

    def _delete_collection(self, collection_id: int, name: str) -> None:
        if dialogs.confirm(self, "Borrar colección", f"¿Borrar la colección «{name}»?\n\n"
                                                     "Sus elementos siguen en el catálogo."):
            self.detail.commit()
            self.catalog.delete_collection(collection_id)
            self.sidebar.clearSelection()
            self._refresh_sidebar()
            self.reload()

    def _bulk_tag(self, ids) -> None:
        self.detail.commit()
        name, ok = QInputDialog.getText(self, "Añadir etiqueta", f"Etiqueta para {len(ids)} elemento(s):")
        if ok and name.strip():
            self.catalog.add_tag(ids, name)
            self._refresh_sidebar()
            self.reload()

    def _bulk_rating(self, ids, rating: int) -> None:
        self.detail.commit()
        self.catalog.set_rating(ids, rating)
        self.reload()

    def _remove_items(self, ids) -> None:
        answer = QMessageBox.question(
            self, "Quitar del catálogo",
            f"¿Quitar {len(ids)} elemento(s) del catálogo?\n\nLos archivos no se borran; "
            "volverán a aparecer sin tus datos si siguen ahí en el próximo escaneo.")
        if answer == QMessageBox.StandardButton.Yes:
            self.detail.show_item(None)
            self.catalog.remove_items(ids)
            self._refresh_sidebar()
            self.reload()

    # Trabajos largos

    def _start_worker(self, worker: QThread) -> None:
        self.worker = worker
        worker.progress.connect(self.status_label.setText)
        self.scan_action.setEnabled(False)
        self.cancel_button.show()
        worker.start()

    def _worker_finished(self) -> None:
        self.worker.wait()
        self.worker = None
        self.scan_action.setEnabled(True)
        self.cancel_button.hide()
        self._refresh_sidebar()
        self.reload()

    def _busy(self) -> bool:
        if self.worker:
            QMessageBox.information(self, "Trabajo en curso", "Espera a que termine el trabajo actual.")
            return True
        return False

    def _add_root(self, mode: str) -> None:
        if self._busy():
            return
        title = "Carpeta de juegos (cada subcarpeta es un juego)" if mode == "games" else "Carpeta de la colección"
        path = QFileDialog.getExistingDirectory(self, title)
        if path:
            self.scan([self.catalog.add_root(path, mode)])

    def scan_all(self) -> None:
        if not self._busy():
            self.scan([root["id"] for root in self.catalog.roots()])

    def scan(self, root_ids: list[int]) -> None:
        if not root_ids:
            self.status_label.setText("Añade primero una carpeta.")
            return
        self.detail.commit()
        worker = ScanWorker(self.catalog.path, root_ids, self)
        worker.done.connect(self._scan_done)
        self._start_worker(worker)

    def _scan_done(self, results: list, offline: list) -> None:
        self._worker_finished()
        totals = {name: sum(getattr(r, name) for r in results)
                  for name in ("added", "updated", "moved", "missing", "restored")}
        errors = [e for r in results for e in r.errors]
        self.status_label.setText(
            f"Escaneo: {totals['added']} nuevos, {totals['updated']} actualizados, {totals['moved']} movidos, "
            f"{totals['missing']} faltantes, {totals['restored']} recuperados"
            + (" (cancelado)" if any(r.cancelled for r in results) else ""))
        if offline:
            QMessageBox.warning(self, "Carpetas no disponibles",
                                "No se pudo acceder a estas carpetas, y sus datos no se han tocado:\n\n"
                                + "\n".join(offline))
        if errors:
            QMessageBox.warning(self, "Escaneo incompleto",
                                f"Hubo {len(errors)} error(es) de lectura, así que no se marcó nada como "
                                "faltante:\n\n" + "\n".join(errors[:10]))

    def _write_files(self, ids: list[int]) -> None:
        if self._busy():
            return
        self.detail.commit()
        items = [self.catalog.get_item(i) for i in ids]
        pending = [i for i in items if metadata.pending_edits(i, full_path(i))]
        if not pending:
            QMessageBox.information(self, "Nada que escribir", "No hay ediciones que este formato pueda guardar.")
            return
        answer = QMessageBox.question(
            self, "Escribir en archivo",
            f"Se modificarán {len(pending)} archivo(s).\n\nCambiarán su fecha y su contenido, por lo que tu "
            "sistema de respaldo los volverá a copiar. Los vídeos grandes pueden tardar en una carpeta de red.\n\n"
            "¿Continuar?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        worker = WriteWorker(self.catalog.path, [i["id"] for i in pending], self)
        worker.done.connect(self._write_done)
        self._start_worker(worker)

    def _write_done(self, written: int, errors: list) -> None:
        self._worker_finished()
        self.status_label.setText(f"Escritos {written} archivo(s)")
        if errors:
            QMessageBox.warning(self, "Escritura incompleta",
                                "Lo que no se pudo escribir sigue guardado en el catálogo:\n\n"
                                + "\n".join(errors[:10]))

    # Mantenimiento

    def _run_maintenance(self, function, on_done) -> None:
        if self._busy():
            return
        self.detail.commit()
        worker = MaintenanceWorker(self.catalog.path, function, self)
        worker.done.connect(on_done)
        self._start_worker(worker)

    def _verify(self) -> None:
        if dialogs.confirm(self, "Comprobar integridad",
                           "Se leerá cada archivo completo, sin modificarlo. En una colección grande o por red "
                           "puede tardar mucho, aunque se puede cancelar.\n\n¿Continuar?"):
            self._run_maintenance(maintenance.verify_integrity, self._verify_done)

    def _verify_done(self, result: maintenance.VerifyResult) -> None:
        self._worker_finished()
        groups = []
        if result.corrupted:
            groups.append((f"Posiblemente dañados ({len(result.corrupted)})", result.corrupted))
        if result.unreadable:
            groups.append((f"No se pudieron leer ({len(result.unreadable)})", result.unreadable))
        summary = (f"Correctos: {result.ok}. Huella nueva o renovada: {result.baseline}. "
                   f"Carpetas de juego omitidas: {result.skipped}.")
        if result.cancelled:
            summary += " Comprobación cancelada: el resultado es parcial."
        if result.corrupted:
            summary += ("\n\nEstos archivos cambiaron sin que cambiara su fecha, lo que suele indicar daño en "
                        "el disco. Compáralos con tu respaldo antes de que se sobrescriba.")
        elif result.baseline and not result.ok:
            summary += "\n\nLa primera comprobación solo guarda las huellas; la siguiente ya podrá detectar daños."
        dialogs.ReportDialog("Integridad de la colección", summary, groups, reveal_path, self).exec()

    def _find_duplicates(self) -> None:
        self._run_maintenance(maintenance.find_duplicates, self._duplicates_done)

    def _duplicates_done(self, result: maintenance.DuplicateResult) -> None:
        self._worker_finished()
        groups = [(f"{Path(paths[0]).name} ({len(paths)} copias)", paths) for paths in result.groups]
        if result.unreadable:
            groups.append((f"No se pudieron leer ({len(result.unreadable)})", result.unreadable))
        summary = f"{len(result.groups)} grupos de archivos idénticos. Espacio repetido: {human_size(result.wasted)}."
        if result.cancelled:
            summary += " Búsqueda cancelada: el resultado es parcial."
        if result.groups:
            summary += "\nMikoshi no borra nada. Doble clic para ver un archivo en su carpeta."
        dialogs.ReportDialog("Duplicados", summary, groups, reveal_path, self).exec()

    def _automatic_backup(self) -> None:
        folder = self.catalog.setting("backup_folder")
        last = float(self.catalog.setting("last_backup", "0"))
        if not folder or time.time() - last < BACKUP_INTERVAL:
            return
        try:
            if not os.path.isdir(folder):
                raise OSError(f"no se encuentra {folder}")
            self.catalog.backup_to(folder)
            self.catalog.set_setting("last_backup", str(time.time()))
            self.catalog.set_setting("backup_error", "")
        except (OSError, sqlite3.Error) as exc:
            self.catalog.set_setting("backup_error", str(exc))

    def _report_backup_error(self) -> None:
        error = self.catalog.setting("backup_error")
        if error:
            self.status_label.setText(f"La última copia automática falló: {error}")

    # Otros

    def _open_settings(self) -> None:
        self.detail.commit()
        dialogs.SettingsDialog(self.catalog, self).exec()

    def _manage_roots(self) -> None:
        if self._busy():
            return
        self.detail.commit()
        RootsDialog(self.catalog, self).exec()
        self._refresh_sidebar()
        self.reload()

    def _backup(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta para la copia (por ejemplo, en el servidor)")
        if not folder:
            return
        self.detail.commit()
        try:
            dest = self.catalog.backup_to(folder)
        except Exception as exc:
            QMessageBox.warning(self, "No se pudo copiar", str(exc))
            return
        self.status_label.setText(f"Copia guardada en {dest}")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Exportar lo que se muestra", "catalogo.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            count = self.catalog.export_csv(path, self.model.items)
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo exportar", str(exc))
            return
        self.status_label.setText(f"Exportados {count} elementos a {path}")

    def closeEvent(self, event) -> None:
        self.detail.commit()
        if self.worker:
            self.worker.requestInterruption()
            self.worker.wait()
        self.model.pool.clear()
        self.model.pool.waitForDone()
        dialogs.wait_for_tasks()
        self.catalog.set_setting("zoom", str(self.grid.iconSize().width()))
        self._automatic_backup()
        self.catalog.close()
        super().closeEvent(event)


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Mikoshi")
    apply_theme(app)
    window = MainWindow()
    window.show()
    return app.exec()

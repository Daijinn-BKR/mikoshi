"""Interfaz de escritorio."""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QLibraryInfo, QModelIndex, QRect, QSize, Qt, QThread, QTranslator, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QIcon, QImage, QIntValidator, QKeySequence, QPainter, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSplitter, QStackedWidget, QStyle,
    QStyledItemDelegate, QTableView, QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import dialogs, maintenance, metadata, online, scanner, thumbnails
from .db import Catalog, display_title, effective, full_path
from .nowplaying import NowPlayingBar
from .player import PLAYABLE, PlayerDialog, PlayerHooks
from .sections import BooksSection, CinemaSection, FavoritesSection, GamesSection, MusicSection
from .theme import apply_theme
from .widgets import Thumbs, creator, human_size, number, open_path, reveal_path, stars, year

BACKUP_INTERVAL = 24 * 3600
SORT_OPTIONS = [
    ("Título", 0, Qt.SortOrder.AscendingOrder), ("Creador", 2, Qt.SortOrder.AscendingOrder),
    ("Año (recientes)", 3, Qt.SortOrder.DescendingOrder), ("Valoración", 4, Qt.SortOrder.DescendingOrder),
    ("Tamaño", 6, Qt.SortOrder.DescendingOrder), ("Tipo", 1, Qt.SortOrder.AscendingOrder),
]
COLUMNS = ["Título", "Tipo", "Creador", "Año", "Valoración", "Etiquetas", "Tamaño", "Ruta"]


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
    done = Signal(int, list, list)

    def __init__(self, db_path: str, item_ids: list[int], parent=None):
        super().__init__(parent)
        self.db_path, self.item_ids = db_path, item_ids

    def run(self) -> None:
        catalog = Catalog(self.db_path)
        written, errors, verified = 0, [], []
        for index, item_id in enumerate(self.item_ids, 1):
            if self.isInterruptionRequested():
                break
            item = catalog.get_item(item_id)
            name = Path(item["rel_path"]).name
            labels = metadata.field_labels(item["kind"])
            self.progress.emit(f"Escribiendo {index}/{len(self.item_ids)} · {name}")
            try:
                # Lo confirmado se ha vuelto a leer del archivo después de escribirlo.
                confirmed, rejected = scanner.write_and_verify(catalog, item)
                written += 1
                if confirmed:
                    fresh = catalog.get_item(item_id)["file_meta"]
                    verified.append(f"{name}: " + ", ".join(
                        f"{labels[k]} = «{fresh.get(k, '') or 'vacío'}» ✓" for k in confirmed))
                if rejected:
                    errors.append(f"{name}: el formato no conservó {', '.join(labels[k] for k in rejected)}. "
                                  "Revisa el valor (temporada y episodio deben ser números).")
            except (metadata.MetadataError, OSError) as exc:
                errors.append(f"{name}: {exc}")
        catalog.close()
        self.done.emit(written, errors, verified)


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


# Modelo de datos, compartido por la cuadrícula y la lista

class ItemsModel(QAbstractTableModel):
    def __init__(self, thumbs: Thumbs, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []
        self.sort_column, self.sort_order = 0, Qt.SortOrder.AscendingOrder
        self.natural: str | None = None
        self.thumbs = thumbs
        # Atajos a la caché compartida de miniaturas.
        self.pixmaps, self.pool, self.signals = thumbs.pixmaps, thumbs.pool, thumbs.signals
        thumbs.updated.connect(self._thumb_ready)

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

    def _thumbnail(self, item: dict) -> QPixmap:
        return self.thumbs.get(item)

    def _thumb_ready(self, item_id: int) -> None:
        for row, item in enumerate(self.items):
            if item["id"] == item_id:
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])
                break

    _number = staticmethod(number)

    def _sorted(self, items: list[dict]) -> list[dict]:
        if self.natural == "list":
            return list(items)
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
        self.verify_button = QPushButton("Comprobar qué hay en el archivo")
        self.verify_button.clicked.connect(self._verify_file)
        layout.addWidget(self.verify_button)
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
            if isinstance(widget, QComboBox):
                value = widget.currentData()
            else:
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
        self.commit()
        thumbnails.save_cover(self.catalog, self.item, data, suffix)
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
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(max(widget.findData(value), 0))
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
                       self.tags, self.notes, self.save_button, self.verify_button):
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
        favorite = item["root_id"] in self.catalog.favorite_root_ids()
        for key, label in metadata.fields_for(item["kind"], favorite):
            if key == "description":
                widget = QPlainTextEdit(meta.get(key, ""))
                widget.setFixedHeight(80)
                widget.textChanged.connect(self._mark_dirty)
            elif key in metadata.CHOICES:
                widget = QComboBox()
                for value, text in metadata.CHOICES[key]:
                    widget.addItem(text, value)
                widget.setCurrentIndex(max(widget.findData(meta.get(key, "")), 0))
                widget.currentIndexChanged.connect(self._mark_dirty)
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

    def _verify_file(self) -> None:
        """Lee el archivo en este momento y lo compara con lo que muestra el catálogo."""
        if not self.item:
            return
        self.commit()
        path = full_path(self.item)
        if not os.path.exists(path):
            QMessageBox.warning(self, "No disponible", f"No se encuentra:\n{path}")
            return
        rows = metadata.verify_in_file(self.item, path)
        if not rows:
            QMessageBox.information(self, "Comprobar archivo",
                                    "Este formato no guarda metadatos en el archivo: todo vive en el catálogo.")
            return
        lines = [f"{'✓' if ok else '✗'} {label}: archivo «{in_file or 'vacío'}»"
                 + ("" if ok else f" · catálogo «{shown or 'vacío'}»") for label, shown, in_file, ok in rows]
        pending = sum(1 for row in rows if not row[3])
        summary = ("Todo lo que ves está escrito en el archivo." if not pending else
                   f"{pending} campo(s) solo están en el catálogo. Usa «Escribir en archivo» para guardarlos.")
        QMessageBox.information(self, "Comprobar archivo", summary + "\n\n" + "\n".join(lines))

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

        self.thumbs = Thumbs(self)
        self.model = ItemsModel(self.thumbs, self)
        self.model.signals.ready.connect(self._thumb_arrived)
        self.now_playing = NowPlayingBar(self.catalog, self.thumbs)

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

        self.sections = [CinemaSection(self), MusicSection(self), FavoritesSection(self), BooksSection(self),
                         GamesSection(self)]
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for section in self.sections:
            self.tabs.addTab(section, section.title)
        self.tabs.addTab(splitter, "Todo")
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.tabs, 1)
        central_layout.addWidget(self.now_playing)
        self.setCentralWidget(central)

        self._build_actions()
        self._refresh_sidebar()
        self.reload()
        self.previous_tab = None
        self.tabs.currentChanged.connect(self._tab_changed)
        saved = self.catalog.setting("tab", "Todo")
        titles = [self.tabs.tabText(i) for i in range(self.tabs.count())]
        self.tabs.setCurrentIndex(titles.index(saved) if saved in titles else self.tabs.count() - 1)
        self._tab_changed(self.tabs.currentIndex())
        self._report_backup_error()

    # Secciones

    def _tab_changed(self, index: int) -> None:
        if self.previous_tab is not None:
            self.previous_tab.leave()
        widget = self.tabs.widget(index)
        self.previous_tab = widget if widget in self.sections else None
        if widget in self.sections:
            self.detail.commit()
            widget.refresh()
        else:
            self.reload()

    def data_changed(self) -> None:
        """Algo cambió en el catálogo: refresca la sección que se está viendo."""
        current = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        if current in getattr(self, "sections", []):
            current.refresh()

    def release_files(self, paths: set[str]) -> None:
        """Los reproductores de Mikoshi sueltan estos archivos: Windows no deja sustituir uno abierto."""
        released = self.now_playing.release(paths)
        for section in self.sections:
            released = section.release(paths) or released
        if released:
            self.status_label.setText("Reproducción detenida para poder escribir en el archivo.")

    def status(self, text: str) -> None:
        self.status_label.setText(text)

    def play_items(self, items: list[dict], index: int, resume: bool = False) -> None:
        """La música va a la barra fija; vídeo y fotos se abren en su ventana."""
        item = items[index]
        if item["kind"] == "audio":
            audio = [i for i in items if i["kind"] == "audio"]
            self.now_playing.play(audio, audio.index(item))
            return
        self.now_playing.pause()
        dialog = PlayerDialog(items, index, open_path, self, hooks=PlayerHooks(self.catalog, resume))
        dialog.exec()
        # Se libera al cerrar: cada reproductor tiene su propio decodificador y su filtro de eventos.
        dialog.deleteLater()

    def edit_item(self, item_id: int) -> None:
        item = self.catalog.get_item(item_id)
        if not item:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Editar · {display_title(item)}")
        dialog.resize(460, 760)
        panel = DetailPanel(self.catalog, dialog)
        panel.show_item(item)
        panel.set_thumbnail(self.thumbs.get(item))
        panel.saved.connect(self._item_saved)
        panel.saved.connect(lambda _id: panel.set_thumbnail(self.thumbs.get(panel.item)))
        panel.write_requested.connect(lambda: self._write_files([item_id]))
        panel.play_requested.connect(lambda: self.play_items([panel.item], 0))
        layout = QVBoxLayout(dialog)
        layout.addWidget(panel, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.finished.connect(lambda _r: panel.commit())
        self.editor = panel
        dialog.exec()
        self.editor = None

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

        def section(title, expanded=True, key=None):
            node = QTreeWidgetItem(self.sidebar, [title])
            node.setFlags(Qt.ItemFlag.ItemIsEnabled)
            node.setData(0, Qt.ItemDataRole.UserRole, ("section", key))
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

        library = section("Biblioteca", key="library")
        entry(library, "Todo", ("all", None))
        for kind, label in metadata.KIND_LABELS.items():
            entry(library, label, ("kind", kind))
        state = section("Estado")
        entry(state, "Sin escribir en archivo", ("pending", None))
        entry(state, "Faltantes", ("missing", None))
        lists_node = section("Mis listas", key="lists")
        folders = self.catalog.list_folders()
        lists = self.catalog.collections()

        def fill(parent, folder_id):
            for folder in folders:
                if folder["parent_id"] == folder_id:
                    node = QTreeWidgetItem(parent, [f"📁 {folder['name']}"])
                    node.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    node.setData(0, Qt.ItemDataRole.UserRole, ("list_folder", folder["id"]))
                    fill(node, folder["id"])
                    node.setExpanded(True)
            for item in lists:
                if item["folder_id"] == folder_id:
                    mark = "♫" if item["kind"] == "playlist" else "▦"
                    entry(parent, f"{mark} {item['name']}", ("collection", item["id"]))

        fill(lists_node, None)
        lists_node.setExpanded(True)
        for title, field in (("Series", "series"), ("Álbumes", "album")):
            names = self.catalog.group_names(field)
            if names:
                group_node = section(title, expanded=False, key=field)
                for name in names:
                    entry(group_node, name, (field, name))
        tags = self.catalog.all_tags()
        if tags:
            tag_node = section("Etiquetas")
            for tag in tags:
                entry(tag_node, tag, ("tag", tag))
        root_node = section("Carpetas en disco", key="roots")
        for root in self.catalog.roots():
            node = entry(root_node, Path(root["path"]).name or root["path"], ("root", root["id"]))
            node.setToolTip(0, root["path"])
        root_node.setExpanded(True)
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
        order = None
        if kind == "collection":
            args["collection"] = value
            if self.catalog.collection(value)["kind"] == "playlist":
                order = self.catalog.list_order(value)
                self.model.natural = "list"
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
        if order is not None:
            place = {item_id: n for n, item_id in enumerate(order)}
            items.sort(key=lambda i: place[i["id"]])
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
        # La cola es lo que se está viendo: del mismo tipo o, en una playlist, todo lo reproducible.
        if self.model.natural == "list":
            queue = [i for i in self.model.items if i["kind"] in PLAYABLE]
        else:
            queue = [i for i in self.model.items if i["kind"] == item["kind"]]
        index = next((n for n, i in enumerate(queue) if i["id"] == item["id"]), None)
        if index is None:
            queue, index = [item], 0
        self.play_items(queue, index)

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
        if ids:
            view = self.stack.currentWidget()
            self._item_menu(ids).exec(view.viewport().mapToGlobal(position))

    def _item_menu(self, ids: list[int]) -> QMenu:
        menu = QMenu(self)
        paths = [full_path(self.catalog.get_item(i)) for i in ids]
        menu.addAction("Copiar ruta", lambda: QApplication.clipboard().setText("\n".join(paths)))
        if len(ids) > 1:
            menu.addAction("Editar varios…", lambda: self._bulk_edit(ids))
        menu.addAction("Añadir etiqueta…", lambda: self._bulk_tag(ids))
        collection_menu = menu.addMenu("Añadir a lista")
        for collection in self.catalog.collections():
            mark = "♫" if collection["kind"] == "playlist" else "▦"
            collection_menu.addAction(f"{mark} {collection['name']}",
                                      lambda c=collection["id"]: self._add_to_collection(c, ids))
        collection_menu.addSeparator()
        collection_menu.addAction("Nueva playlist…", lambda: self._add_to_collection(None, ids, "playlist"))
        collection_menu.addAction("Nueva colección…", lambda: self._add_to_collection(None, ids))
        kind, value = self._current_filter()
        if kind == "collection":
            if self.model.natural == "list" and len(ids) == 1:
                menu.addAction("Subir en la playlist", lambda: self._move_in_list(value, ids[0], -1))
                menu.addAction("Bajar en la playlist", lambda: self._move_in_list(value, ids[0], 1))
            menu.addAction("Quitar de esta lista", lambda: self._remove_from_collection(value, ids))
        rating = menu.addMenu("Valorar")
        for value in range(6):
            rating.addAction(stars(value) if value else "Sin valorar",
                             lambda v=value: self._bulk_rating(ids, v))
        menu.addSeparator()
        menu.addAction("Escribir ediciones en archivo…", lambda: self._write_files(ids))
        menu.addAction("Quitar del catálogo…", lambda: self._remove_items(ids))
        return menu

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

    def _add_to_collection(self, collection_id, ids, kind="collection") -> None:
        self.detail.commit()
        if collection_id is None:
            collection_id = self._create_list(kind)
            if collection_id is None:
                return
        self.catalog.add_to_collection(collection_id, ids)
        self.status_label.setText(f"Añadidos {len(ids)} elementos a la lista.")

    def _remove_from_collection(self, collection_id, ids) -> None:
        self.detail.commit()
        self.catalog.remove_from_collection(collection_id, ids)
        self.reload()

    def _move_in_list(self, collection_id: int, item_id: int, step: int) -> None:
        self.detail.commit()
        self.catalog.move_in_list(collection_id, item_id, step)
        self.reload()
        row = next(n for n, item in enumerate(self.model.items) if item["id"] == item_id)
        self.grid.setCurrentIndex(self.model.index(row, 0))

    # Menú de la barra lateral

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "Nombre:", text=initial)
        name = name.strip()
        return name if ok and name else None

    def _after_sidebar_change(self) -> None:
        self.detail.commit()
        self._refresh_sidebar()
        self.reload()

    def _create_list(self, kind: str, folder_id: int | None = None) -> int | None:
        label = "playlist" if kind == "playlist" else "colección"
        name = self._ask_name(f"Nueva {label}")
        if not name:
            return None
        if self.catalog.list_name_exists(name):
            QMessageBox.information(self, "Nombre ocupado", f"Ya hay una lista llamada «{name}».")
            return None
        collection_id = self.catalog.create_collection(name, kind, folder_id)
        self._after_sidebar_change()
        return collection_id

    def _create_folder(self, parent_id: int | None = None) -> None:
        name = self._ask_name("Nueva carpeta de listas")
        if name:
            self.catalog.create_folder(name, parent_id)
            self._after_sidebar_change()

    def _rename_list(self, collection_id: int) -> None:
        current = self.catalog.collection(collection_id)["name"]
        name = self._ask_name("Renombrar lista", current)
        if not name:
            return
        if self.catalog.list_name_exists(name, excluding=collection_id):
            QMessageBox.information(self, "Nombre ocupado", f"Ya hay una lista llamada «{name}».")
            return
        self.catalog.rename_collection(collection_id, name)
        self._after_sidebar_change()

    def _delete_collection(self, collection_id: int, name: str) -> None:
        if dialogs.confirm(self, "Borrar lista", f"¿Borrar la lista «{name}»?\n\n"
                                                 "Sus elementos y archivos no se tocan."):
            self.detail.commit()
            self.catalog.delete_collection(collection_id)
            self.sidebar.clearSelection()
            self._after_sidebar_change()

    def _move_list(self, collection_id: int, folder_id: int | None) -> None:
        self.catalog.move_collection(collection_id, folder_id)
        self._after_sidebar_change()

    def _rename_folder(self, folder_id: int, current: str) -> None:
        name = self._ask_name("Renombrar carpeta", current)
        if name:
            self.catalog.rename_folder(folder_id, name)
            self._after_sidebar_change()

    def _move_folder(self, folder_id: int, parent_id: int | None) -> None:
        self.catalog.move_folder(folder_id, parent_id)
        self._after_sidebar_change()

    def _delete_folder(self, folder_id: int, name: str) -> None:
        if dialogs.confirm(self, "Borrar carpeta", f"¿Borrar la carpeta «{name}»?\n\n"
                                                   "Las listas que contiene no se borran: suben un nivel."):
            self.catalog.delete_folder(folder_id)
            self._after_sidebar_change()

    def _play_list(self, collection_id: int) -> None:
        self.detail.commit()
        place = {item_id: n for n, item_id in enumerate(self.catalog.list_order(collection_id))}
        items = sorted(self.catalog.query_items(collection=collection_id), key=lambda i: place[i["id"]])
        queue = [i for i in items if i["kind"] in PLAYABLE]
        if not queue:
            QMessageBox.information(self, "Nada que reproducir", "Esta lista no tiene vídeo, música ni fotos.")
            return
        self.play_items(queue, 0)

    def _rename_tag(self, tag: str) -> None:
        name = self._ask_name("Renombrar etiqueta", tag)
        if name and name != tag:
            self.catalog.rename_tag(tag, name)
            self.sidebar.clearSelection()
            self._after_sidebar_change()

    def _delete_tag(self, tag: str) -> None:
        if dialogs.confirm(self, "Borrar etiqueta", f"¿Quitar la etiqueta «{tag}» de todos sus elementos?"):
            self.detail.commit()
            self.catalog.delete_tag(tag)
            self.sidebar.clearSelection()
            self._after_sidebar_change()

    def _rename_group(self, field: str, current: str) -> None:
        label = "serie" if field == "series" else "álbum"
        name = self._ask_name(f"Renombrar {label}", current)
        if name and name != current:
            self.detail.commit()
            ids = [i["id"] for i in self.catalog.query_items(group=(field, current))]
            self.catalog.bulk_edit(ids, {field: name})
            self.sidebar.clearSelection()
            self._after_sidebar_change()

    def _set_root_mode(self, root_id: int, mode: str) -> None:
        self.catalog.set_root_mode(root_id, mode)
        self._after_sidebar_change()
        self.status_label.setText("Carpeta asignada a Videos favoritos." if mode == "favorites"
                                  else "La carpeta ya no es de Videos favoritos.")

    def _relocate_root(self, root_id: int) -> None:
        if self._busy():
            return
        path = QFileDialog.getExistingDirectory(self, "Nueva ubicación", self.catalog.root(root_id)["path"])
        if path:
            self.catalog.relocate_root(root_id, path)
            self._after_sidebar_change()

    def _remove_root(self, root_id: int) -> None:
        if self._busy():
            return
        path = self.catalog.root(root_id)["path"]
        if dialogs.confirm(self, "Quitar carpeta",
                           f"¿Quitar {path} del catálogo?\n\nSe perderán sus etiquetas, valoraciones y ediciones "
                           "no escritas. Los archivos no se tocan."):
            self.detail.commit()
            self.detail.show_item(None)
            self.catalog.remove_root(root_id)
            self.sidebar.clearSelection()
            self._after_sidebar_change()

    def _open_root(self, root_id: int) -> None:
        path = self.catalog.root(root_id)["path"]
        try:
            open_path(path)
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo abrir", f"{path}\n\n{exc}")

    def _scan_root(self, root_id: int) -> None:
        if not self._busy():
            self.scan([root_id])

    def _write_all_pending(self) -> None:
        items = self.catalog.query_items()
        self._write_files([i["id"] for i in items if metadata.pending_edits(i, full_path(i))])

    def _remove_all_missing(self) -> None:
        ids = [i["id"] for i in self.catalog.query_items(status="missing")]
        if not ids:
            QMessageBox.information(self, "Sin faltantes", "No hay elementos faltantes.")
            return
        self._remove_items(ids)

    def _move_targets(self, move, excluded: set[int] = frozenset()) -> list:
        targets = [("Sin carpeta", lambda: move(None))]
        for folder in self.catalog.list_folders():
            if folder["id"] not in excluded:
                targets.append((folder["name"], lambda f=folder["id"]: move(f)))
        return targets

    def _sidebar_actions(self, data) -> list:
        """Opciones de cada nodo: (texto, función), (texto, [subopciones]) o None como separador."""
        kind, value = data
        if kind == "section":
            if value == "lists":
                return [("Nueva playlist…", lambda: self._create_list("playlist")),
                        ("Nueva colección…", lambda: self._create_list("collection")),
                        ("Nueva carpeta…", self._create_folder)]
            if value == "roots":
                return [("Añadir carpeta…", lambda: self._add_root("auto")),
                        ("Añadir carpeta de juegos…", lambda: self._add_root("games")),
                        ("Escanear todas", self.scan_all),
                        ("Gestionar carpetas…", self._manage_roots)]
            if value == "library":
                return [("Escanear todo", self.scan_all)]
            return []
        if kind in ("all", "kind"):
            return [("Escanear todo", self.scan_all)]
        if kind == "pending":
            return [("Escribir todo en archivos…", self._write_all_pending)]
        if kind == "missing":
            return [("Quitar todos del catálogo…", self._remove_all_missing)]
        if kind == "list_folder":
            name = next(f["name"] for f in self.catalog.list_folders() if f["id"] == value)
            return [("Nueva playlist aquí…", lambda: self._create_list("playlist", value)),
                    ("Nueva colección aquí…", lambda: self._create_list("collection", value)),
                    ("Nueva subcarpeta…", lambda: self._create_folder(value)), None,
                    ("Renombrar…", lambda: self._rename_folder(value, name)),
                    ("Mover a", self._move_targets(lambda p: self._move_folder(value, p),
                                                   self.catalog.folder_descendants(value))),
                    ("Borrar carpeta…", lambda: self._delete_folder(value, name))]
        if kind == "collection":
            info = self.catalog.collection(value)
            play = [("Reproducir", lambda: self._play_list(value)), None]
            return (play + [
                ("Renombrar…", lambda: self._rename_list(value)),
                ("Mover a", self._move_targets(lambda f: self._move_list(value, f))),
                ("Borrar lista…", lambda: self._delete_collection(value, info["name"])),
            ])
        if kind == "tag":
            return [("Renombrar…", lambda: self._rename_tag(value)),
                    ("Borrar etiqueta…", lambda: self._delete_tag(value))]
        if kind in ("series", "album"):
            return [("Renombrar…", lambda: self._rename_group(kind, value))]
        if kind == "root":
            favorite = self.catalog.root(value)["mode"] == "favorites"
            mode_action = (("Dejar de usar como Videos favoritos", lambda: self._set_root_mode(value, "auto"))
                           if favorite else
                           ("Usar como Videos favoritos", lambda: self._set_root_mode(value, "favorites")))
            return [("Escanear esta carpeta", lambda: self._scan_root(value)), mode_action,
                    ("Abrir en el explorador", lambda: self._open_root(value)), None,
                    ("Cambiar ruta…", lambda: self._relocate_root(value)),
                    ("Quitar del catálogo…", lambda: self._remove_root(value))]
        return []

    def _fill_menu(self, menu: QMenu, actions: list) -> None:
        for action in actions:
            if action is None:
                menu.addSeparator()
            elif isinstance(action[1], list):
                self._fill_menu(menu.addMenu(action[0]), action[1])
            else:
                menu.addAction(action[0], action[1])

    def _sidebar_menu(self, position) -> None:
        node = self.sidebar.itemAt(position)
        data = node.data(0, Qt.ItemDataRole.UserRole) if node else ("section", "lists")
        actions = self._sidebar_actions(tuple(data)) if data else []
        if actions:
            menu = QMenu(self)
            self._fill_menu(menu, actions)
            menu.exec(self.sidebar.viewport().mapToGlobal(position))

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
        self.data_changed()

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
        self.release_files({os.path.normcase(full_path(i)) for i in pending})
        worker = WriteWorker(self.catalog.path, [i["id"] for i in pending], self)
        worker.done.connect(self._write_done)
        self._start_worker(worker)

    def _write_done(self, written: int, errors: list, verified: list) -> None:
        self._worker_finished()
        self.status_label.setText(f"Escritos {written} archivo(s), comprobados al releerlos")
        groups = []
        if verified:
            groups.append((f"Comprobado en el archivo ({len(verified)})", verified))
        if errors:
            groups.append((f"Problemas ({len(errors)}): lo no escrito sigue en el catálogo", errors))
        summary = ("Después de escribir, Mikoshi volvió a leer cada archivo. Estos son los valores que contiene "
                   "ahora de verdad.")
        dialogs.ReportDialog("Resultado de la escritura", summary, groups, lambda _p: None, self).exec()

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
        for section in self.sections:
            section.leave()
        self.now_playing.stop()
        self.thumbs.shutdown()
        dialogs.wait_for_tasks()
        self.catalog.set_setting("tab", self.tabs.tabText(self.tabs.currentIndex()))
        self.catalog.set_setting("zoom", str(self.grid.iconSize().width()))
        self._automatic_backup()
        self.catalog.close()
        super().closeEvent(event)


def install_spanish(app) -> None:
    """Traduce al español los textos propios de Qt: botones Guardar, Cancelar, Sí, No…"""
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    app.translators = []
    for name in ("qtbase_es", "qt_es"):
        translator = QTranslator(app)
        if translator.load(name, folder):
            app.installTranslator(translator)
            app.translators.append(translator)


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Mikoshi")
    install_spanish(app)
    apply_theme(app)
    window = MainWindow()
    window.show()
    return app.exec()

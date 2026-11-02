"""Interfaz de escritorio."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject, QRect, QRunnable, QSize, Qt, QThread, QThreadPool, Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate,
    QTableView, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import metadata, scanner, thumbnails
from .db import Catalog, covers_dir, display_title, effective, full_path

KIND_STYLE = {
    "video": ("#3f6fb5", "VÍDEO"),
    "audio": ("#8a4fb0", "AUDIO"),
    "image": ("#3a9276", "IMAGEN"),
    "document": ("#b5793f", "LIBRO"),
    "game": ("#b54f5c", "JUEGO"),
}
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
    pixmap.fill(QColor(color))
    painter = QPainter(pixmap)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
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

    def _sorted(self, items: list[dict]) -> list[dict]:
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
        self.set_items(self.items)

    def update_item(self, item: dict) -> None:
        for row, current in enumerate(self.items):
            if current["id"] == item["id"]:
                self.items[row] = item
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
                break


class GridDelegate(QStyledItemDelegate):
    """Celda de tamaño fijo: miniatura centrada y título en dos líneas como máximo."""
    ICON = 150
    CELL = QSize(172, 192)

    def sizeHint(self, option, index):
        return self.CELL

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect
        selected = option.state & QStyle.StateFlag.State_Selected
        if selected:
            painter.fillRect(rect, option.palette.highlight())
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        icon_rect = QRect(rect.x(), rect.y() + 2, rect.width(), self.ICON)
        icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter)
        text_rect = QRect(rect.x() + 4, icon_rect.bottom() + 2, rect.width() - 8, rect.bottom() - icon_rect.bottom() - 2)
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

        actions = QHBoxLayout()
        self.open_button = QPushButton("Abrir")
        self.reveal_button = QPushButton("Ver en carpeta")
        self.cover_button = QPushButton("Portada…")
        for button in (self.open_button, self.reveal_button, self.cover_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.open_button.clicked.connect(lambda: self._launch(open_path))
        self.reveal_button.clicked.connect(lambda: self._launch(reveal_path))
        self.cover_button.clicked.connect(self._choose_cover)

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
        # Un nombre nuevo cada vez hace que la miniatura se regenere sola.
        dest = covers_dir() / f"{self.item['id']}-{int(time.time())}{Path(path).suffix.lower()}"
        try:
            shutil.copy(path, dest)
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo copiar", str(exc))
            return
        if self.item["cover"]:
            Path(self.item["cover"]).unlink(missing_ok=True)
        self.commit()
        self.catalog.save_user_data(self.item["id"], cover=str(dest))
        self.saved.emit(self.item["id"])
        self.show_item(self.catalog.get_item(self.item["id"]))

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        self.cover.setPixmap(pixmap.scaled(QSize(240, 240), Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))

    def show_multiple(self, count: int) -> None:
        self.show_item(None)
        self.heading.setText(f"{count} elementos seleccionados.\nUsa el clic derecho para etiquetar o valorar.")

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
    GRID, LIST = 0, 1

    def __init__(self, db_path=None):
        super().__init__()
        self.catalog = Catalog(db_path)
        self.worker: QThread | None = None
        self.setWindowTitle("Catálogo Multimedia")
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
        self.grid.setIconSize(QSize(150, 150))
        self.grid.setGridSize(QSize(176, 196))
        self.grid.setItemDelegate(GridDelegate(self.grid))
        self.grid.setSpacing(4)
        self.grid.setModel(self.model)

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
            view.doubleClicked.connect(lambda _index: self.detail._launch(open_path))
        self.grid.selectionModel().selectionChanged.connect(self._selection_changed)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.table)

        self.detail = DetailPanel(self.catalog)
        self.detail.saved.connect(self._item_saved)
        self.detail.tags_changed.connect(self._refresh_sidebar)
        self.detail.write_requested.connect(lambda: self._write_files(self._selected_ids()))

        splitter = QSplitter()
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.stack)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([210, 750, 360])
        self.setCentralWidget(splitter)

        self._build_actions()
        self._refresh_sidebar()
        self.reload()

    # Menús y barra

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
        quit_action = action("Salir", self.close, "Ctrl+Q")
        grid_action = action("Cuadrícula", lambda: self.stack.setCurrentIndex(self.GRID), "Ctrl+1")
        list_action = action("Lista", lambda: self.stack.setCurrentIndex(self.LIST), "Ctrl+2")

        file_menu = self.menuBar().addMenu("&Archivo")
        file_menu.addActions([self.add_action, self.add_games_action, manage])
        file_menu.addSeparator()
        file_menu.addActions([backup, export])
        file_menu.addSeparator()
        file_menu.addAction(quit_action)
        view_menu = self.menuBar().addMenu("&Ver")
        view_menu.addActions([grid_action, list_action])

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

        def section(title):
            node = QTreeWidgetItem(self.sidebar, [title])
            node.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = node.font(0)
            font.setBold(True)
            node.setFont(0, font)
            node.setExpanded(True)
            return node

        def entry(parent, text, data):
            node = QTreeWidgetItem(parent, [text])
            node.setData(0, Qt.ItemDataRole.UserRole, data)
            if data == current:
                node.setSelected(True)
            return node

        library = section("Biblioteca")
        entry(library, "Todo", ("all", None))
        for kind, label in metadata.KIND_LABELS.items():
            entry(library, label, ("kind", kind))
        state = section("Estado")
        entry(state, "Sin escribir en archivo", ("pending", None))
        entry(state, "Faltantes", ("missing", None))
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
        self.model.set_items(items)
        self.detail.show_item(None)
        self._show_count()

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
        menu.addAction("Añadir etiqueta…", lambda: self._bulk_tag(ids))
        rating = menu.addMenu("Valorar")
        for value in range(6):
            rating.addAction(stars(value) if value else "Sin valorar",
                             lambda v=value: self._bulk_rating(ids, v))
        menu.addSeparator()
        menu.addAction("Escribir ediciones en archivo…", lambda: self._write_files(ids))
        menu.addAction("Quitar del catálogo…", lambda: self._remove_items(ids))
        menu.exec(view.viewport().mapToGlobal(position))

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

    # Otros

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
        self.catalog.close()
        super().closeEvent(event)


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Catálogo Multimedia")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()

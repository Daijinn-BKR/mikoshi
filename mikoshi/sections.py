"""Secciones especializadas: Películas y series, Música, Videos favoritos, Libros y Juegos.

Cada una lee el mismo catálogo y pide a la ventana principal lo común: editar,
reproducir, el menú de un elemento y las tareas largas.
"""
from __future__ import annotations

import os
import re
import zlib
from collections import Counter, defaultdict
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QDate, QModelIndex, QSize, Qt
from PySide6.QtGui import QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QSplitter, QStackedWidget, QTableView,
    QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import dialogs, online, thumbnails
from .db import display_title, effective, full_path
from .player import PlayerHooks, PlayerWidget
from .widgets import (
    POSTER, SQUARE, WIDE, CardModel, card_view, clock, draw_cover, human_size, number, open_path,
    reveal_path, seconds, stars, video_category, year, ago,
)


def heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("title", True)
    return label


def muted(text: str = "") -> QLabel:
    label = QLabel(text, wordWrap=True)
    label.setProperty("muted", True)
    return label


def cover_pixmap(source: QPixmap, width: int, height: int) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw_cover(painter, source, pixmap.rect(), 8)
    painter.end()
    return pixmap


def matches(item: dict, text: str) -> bool:
    if not text:
        return True
    haystack = " ".join([display_title(item), item["rel_path"], item["tags"] or "",
                         *[str(v) for v in effective(item).values()]]).casefold()
    return all(word in haystack for word in text.casefold().split())


def chip(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setCheckable(True)
    button.setProperty("chip", True)
    return button


class IconBinder:
    """Pone la miniatura en las filas de una lista y la renueva cuando termina de crearse.

    Sin esto, la fila se queda para siempre con el recuadro de color provisional.
    """

    def __init__(self, thumbs):
        self.thumbs = thumbs
        self.entries: dict[int, list] = defaultdict(list)
        thumbs.updated.connect(self._updated)

    def clear(self, owner) -> None:
        for item_id in list(self.entries):
            self.entries[item_id] = [e for e in self.entries[item_id] if e[0] is not owner]

    def bind(self, owner, entry, item: dict, make=QIcon) -> None:
        entry.setIcon(make(self.thumbs.get(item)))
        self.entries[item["id"]].append((owner, entry, item, make))

    def _updated(self, item_id: int) -> None:
        for _owner, entry, item, make in self.entries.get(item_id, []):
            entry.setIcon(make(self.thumbs.get(item)))


class Section(QWidget):
    title = ""

    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main
        self.catalog = main.catalog
        self.thumbs = main.thumbs
        self.icons = IconBinder(self.thumbs)

    def refresh(self) -> None:
        raise NotImplementedError

    def leave(self) -> None:
        """Al salir de la sección: detener lo que esté sonando dentro de ella."""

    def release(self, paths: set[str]) -> bool:
        """Suelta los archivos de `paths` que la sección tenga abiertos."""
        return False

    def edit(self, item: dict) -> None:
        self.main.edit_item(item["id"])
        self.refresh()


# Música

class TrackModel(QAbstractTableModel):
    COLUMNS = ["#", "Título", "Artista", "Álbum", "Año", "Género", "Duración", "Valoración", "Escuchas"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []
        self.activity: dict = {}
        self.playing_id: int | None = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            meta = effective(item)
            track = number(meta.get("track"), 0)
            return [
                str(track) if track else "", display_title(item), meta.get("artist", ""), meta.get("album", ""),
                year(item), meta.get("genre", ""), clock(seconds(item)), stars(item["rating"]) if item["rating"] else "",
                str(self.activity.get(item["id"], {}).get("play_count", "") or ""),
            ][index.column()]
        if role == Qt.ItemDataRole.FontRole and item["id"] == self.playing_id:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (0, 6):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def set_items(self, items: list[dict], activity: dict) -> None:
        self.beginResetModel()
        self.items, self.activity = items, activity
        self.endResetModel()

    def set_playing(self, item_id: int | None) -> None:
        self.playing_id = item_id
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(self.COLUMNS) - 1))


class MusicSection(Section):
    title = "Música"
    BROWSE = [("artist", "Artistas"), ("album", "Álbumes"), ("genre", "Géneros"), ("year", "Años")]

    def __init__(self, main, parent=None):
        super().__init__(main, parent)
        self.tracks: list[dict] = []
        self.activity: dict = {}

        self.browse_mode = QComboBox()
        for _key, label in self.BROWSE:
            self.browse_mode.addItem(label)
        self.browser = QListWidget()
        self.browser.setIconSize(QSize(40, 40))
        left = QVBoxLayout()
        left.addWidget(self.browse_mode)
        left.addWidget(self.browser, 1)
        left_box = QWidget()
        left_box.setLayout(left)

        self.search = QLineEdit(placeholderText="Buscar canción, artista o álbum…", clearButtonEnabled=True)
        self.sort = QComboBox()
        self.sort.addItems(["Álbum", "Artista", "Título", "Año", "Género", "Añadidas recientemente", "Más escuchadas"])
        play_all = QPushButton("Reproducir")
        play_all.setProperty("primary", True)
        shuffle = QPushButton("Aleatorio")
        self.count = muted()
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(QLabel("Ordenar por"))
        top.addWidget(self.sort)
        top.addWidget(play_all)
        top.addWidget(shuffle)

        self.model = TrackModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((40, 260, 170, 200, 54, 120, 70, 90, 70)):
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        right = QVBoxLayout()
        right.addLayout(top)
        right.addWidget(self.table, 1)
        right.addWidget(self.count)
        right_box = QWidget()
        right_box.setLayout(right)

        splitter = QSplitter()
        splitter.addWidget(left_box)
        splitter.addWidget(right_box)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 900])
        QVBoxLayout(self).addWidget(splitter)

        self.browse_mode.currentIndexChanged.connect(lambda _i: self._fill_browser())
        self.browser.currentItemChanged.connect(lambda *_a: self._apply())
        self.search.textChanged.connect(lambda _t: self._apply())
        self.sort.currentIndexChanged.connect(lambda _i: self._apply())
        self.table.doubleClicked.connect(lambda index: self.main.play_items(self.model.items, index.row()))
        self.table.customContextMenuRequested.connect(self._menu)
        play_all.clicked.connect(lambda: self._play_all(False))
        shuffle.clicked.connect(lambda: self._play_all(True))
        main.now_playing.current_changed.connect(lambda item: self.model.set_playing(item["id"] if item else None))

    def refresh(self) -> None:
        self.tracks = self.catalog.query_items(kind="audio")
        self.activity = self.catalog.activity()
        self._fill_browser()

    def _group_value(self, item: dict, key: str) -> str:
        if key == "year":
            return year(item) or "Sin año"
        return effective(item).get(key, "") or "Desconocido"

    def _fill_browser(self) -> None:
        key = self.BROWSE[self.browse_mode.currentIndex()][0]
        current = self.browser.currentItem().data(Qt.ItemDataRole.UserRole) if self.browser.currentItem() else None
        groups: dict[str, list[dict]] = defaultdict(list)
        for item in self.tracks:
            groups[self._group_value(item, key)].append(item)
        self.browser.blockSignals(True)
        self.icons.clear(self.browser)
        self.browser.clear()
        everything = QListWidgetItem(f"Todo ({len(self.tracks)})")
        everything.setData(Qt.ItemDataRole.UserRole, None)
        self.browser.addItem(everything)
        reverse = key == "year"
        for name in sorted(groups, key=str.casefold, reverse=reverse):
            members = groups[name]
            label = name
            if key == "album":
                artists = Counter(effective(i).get("artist", "") for i in members).most_common(1)
                label = f"{name}\n{artists[0][0]}" if artists and artists[0][0] else name
            entry = QListWidgetItem(f"{label}  ({len(members)})" if key != "album" else label)
            entry.setData(Qt.ItemDataRole.UserRole, name)
            if key == "album":
                self.icons.bind(self.browser, entry, members[0])
            self.browser.addItem(entry)
            if name == current:
                self.browser.setCurrentItem(entry)
        if self.browser.currentItem() is None:
            self.browser.setCurrentRow(0)
        self.browser.blockSignals(False)
        self._apply()

    def sort_key(self, mode: str):
        meta = lambda item: effective(item)
        track = lambda item: number(meta(item).get("track"))
        album = lambda item: meta(item).get("album", "").casefold()
        artist = lambda item: meta(item).get("artist", "").casefold()
        title = lambda item: display_title(item).casefold()
        return {
            "Álbum": lambda i: (album(i), track(i), title(i)),
            "Artista": lambda i: (artist(i), album(i), track(i)),
            "Título": title,
            "Año": lambda i: (number(year(i)), album(i), track(i)),
            "Género": lambda i: (meta(i).get("genre", "").casefold(), artist(i), album(i), track(i)),
            "Añadidas recientemente": lambda i: -i["added_at"],
            "Más escuchadas": lambda i: (-self.activity.get(i["id"], {}).get("play_count", 0), title(i)),
        }[mode]

    def _apply(self) -> None:
        key = self.BROWSE[self.browse_mode.currentIndex()][0]
        selected = self.browser.currentItem().data(Qt.ItemDataRole.UserRole) if self.browser.currentItem() else None
        text = self.search.text()
        items = [i for i in self.tracks
                 if (selected is None or self._group_value(i, key) == selected) and matches(i, text)]
        items.sort(key=self.sort_key(self.sort.currentText()))
        self.model.set_items(items, self.activity)
        playing = self.main.now_playing.current
        self.model.playing_id = playing["id"] if playing else None
        total = sum(seconds(i) for i in items)
        self.count.setText(f"{len(items)} canciones · {clock(total) or '0:00'}")

    def selected(self) -> list[dict]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self.model.items[row] for row in rows]

    def _play_all(self, shuffle: bool) -> None:
        if self.model.items:
            self.main.now_playing.set_shuffle(shuffle)
            self.main.play_items(self.model.items, 0)

    def _menu(self, position) -> None:
        items = self.selected()
        if not items:
            return
        bar = self.main.now_playing
        menu = QMenu(self)
        first = self.model.items.index(items[0])
        menu.addAction("Reproducir", lambda: self.main.play_items(self.model.items, first))
        menu.addAction("Reproducir a continuación", lambda: bar.play_next(items))
        menu.addAction("Añadir a la cola", lambda: bar.enqueue(items))
        menu.addSeparator()
        if len(items) == 1:
            menu.addAction("Editar…", lambda: self.edit(items[0]))
        more = self.main._item_menu([i["id"] for i in items])
        more.setTitle("Más opciones")
        menu.addMenu(more)
        menu.exec(self.table.viewport().mapToGlobal(position))


# Libros

class BookModel(QAbstractTableModel):
    COLUMNS = ["Título", "Autor", "Serie", "Año", "Editorial", "Género", "Valoración", "Formato", "Tamaño"]

    def __init__(self, thumbs, parent=None):
        super().__init__(parent)
        self.thumbs, self.items = thumbs, []
        thumbs.updated.connect(self._thumb_updated)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            meta = effective(item)
            return [display_title(item), meta.get("author", ""), meta.get("saga", ""), year(item),
                    meta.get("publisher", ""), meta.get("genre", ""), stars(item["rating"]) if item["rating"] else "",
                    Path(item["rel_path"]).suffix.lstrip(".").upper(), human_size(item["size"])][index.column()]
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            return QIcon(self.thumbs.get(item))
        return None

    def set_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def _thumb_updated(self, item_id: int) -> None:
        for row, item in enumerate(self.items):
            if item["id"] == item_id:
                self.dataChanged.emit(self.index(row, 0), self.index(row, 0))


class BooksSection(Section):
    title = "Libros"
    CATEGORIES = [("author", "Autores"), ("saga", "Series"), ("genre", "Géneros"), ("publisher", "Editoriales"),
                  ("tag", "Etiquetas"), ("rating", "Valoración")]

    def __init__(self, main, parent=None):
        super().__init__(main, parent)
        self.books: list[dict] = []
        self.current: dict | None = None

        self.browser = QTreeWidget(headerHidden=True)
        self.search = QLineEdit(placeholderText="Buscar título, autor, serie…", clearButtonEnabled=True)
        self.sort = QComboBox()
        self.sort.addItems(["Título", "Autor", "Serie", "Año", "Añadidos recientemente", "Valoración"])
        self.model = BookModel(self.thumbs, self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setIconSize(QSize(22, 30))
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 280)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.count = muted()

        center = QVBoxLayout()
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(QLabel("Ordenar por"))
        top.addWidget(self.sort)
        center.addLayout(top)
        center.addWidget(self.table, 1)
        center.addWidget(self.count)
        center_box = QWidget()
        center_box.setLayout(center)

        self.cover = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.cover.setFixedSize(200, 300)
        self.book_title = QLabel(wordWrap=True)
        self.book_title.setProperty("title", True)
        self.book_author = QLabel(wordWrap=True)
        self.book_meta = muted()
        self.description = QTextBrowser()
        self.description.setOpenExternalLinks(False)
        self.read_button = QPushButton("Leer")
        self.read_button.setProperty("primary", True)
        self.edit_button = QPushButton("Editar metadatos…")
        self.folder_button = QPushButton("Ver en carpeta")
        details = QVBoxLayout()
        details.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignHCenter)
        details.addWidget(self.book_title)
        details.addWidget(self.book_author)
        details.addWidget(self.book_meta)
        buttons = QHBoxLayout()
        for button in (self.read_button, self.edit_button, self.folder_button):
            buttons.addWidget(button)
        details.addLayout(buttons)
        details.addWidget(self.description, 1)
        details_box = QWidget()
        details_box.setLayout(details)
        details_box.setMinimumWidth(300)

        splitter = QSplitter()
        splitter.addWidget(self.browser)
        splitter.addWidget(center_box)
        splitter.addWidget(details_box)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([200, 700, 320])
        QVBoxLayout(self).addWidget(splitter)

        self.browser.itemSelectionChanged.connect(self._apply)
        self.search.textChanged.connect(lambda _t: self._apply())
        self.sort.currentIndexChanged.connect(lambda _i: self._apply())
        self.table.selectionModel().selectionChanged.connect(lambda *_a: self._show_current())
        self.table.doubleClicked.connect(lambda _i: self._read())
        self.read_button.clicked.connect(self._read)
        self.edit_button.clicked.connect(lambda: self.current and self.edit(self.current))
        self.folder_button.clicked.connect(lambda: self.current and reveal_path(full_path(self.current)))
        self.thumbs.updated.connect(lambda item_id: self.current and self.current["id"] == item_id
                                    and self._show_current())
        self._show_book(None)

    def _value(self, item: dict, key: str) -> list[str]:
        if key == "tag":
            return [t.strip() for t in (item["tags"] or "").split(",") if t.strip()]
        if key == "rating":
            return [stars(item["rating"]) if item["rating"] else "Sin valorar"]
        if key in ("author", "genre"):
            return [v.strip() for v in effective(item).get(key, "").split(",") if v.strip()]
        value = effective(item).get(key, "")
        return [value] if value else []

    def refresh(self) -> None:
        self.books = self.catalog.query_items(kind="document")
        current = self.browser.selectedItems()[0].data(0, Qt.ItemDataRole.UserRole) if self.browser.selectedItems() else None
        self.browser.blockSignals(True)
        self.browser.clear()
        everything = QTreeWidgetItem(self.browser, [f"Todos los libros ({len(self.books)})"])
        everything.setData(0, Qt.ItemDataRole.UserRole, None)
        for key, label in self.CATEGORIES:
            counts = Counter(v for item in self.books for v in self._value(item, key))
            if not counts:
                continue
            node = QTreeWidgetItem(self.browser, [label])
            node.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for value in sorted(counts, key=str.casefold):
                child = QTreeWidgetItem(node, [f"{value} ({counts[value]})"])
                child.setData(0, Qt.ItemDataRole.UserRole, (key, value))
                if (key, value) == current:
                    child.setSelected(True)
                    node.setExpanded(True)
        if not self.browser.selectedItems():
            everything.setSelected(True)
        self.browser.blockSignals(False)
        self._apply()

    def _apply(self) -> None:
        selected = self.browser.selectedItems()
        group = selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else None
        if isinstance(group, list):
            group = tuple(group)
        items = [i for i in self.books
                 if (group is None or group[1] in self._value(i, group[0])) and matches(i, self.search.text())]
        meta = lambda i, k: effective(i).get(k, "").casefold()
        keys = {
            "Título": lambda i: display_title(i).casefold(),
            "Autor": lambda i: (meta(i, "author"), meta(i, "saga"), display_title(i).casefold()),
            "Serie": lambda i: (meta(i, "saga") or "￿", display_title(i).casefold()),
            "Año": lambda i: (number(year(i)), display_title(i).casefold()),
            "Añadidos recientemente": lambda i: -i["added_at"],
            "Valoración": lambda i: (-i["rating"], display_title(i).casefold()),
        }
        items.sort(key=keys[self.sort.currentText()])
        self.model.set_items(items)
        self.count.setText(f"{len(items)} libros")
        self._show_book(None)

    def _show_current(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        self._show_book(self.model.items[rows[0].row()] if rows else None)

    def _show_book(self, item: dict | None) -> None:
        self.current = item
        for button in (self.read_button, self.edit_button, self.folder_button):
            button.setEnabled(item is not None)
        if not item:
            self.cover.clear()
            self.book_title.setText("Selecciona un libro")
            self.book_author.clear()
            self.book_meta.clear()
            self.description.clear()
            return
        meta = effective(item)
        self.cover.setPixmap(cover_pixmap(self.thumbs.get(item), 200, 300))
        self.book_title.setText(display_title(item))
        self.book_author.setText(meta.get("author", "Autor desconocido"))
        lines = []
        if meta.get("saga"):
            lines.append(f"Serie: {meta['saga']}")
        lines.append(" · ".join(p for p in (year(item), meta.get("publisher", ""), meta.get("genre", "")) if p))
        if item["rating"]:
            lines.append(stars(item["rating"]))
        if item["tags"]:
            lines.append(f"Etiquetas: {item['tags']}")
        lines.append(f"{Path(item['rel_path']).suffix.lstrip('.').upper()} · {human_size(item['size'])}")
        self.book_meta.setText("\n".join(line for line in lines if line))
        description = meta.get("description", "") or "Sin descripción."
        if re.search(r"<[a-zA-Z/][^>]*>", description):
            # Los EPUB guardan la sinopsis en HTML: se muestra con formato, sin enlaces ni imágenes externas.
            self.description.setHtml(description)
        else:
            self.description.setPlainText(description)

    def _read(self) -> None:
        if not self.current:
            return
        path = full_path(self.current)
        if not os.path.exists(path):
            QMessageBox.warning(self, "No disponible", f"No se encuentra:\n{path}")
            return
        self.catalog.record_play(self.current["id"])
        open_path(path)


# Juegos

class GamesSection(Section):
    title = "Juegos"

    def __init__(self, main, parent=None):
        super().__init__(main, parent)
        self.games: list[dict] = []
        self.activity: dict = {}
        self.current: dict | None = None

        self.search = QLineEdit(placeholderText="Buscar juego…", clearButtonEnabled=True)
        self.listing = QListWidget()
        self.listing.setIconSize(QSize(28, 28))
        library_button = QPushButton("Biblioteca")
        left = QVBoxLayout()
        left.addWidget(library_button)
        left.addWidget(self.search)
        left.addWidget(self.listing, 1)
        left_box = QWidget()
        left_box.setLayout(left)

        self.sort = QComboBox()
        self.sort.addItems(["Nombre", "Jugados recientemente", "Añadidos recientemente"])
        self.steam_all = QPushButton("Descargar portadas de Steam")
        self.grid_model = CardModel(self.thumbs, subtitle=self._grid_subtitle)
        self.grid = card_view(self.grid_model, 150, POSTER)
        library_top = QHBoxLayout()
        library_top.addWidget(heading("Biblioteca"))
        library_top.addStretch()
        library_top.addWidget(QLabel("Ordenar por"))
        library_top.addWidget(self.sort)
        library_top.addWidget(self.steam_all)
        library = QVBoxLayout()
        library.addLayout(library_top)
        library.addWidget(self.grid, 1)
        self.empty = muted("Añade una carpeta de juegos con tus accesos directos (.lnk o .url de Steam): "
                           "menú Archivo › Añadir carpeta de juegos.")
        library.addWidget(self.empty)
        library_page = QWidget()
        library_page.setLayout(library)

        self.cover = QLabel()
        self.cover.setFixedSize(240, 360)
        self.game_title = heading("")
        self.game_meta = muted()
        self.stats = muted()
        self.play_button = QPushButton("JUGAR")
        self.play_button.setProperty("primary", True)
        self.play_button.setMinimumHeight(44)
        self.play_button.setMinimumWidth(180)
        self.edit_button = QPushButton("Editar…")
        self.steam_button = QPushButton("Portada de Steam")
        self.folder_button = QPushButton("Ver en carpeta")
        self.description = QTextBrowser()
        back = QPushButton("← Biblioteca")
        info = QVBoxLayout()
        info.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
        info.addWidget(self.game_title)
        info.addWidget(self.game_meta)
        actions = QHBoxLayout()
        actions.addWidget(self.play_button)
        actions.addWidget(self.stats, 1)
        info.addLayout(actions)
        secondary = QHBoxLayout()
        for button in (self.edit_button, self.steam_button, self.folder_button):
            secondary.addWidget(button)
        secondary.addStretch()
        info.addLayout(secondary)
        info.addWidget(self.description, 1)
        page = QHBoxLayout()
        page.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)
        page.addLayout(info, 1)
        game_page = QWidget()
        game_page.setLayout(page)

        self.pages = QStackedWidget()
        self.pages.addWidget(library_page)
        self.pages.addWidget(game_page)
        splitter = QSplitter()
        splitter.addWidget(left_box)
        splitter.addWidget(self.pages)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 900])
        QVBoxLayout(self).addWidget(splitter)

        library_button.clicked.connect(self._show_library)
        back.clicked.connect(self._show_library)
        self.search.textChanged.connect(lambda _t: self._apply())
        self.sort.currentIndexChanged.connect(lambda _i: self._apply())
        self.listing.itemClicked.connect(lambda entry: self.show_game(entry.data(Qt.ItemDataRole.UserRole)))
        self.grid.clicked.connect(lambda index: self.show_game(self.grid_model.items[index.row()]))
        self.play_button.clicked.connect(lambda: self.current and self.launch(self.current))
        self.edit_button.clicked.connect(self._edit_current)
        self.folder_button.clicked.connect(lambda: self.current and reveal_path(full_path(self.current)))
        self.steam_button.clicked.connect(lambda: self.current and self.fetch_steam([self.current]))
        self.steam_all.clicked.connect(self._fetch_all_steam)
        self.thumbs.updated.connect(self._thumb_updated)

    def _grid_subtitle(self, item: dict) -> str:
        last = self.activity.get(item["id"], {}).get("last_played")
        return f"Jugado {ago(last)}" if last else effective(item).get("platform", "")

    def refresh(self) -> None:
        self.games = self.catalog.query_items(kind="game")
        self.activity = self.catalog.activity()
        self._apply()
        if self.current:
            fresh = next((g for g in self.games if g["id"] == self.current["id"]), None)
            if fresh:
                self.show_game(fresh)
            else:
                self._show_library()

    def _apply(self) -> None:
        text = self.search.text()
        games = [g for g in self.games if matches(g, text)]
        mode = self.sort.currentText()
        if mode == "Jugados recientemente":
            games.sort(key=lambda g: (-self.activity.get(g["id"], {}).get("last_played", 0), display_title(g).casefold()))
        elif mode == "Añadidos recientemente":
            games.sort(key=lambda g: -g["added_at"])
        else:
            games.sort(key=lambda g: display_title(g).casefold())
        self.grid_model.set_items(games)
        self.icons.clear(self.listing)
        self.listing.clear()
        for game in sorted(games, key=lambda g: display_title(g).casefold()):
            entry = QListWidgetItem(display_title(game))
            self.icons.bind(self.listing, entry, game)
            entry.setData(Qt.ItemDataRole.UserRole, game)
            self.listing.addItem(entry)
        self.empty.setVisible(not self.games)
        self.steam_all.setVisible(any(online.steam_app_id(full_path(g)) for g in self.games))

    def _show_library(self) -> None:
        self.current = None
        self.pages.setCurrentIndex(0)

    def show_game(self, game: dict) -> None:
        self.current = game
        meta = effective(game)
        path = full_path(game)
        self.cover.setPixmap(cover_pixmap(self.thumbs.get(game), 240, 360))
        self.game_title.setText(display_title(game))
        self.game_meta.setText(" · ".join(p for p in (meta.get("platform"), year(game), meta.get("developer"),
                                                       meta.get("genre")) if p) or Path(game["rel_path"]).name)
        stats = self.activity.get(game["id"], {})
        if stats.get("play_count"):
            self.stats.setText(f"Última vez: {ago(stats['last_played'])} · {stats['play_count']} veces")
        else:
            self.stats.setText("Aún no lo has abierto desde Mikoshi")
        self.play_button.setText("ABRIR CARPETA" if os.path.isdir(path) else "JUGAR")
        self.steam_button.setVisible(online.steam_app_id(path) is not None)
        self.description.setPlainText(meta.get("description", "") or "Sin descripción.")
        self.pages.setCurrentIndex(1)

    def launch(self, game: dict) -> None:
        path = full_path(game)
        if not os.path.exists(path):
            QMessageBox.warning(self, "No disponible", f"No se encuentra el acceso directo:\n{path}")
            return
        try:
            open_path(path)
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo abrir", str(exc))
            return
        self.catalog.record_play(game["id"])
        self.activity = self.catalog.activity()
        if self.current and self.current["id"] == game["id"]:
            self.show_game(game)

    def _edit_current(self) -> None:
        if self.current:
            self.edit(self.current)

    def fetch_steam(self, games: list[dict]) -> None:
        targets = [(g, online.steam_app_id(full_path(g))) for g in games]
        targets = [(g, app_id) for g, app_id in targets if app_id]
        if not targets:
            return
        self.main.status(f"Descargando {len(targets)} portada(s) de Steam…")

        def work():
            results = []
            for game, app_id in targets:
                try:
                    results.append((game, online.steam_cover(app_id), None))
                except online.OnlineError as exc:
                    results.append((game, None, str(exc)))
            return results

        task = dialogs.Task(work)
        task.succeeded.connect(self._steam_done)
        task.start()

    def _steam_done(self, results) -> None:
        saved = 0
        for game, data, _error in results:
            if data:
                thumbnails.save_cover(self.catalog, self.catalog.get_item(game["id"]), data)
                saved += 1
        failed = len(results) - saved
        self.main.status(f"Portadas de Steam: {saved} guardadas" + (f", {failed} no encontradas" if failed else ""))
        self.refresh()

    def _fetch_all_steam(self) -> None:
        self.fetch_steam([g for g in self.games if not g["cover"]])

    def _thumb_updated(self, item_id: int) -> None:
        if self.current and self.current["id"] == item_id:
            self.cover.setPixmap(cover_pixmap(self.thumbs.get(self.current), 240, 360))


# Películas y series

class CinemaSection(Section):
    title = "Películas y series"

    def __init__(self, main, parent=None):
        super().__init__(main, parent)
        self.movies: list[dict] = []
        self.series: dict[str, list[dict]] = {}
        self.activity: dict = {}
        self.current_movie: dict | None = None
        self.current_series: str | None = None

        self.nav = {name: chip(name) for name in ("Inicio", "Películas", "Series")}
        self.search = QLineEdit(placeholderText="Buscar…", clearButtonEnabled=True)
        self.search.setMaximumWidth(320)
        nav = QHBoxLayout()
        for button in self.nav.values():
            nav.addWidget(button)
        nav.addStretch()
        nav.addWidget(self.search)

        self.continue_model = CardModel(self.thumbs, subtitle=self._episode_or_year, progress=self._progress)
        self.recent_model = CardModel(self.thumbs, subtitle=self._episode_or_year, badge=self._watched_badge)
        self.continue_view = card_view(self.continue_model, 240, WIDE, row=True)
        self.recent_view = card_view(self.recent_model, 130, POSTER, row=True)
        self.continue_title = heading("Continuar viendo")
        home = QVBoxLayout()
        home.addWidget(self.continue_title)
        home.addWidget(self.continue_view)
        home.addWidget(heading("Añadidos recientemente"))
        home.addWidget(self.recent_view)
        self.home_empty = muted("Aún no hay películas ni series. Añade la carpeta donde las guardas desde "
                                "Archivo › Añadir carpeta.")
        home.addWidget(self.home_empty)
        home.addStretch()
        home_page = QWidget()
        home_page.setLayout(home)

        self.movie_sort = QComboBox()
        self.movie_sort.addItems(["Título", "Año", "Añadidas recientemente", "Valoración"])
        self.movie_model = CardModel(self.thumbs, subtitle=lambda i: year(i), badge=self._watched_badge,
                                     progress=self._progress)
        self.movie_grid = card_view(self.movie_model, 150, POSTER)
        movies = QVBoxLayout()
        movies_top = QHBoxLayout()
        movies_top.addWidget(heading("Películas"))
        movies_top.addStretch()
        movies_top.addWidget(QLabel("Ordenar por"))
        movies_top.addWidget(self.movie_sort)
        movies.addLayout(movies_top)
        movies.addWidget(self.movie_grid, 1)
        movies_page = QWidget()
        movies_page.setLayout(movies)

        self.series_model = CardModel(self.thumbs, title=lambda s: s["name"], subtitle=self._series_subtitle)
        self.series_model.cover_of = lambda s: s["cover"]
        self.series_grid = card_view(self.series_model, 150, POSTER)
        series = QVBoxLayout()
        series.addWidget(heading("Series"))
        series.addWidget(self.series_grid, 1)
        series_page = QWidget()
        series_page.setLayout(series)

        self.movie_page = self._build_movie_page()
        self.series_page = self._build_series_page()

        self.pages = QStackedWidget()
        for page in (home_page, movies_page, series_page, self.movie_page, self.series_page):
            self.pages.addWidget(page)
        layout = QVBoxLayout(self)
        layout.addLayout(nav)
        layout.addWidget(self.pages, 1)

        for index, button in enumerate(self.nav.values()):
            button.clicked.connect(lambda _c=False, i=index: self.show_page(i))
        self.search.textChanged.connect(lambda _t: self._apply())
        self.movie_sort.currentIndexChanged.connect(lambda _i: self._apply())
        self.movie_grid.clicked.connect(lambda index: self.show_movie(self.movie_model.items[index.row()]))
        self.series_grid.clicked.connect(lambda index: self.show_series(self.series_model.items[index.row()]["name"]))
        self.continue_view.clicked.connect(lambda index: self._open_card(self.continue_model.items[index.row()]))
        self.recent_view.clicked.connect(lambda index: self._open_card(self.recent_model.items[index.row()]))
        self.thumbs.updated.connect(self._thumb_updated)
        self.show_page(0)

    # Páginas de detalle

    def _build_movie_page(self) -> QWidget:
        self.movie_cover = QLabel()
        self.movie_cover.setFixedSize(240, 360)
        self.movie_title = heading("")
        self.movie_meta = muted()
        self.movie_rating = QLabel()
        self.movie_play = QPushButton("Reproducir")
        self.movie_play.setProperty("primary", True)
        self.movie_play.setMinimumHeight(40)
        self.movie_restart = QPushButton("Desde el principio")
        self.movie_watched = QPushButton()
        movie_edit = QPushButton("Editar…")
        movie_folder = QPushButton("Ver en carpeta")
        self.movie_description = QTextBrowser()
        back = QPushButton("← Volver")
        info = QVBoxLayout()
        info.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
        info.addWidget(self.movie_title)
        info.addWidget(self.movie_meta)
        info.addWidget(self.movie_rating)
        buttons = QHBoxLayout()
        for button in (self.movie_play, self.movie_restart, self.movie_watched, movie_edit, movie_folder):
            buttons.addWidget(button)
        buttons.addStretch()
        info.addLayout(buttons)
        info.addWidget(self.movie_description, 1)
        row = QHBoxLayout()
        row.addWidget(self.movie_cover, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(info, 1)
        page = QWidget()
        page.setLayout(row)
        back.clicked.connect(lambda: self.show_page(1))
        self.movie_play.clicked.connect(lambda: self.play([self.current_movie], resume=True))
        self.movie_restart.clicked.connect(lambda: self.play([self.current_movie], resume=False))
        self.movie_watched.clicked.connect(self._toggle_movie_watched)
        movie_edit.clicked.connect(lambda: self.current_movie and self.edit(self.current_movie))
        movie_folder.clicked.connect(lambda: self.current_movie and reveal_path(full_path(self.current_movie)))
        return page

    def _build_series_page(self) -> QWidget:
        self.series_cover = QLabel()
        self.series_cover.setFixedSize(200, 300)
        self.series_title = heading("")
        self.series_meta = muted()
        self.season = QComboBox()
        self.next_up = QPushButton()
        self.next_up.setProperty("primary", True)
        self.next_up.setMinimumHeight(40)
        self.season_watched = QPushButton("Marcar temporada como vista")
        self.series_cover_button = QPushButton("Portada de la serie…")
        self.episodes = QListWidget()
        self.episodes.setIconSize(QSize(176, 99))
        self.episodes.setSpacing(4)
        back = QPushButton("← Series")
        head = QVBoxLayout()
        head.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
        head.addWidget(self.series_title)
        head.addWidget(self.series_meta)
        actions = QHBoxLayout()
        actions.addWidget(self.next_up)
        actions.addWidget(QLabel("Temporada"))
        actions.addWidget(self.season)
        actions.addWidget(self.season_watched)
        actions.addWidget(self.series_cover_button)
        actions.addStretch()
        head.addLayout(actions)
        top = QHBoxLayout()
        top.addWidget(self.series_cover, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(head, 1)
        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.episodes, 1)
        page = QWidget()
        page.setLayout(layout)
        back.clicked.connect(lambda: self.show_page(2))
        self.season.currentIndexChanged.connect(lambda _i: self._fill_episodes())
        self.episodes.itemDoubleClicked.connect(self._play_episode)
        self.episodes.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.episodes.customContextMenuRequested.connect(self._episode_menu)
        self.next_up.clicked.connect(self._play_next_up)
        self.season_watched.clicked.connect(self._mark_season)
        self.series_cover_button.clicked.connect(self._choose_series_cover)
        return page

    # Datos

    def _progress(self, item: dict) -> float:
        stats = self.activity.get(item["id"], {})
        if stats.get("position_ms") and stats.get("duration_ms"):
            return stats["position_ms"] / stats["duration_ms"]
        return 0.0

    def _watched(self, item: dict) -> bool:
        return bool(self.activity.get(item["id"], {}).get("watched"))

    def _watched_badge(self, item: dict) -> str:
        return "✓ Visto" if self._watched(item) else ""

    def _episode_or_year(self, item: dict) -> str:
        meta = effective(item)
        if meta.get("series"):
            return f"{meta['series']} · T{number(meta.get('season'), 1)}E{number(meta.get('episode'), 0)}"
        return year(item)

    def _series_subtitle(self, series: dict) -> str:
        seen = sum(1 for e in series["episodes"] if self._watched(e))
        return f"{len(series['episodes'])} episodios · {seen} vistos"

    @staticmethod
    def episode_key(item: dict):
        meta = effective(item)
        return number(meta.get("season"), 1), number(meta.get("episode")), display_title(item).casefold()

    def refresh(self) -> None:
        favorites = self.catalog.favorite_root_ids()
        videos = self.catalog.query_items(kind="video")
        self.activity = self.catalog.activity()
        self.movies = [v for v in videos if video_category(v, favorites) == "movie"]
        self.series = defaultdict(list)
        for video in videos:
            if video_category(video, favorites) == "series":
                self.series[effective(video).get("series") or "Sin serie"].append(video)
        for episodes in self.series.values():
            episodes.sort(key=self.episode_key)
        self._apply()
        if self.pages.currentWidget() is self.movie_page and self.current_movie:
            fresh = next((m for m in videos if m["id"] == self.current_movie["id"]), None)
            self.show_movie(fresh) if fresh else self.show_page(1)
        elif self.pages.currentWidget() is self.series_page and self.current_series:
            self.show_series(self.current_series) if self.current_series in self.series else self.show_page(2)

    def _apply(self) -> None:
        text = self.search.text()
        movies = [m for m in self.movies if matches(m, text)]
        sorters = {
            "Título": lambda m: display_title(m).casefold(),
            "Año": lambda m: -number(year(m), 0),
            "Añadidas recientemente": lambda m: -m["added_at"],
            "Valoración": lambda m: (-m["rating"], display_title(m).casefold()),
        }
        movies.sort(key=sorters[self.movie_sort.currentText()])
        self.movie_model.set_items(movies)
        series = [{"name": name, "episodes": eps, "cover": next((e for e in eps if e["cover"]), eps[0])}
                  for name, eps in sorted(self.series.items(), key=lambda kv: kv[0].casefold())
                  if not text or text.casefold() in name.casefold() or any(matches(e, text) for e in eps)]
        self.series_model.set_items(series)
        everything = self.movies + [e for eps in self.series.values() for e in eps]
        in_progress = [v for v in everything if self._progress(v) > 0 and not self._watched(v)]
        in_progress.sort(key=lambda v: -self.activity[v["id"]]["last_played"])
        self.continue_model.set_items(in_progress[:20])
        self.continue_title.setVisible(bool(in_progress))
        self.continue_view.setVisible(bool(in_progress))
        recent = sorted(everything, key=lambda v: -v["added_at"])[:20]
        self.recent_model.set_items(recent)
        self.home_empty.setVisible(not everything)

    def show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for position, button in enumerate(self.nav.values()):
            button.setChecked(position == index)

    def _open_card(self, item: dict) -> None:
        meta = effective(item)
        favorites = self.catalog.favorite_root_ids()
        if video_category(item, favorites) == "series":
            self.show_series(meta.get("series") or "Sin serie")
        else:
            self.show_movie(item)

    def show_movie(self, movie: dict) -> None:
        self.current_movie = movie
        meta = effective(movie)
        self.movie_cover.setPixmap(cover_pixmap(self.thumbs.get(movie), 240, 360))
        self.movie_title.setText(display_title(movie))
        parts = [year(movie), meta.get("genre", ""), clock(seconds(movie))]
        if meta.get("director"):
            parts.append(f"Dirección: {meta['director']}")
        self.movie_meta.setText(" · ".join(p for p in parts if p))
        self.movie_rating.setText(stars(movie["rating"]) if movie["rating"] else "")
        position = self.activity.get(movie["id"], {}).get("position_ms", 0)
        resumable = position > 0 and not self._watched(movie)
        self.movie_play.setText(f"Continuar desde {clock(position // 1000)}" if resumable else "Reproducir")
        self.movie_restart.setVisible(resumable)
        self.movie_watched.setText("Marcar como no vista" if self._watched(movie) else "Marcar como vista")
        self.movie_description.setPlainText(meta.get("description", "") or "Sin descripción.")
        self.pages.setCurrentWidget(self.movie_page)
        for button in self.nav.values():
            button.setChecked(False)

    def show_series(self, name: str) -> None:
        self.current_series = name
        episodes = self.series[name]
        self.series_cover.setPixmap(cover_pixmap(self.thumbs.get(next((e for e in episodes if e["cover"]),
                                                                      episodes[0])), 200, 300))
        self.series_title.setText(name)
        seasons = sorted({self.episode_key(e)[0] for e in episodes})
        seen = sum(1 for e in episodes if self._watched(e))
        self.series_meta.setText(f"{len(seasons)} temporada(s) · {len(episodes)} episodios · {seen} vistos")
        current = self.season.currentData()
        self.season.blockSignals(True)
        self.season.clear()
        for season in seasons:
            self.season.addItem(f"Temporada {season}", season)
        index = self.season.findData(current)
        self.season.setCurrentIndex(index if index >= 0 else 0)
        self.season.blockSignals(False)
        self._fill_episodes()
        upcoming = self.next_episode()
        self.next_up.setVisible(upcoming is not None)
        if upcoming:
            season, episode, _title = self.episode_key(upcoming)
            verb = "Continuar" if self._progress(upcoming) else "Reproducir"
            self.next_up.setText(f"{verb} T{season}E{episode if episode < 10 ** 6 else '?'}")
        self.pages.setCurrentWidget(self.series_page)
        for button in self.nav.values():
            button.setChecked(False)

    def season_episodes(self) -> list[dict]:
        season = self.season.currentData()
        return [e for e in self.series.get(self.current_series, []) if self.episode_key(e)[0] == season]

    def next_episode(self) -> dict | None:
        """El primero sin ver, en orden de temporada y episodio."""
        return next((e for e in self.series.get(self.current_series, []) if not self._watched(e)), None)

    def _fill_episodes(self) -> None:
        self.icons.clear(self.episodes)
        self.episodes.clear()
        for episode in self.season_episodes():
            _season, number_, _title = self.episode_key(episode)
            stats = self.activity.get(episode["id"], {})
            status = "✓ Visto" if self._watched(episode) else (
                f"Visto hasta {clock(stats['position_ms'] // 1000)}" if stats.get("position_ms") else "")
            label = f"{number_ if number_ < 10 ** 6 else '·'}. {display_title(episode)}\n" \
                    f"{' · '.join(p for p in (clock(seconds(episode)), status) if p)}"
            entry = QListWidgetItem(label)
            self.icons.bind(self.episodes, entry, episode, lambda p: QIcon(cover_pixmap(p, 176, 99)))
            entry.setData(Qt.ItemDataRole.UserRole, episode)
            self.episodes.addItem(entry)

    def play(self, items: list[dict], resume: bool = True) -> None:
        if items and items[0]:
            self.main.play_items(items, 0, resume=resume)
            self.refresh()

    def _play_episode(self, entry: QListWidgetItem) -> None:
        episodes = self.season_episodes()
        index = self.episodes.row(entry)
        self.play(episodes[index:], resume=True)

    def _play_next_up(self) -> None:
        upcoming = self.next_episode()
        if upcoming:
            episodes = self.series[self.current_series]
            self.play(episodes[episodes.index(upcoming):], resume=True)

    def _toggle_movie_watched(self) -> None:
        if self.current_movie:
            self.catalog.set_watched(self.current_movie["id"], not self._watched(self.current_movie))
            self.refresh()

    def _mark_season(self) -> None:
        for episode in self.season_episodes():
            self.catalog.set_watched(episode["id"], True)
        self.refresh()

    def _episode_menu(self, position) -> None:
        entry = self.episodes.itemAt(position)
        if not entry:
            return
        episode = entry.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Reproducir desde aquí", lambda: self._play_episode(entry))
        watched = self._watched(episode)
        menu.addAction("Marcar como no visto" if watched else "Marcar como visto",
                       lambda: (self.catalog.set_watched(episode["id"], not watched), self.refresh()))
        menu.addAction("Editar…", lambda: self.edit(episode))
        menu.exec(self.episodes.viewport().mapToGlobal(position))

    def _choose_series_cover(self) -> None:
        if not self.current_series:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Portada de la serie", "", "Imágenes (*.jpg *.jpeg *.png *.webp)")
        if path:
            # La portada se guarda en el primer episodio: es el que representa a la serie.
            first = self.series[self.current_series][0]
            thumbnails.save_cover(self.catalog, first, Path(path).read_bytes(), Path(path).suffix.lower())
            self.refresh()

    def _thumb_updated(self, item_id: int) -> None:
        if self.pages.currentWidget() is self.movie_page and self.current_movie and self.current_movie["id"] == item_id:
            self.movie_cover.setPixmap(cover_pixmap(self.thumbs.get(self.current_movie), 240, 360))


# Videos favoritos

class FavoritesSection(Section):
    title = "Videos favoritos"
    SORTS = ["Recientes", "Más vistos", "Título", "Duración"]

    def __init__(self, main, parent=None):
        super().__init__(main, parent)
        self.videos: list[dict] = []
        self.activity: dict = {}

        self.current_channel: str | None = None
        self.return_page: QWidget | None = None

        self.chips = {name: chip(name) for name in self.SORTS}
        self.chips["Recientes"].setChecked(True)
        self.channels_button = QPushButton("Canales")
        self.search = QLineEdit(placeholderText="Buscar en tus videos…", clearButtonEnabled=True)
        self.search.setMaximumWidth(380)
        top = QHBoxLayout()
        for button in self.chips.values():
            top.addWidget(button)
        top.addSpacing(12)
        top.addWidget(self.channels_button)
        top.addStretch()
        top.addWidget(self.search)
        self.model = self._video_model()
        self.grid = card_view(self.model, 280, WIDE)
        grid = QVBoxLayout()
        grid.addLayout(top)
        grid.addWidget(self.grid, 1)
        self.grid_page = QWidget()
        self.grid_page.setLayout(grid)

        self.setup_page = self._build_setup_page()
        self.watch_page = self._build_watch_page()
        self.channels_page = self._build_channels_page()
        self.channel_page = self._build_channel_page()
        self.pages = QStackedWidget()
        for page in (self.grid_page, self.watch_page, self.setup_page, self.channels_page, self.channel_page):
            self.pages.addWidget(page)
        QVBoxLayout(self).addWidget(self.pages)

        for name, button in self.chips.items():
            button.clicked.connect(lambda _c=False, n=name: self._set_sort(n))
        self.channels_button.clicked.connect(self.show_channels)
        self.search.textChanged.connect(lambda _t: self._apply())
        self._connect_video_grid(self.grid, self.model)

    def _video_model(self) -> CardModel:
        return CardModel(self.thumbs, subtitle=self._subtitle, badge=lambda i: clock(seconds(i)),
                         progress=self._progress)

    def _connect_video_grid(self, view, model) -> None:
        view.clicked.connect(lambda index: self.watch(model.items, index.row()))
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(
            lambda position: self._grid_menu(view, model, position))

    @staticmethod
    def channel_of(item: dict) -> str:
        return effective(item).get("artist", "").strip() or "Sin autor"

    def channels(self) -> dict[str, list[dict]]:
        found: dict[str, list[dict]] = defaultdict(list)
        for video in self.sorted_videos():
            found[self.channel_of(video)].append(video)
        return found

    def _build_channels_page(self) -> QWidget:
        back = QPushButton("← Videos")
        back.clicked.connect(lambda: self.pages.setCurrentWidget(self.grid_page))
        top = QHBoxLayout()
        top.addWidget(back)
        top.addWidget(heading("Canales"))
        top.addStretch()
        self.channels_model = CardModel(self.thumbs, title=lambda c: c["name"],
                                        subtitle=lambda c: f"{len(c['videos'])} videos")
        self.channels_model.cover_of = self._channel_cover
        self.channels_grid = card_view(self.channels_model, 180, SQUARE)
        self.channels_grid.clicked.connect(
            lambda index: self.show_channel(self.channels_model.items[index.row()]["name"]))
        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.channels_grid, 1)
        page = QWidget()
        page.setLayout(layout)
        return page

    def _build_channel_page(self) -> QWidget:
        back = QPushButton("← Canales")
        back.clicked.connect(self.show_channels)
        self.channel_avatar = QLabel()
        self.channel_avatar.setFixedSize(72, 72)
        self.channel_title = heading("")
        self.channel_meta = muted()
        self.channel_meta.setWordWrap(False)
        self.channel_details = muted()
        edit = QPushButton("Editar canal…")
        edit.clicked.connect(self._edit_channel)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(self.channel_title)
        texts.addWidget(self.channel_meta)
        texts.addWidget(self.channel_details)
        top = QHBoxLayout()
        top.addWidget(back, 0, Qt.AlignmentFlag.AlignTop)
        top.addSpacing(8)
        top.addWidget(self.channel_avatar)
        top.addLayout(texts)
        top.addStretch()
        top.addWidget(edit, 0, Qt.AlignmentFlag.AlignTop)
        self.channel_model = self._video_model()
        self.channel_grid = card_view(self.channel_model, 280, WIDE)
        self._connect_video_grid(self.channel_grid, self.channel_model)
        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.channel_grid, 1)
        page = QWidget()
        page.setLayout(layout)
        return page

    def show_channels(self) -> None:
        self.current_channel = None
        found = self.channels()
        self.channels_model.set_items([{"name": name, "videos": videos}
                                       for name, videos in sorted(found.items(), key=lambda kv: kv[0].casefold())])
        self.pages.setCurrentWidget(self.channels_page)

    def show_channel(self, name: str) -> None:
        if self.pages.currentWidget() is self.watch_page:
            self.player.stop()
        self.current_channel = name
        videos = self.channels().get(name, [])
        info = self.catalog.channel(name)
        self.channel_title.setText(name)
        total = sum(seconds(v) for v in videos)
        counts = f"{len(videos)} videos en tu colección"
        if info["total_videos"]:
            counts += f" · {info['total_videos']} en total"
        self.channel_meta.setText(f"{counts} · {clock(total) or '0:00'}")
        details = []
        if info["country"]:
            details.append(info["country"])
        created = QDate.fromString(info["created"], "yyyy-MM-dd")
        if created.isValid():
            details.append(f"Creado el {created.toString('dd/MM/yyyy')}")
        self.channel_details.setText(" · ".join(details))
        self.channel_details.setVisible(bool(details))
        avatar = QPixmap(info["avatar"]) if info["avatar"] else QPixmap()
        if avatar.isNull() and videos:
            avatar = self.thumbs.get(videos[0])
        self.channel_avatar.setPixmap(dialogs.round_avatar(avatar, 72))
        self.channel_model.set_items(videos)
        self.pages.setCurrentWidget(self.channel_page)

    def _edit_channel(self) -> None:
        if not self.current_channel:
            return
        dialog = dialogs.ChannelDialog(self.catalog, self.current_channel, len(self.channel_model.items), self)
        if dialog.exec() == dialogs.QDialog.DialogCode.Accepted:
            self.show_channel(self.current_channel)

    def _channel_cover(self, channel: dict) -> dict:
        """Lo que se pinta en la tarjeta del canal: su foto si la tiene; si no, su primer vídeo."""
        avatar = self.catalog.channel(channel["name"])["avatar"]
        if not avatar:
            return channel["videos"][0]
        # Un «elemento» de mentira con la foto como portada: así la caché de miniaturas la trata igual.
        # Número negativo que cabe en 32 bits: Qt pasa el id por una señal de enteros.
        return {"id": -1 - (zlib.crc32(avatar.encode()) & 0x7FFFFFFF), "mtime": 0, "cover": avatar, "kind": "video", "status": "ok",
                "rel_path": "", "root_path": ""}

    def ask_resume(self, item: dict, position_ms: int) -> bool:
        """Pregunta si se sigue donde se dejó. True: continuar; False: desde el principio."""
        box = QMessageBox(self)
        box.setWindowTitle("Continuar viendo")
        box.setText(f"Dejaste «{display_title(item)}» en {clock(position_ms // 1000)}.")
        resume = box.addButton(f"Continuar desde {clock(position_ms // 1000)}", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Desde el principio", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(resume)
        box.exec()
        return box.clickedButton() is resume

    def video_menu(self, items: list[dict], row: int) -> QMenu:
        item = items[row]
        stats = self.activity.get(item["id"], {})
        menu = QMenu(self)
        menu.addAction("Reproducir", lambda: self.watch(items, row))
        if stats.get("position_ms") and not stats.get("watched"):
            menu.addAction("Reproducir desde el principio", lambda: self.watch(items, row, resume=False))
        menu.addAction("Editar metadatos…", lambda: self.edit(item))
        channel = self.channel_of(item)
        menu.addAction(f"Ver canal «{channel}»", lambda: self.show_channel(channel))
        menu.addSeparator()
        watched = bool(stats.get("watched"))
        menu.addAction("Marcar como no visto" if watched else "Marcar como visto",
                       lambda: (self.catalog.set_watched(item["id"], not watched), self.refresh()))
        if stats.get("position_ms"):
            menu.addAction("Olvidar por dónde iba",
                           lambda: (self.catalog.set_watched(item["id"], False), self.refresh()))
        menu.addAction("Ver en carpeta", lambda: reveal_path(full_path(item)))
        more = self.main._item_menu([item["id"]])
        more.setTitle("Más opciones")
        menu.addMenu(more)
        return menu

    def _grid_menu(self, view, model, position) -> None:
        index = view.indexAt(position)
        if index.isValid():
            view.setCurrentIndex(index)
            self.video_menu(model.items, index.row()).exec(view.viewport().mapToGlobal(position))

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()
        layout.addWidget(heading("Tus videos favoritos"), 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(muted("Elige la carpeta donde guardas tus videos favoritos. Mikoshi solo la lee."),
                         0, Qt.AlignmentFlag.AlignHCenter)
        self.suggestions = QVBoxLayout()
        layout.addLayout(self.suggestions)
        choose = QPushButton("Elegir otra carpeta…")
        choose.clicked.connect(self._choose_folder)
        layout.addWidget(choose, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return page

    def _build_watch_page(self) -> QWidget:
        self.player = PlayerWidget([], 0, open_path, None)
        self.player.item_changed.connect(self._show_info)
        self.watch_title = heading("")
        self.watch_title.setWordWrap(True)
        self.watch_meta = muted()
        self.watch_description = muted()
        self.channel_button = QPushButton()
        self.channel_button.setProperty("chip", True)
        self.channel_button.setToolTip("Ver todos los videos de este autor")
        back = QPushButton("← Volver")
        edit = QPushButton("Editar metadatos…")
        folder = QPushButton("Ver en carpeta")
        top = QHBoxLayout()
        top.addWidget(back)
        top.addStretch()
        buttons = QHBoxLayout()
        buttons.addWidget(self.channel_button)
        buttons.addStretch()
        buttons.addWidget(edit)
        buttons.addWidget(folder)
        left = QVBoxLayout()
        left.addLayout(top)
        left.addWidget(self.player, 1)
        left.addWidget(self.watch_title)
        left.addLayout(buttons)
        left.addWidget(self.watch_meta)
        left.addWidget(self.watch_description)
        self.up_next = QListWidget()
        self.up_next.setIconSize(QSize(168, 94))
        self.up_next.setFixedWidth(400)
        self.up_next.setWordWrap(True)
        right = QVBoxLayout()
        right.addWidget(QLabel("A continuación"))
        right.addWidget(self.up_next, 1)
        row = QHBoxLayout()
        row.addLayout(left, 1)
        row.addLayout(right)
        page = QWidget()
        page.setLayout(row)
        back.clicked.connect(self._back)
        self.channel_button.clicked.connect(lambda: self.show_channel(self.channel_of(self.player.item)))
        edit.clicked.connect(self._edit_playing)
        folder.clicked.connect(lambda: self.player.items and reveal_path(full_path(self.player.item)))
        self.up_next.itemClicked.connect(lambda entry: self.player.go(entry.data(Qt.ItemDataRole.UserRole)))
        return page

    def _subtitle(self, item: dict) -> str:
        plays = self.activity.get(item["id"], {}).get("play_count", 0)
        views = "1 reproducción" if plays == 1 else f"{plays} reproducciones"
        return f"{views} · {ago(item['added_at'])}"

    def _progress(self, item: dict) -> float:
        stats = self.activity.get(item["id"], {})
        if stats.get("position_ms") and stats.get("duration_ms") and not stats.get("watched"):
            return stats["position_ms"] / stats["duration_ms"]
        return 0.0

    def refresh(self) -> None:
        roots = self.catalog.favorite_root_ids() or self._adopt_named_folder()
        self.activity = self.catalog.activity()
        self.videos = [v for v in self.catalog.query_items(kind="video") if v["root_id"] in roots]
        if not roots:
            self._fill_suggestions()
            self.pages.setCurrentWidget(self.setup_page)
            return
        if self.pages.currentWidget() is self.setup_page:
            self.pages.setCurrentWidget(self.grid_page)
        self._apply()
        if self.pages.currentWidget() is self.channel_page and self.current_channel:
            self.show_channel(self.current_channel)

    def _adopt_named_folder(self) -> set[int]:
        """Si una sola carpeta se llama como «Videos favoritos», se usa sin preguntar."""
        candidates = [r for r in self.catalog.roots()
                      if r["mode"] == "auto" and "favorit" in Path(r["path"]).name.casefold()]
        if len(candidates) != 1:
            return set()
        self.catalog.set_root_mode(candidates[0]["id"], "favorites")
        self.main.status(f"«{Path(candidates[0]['path']).name}» se usa como Videos favoritos.")
        return {candidates[0]["id"]}

    def _fill_suggestions(self) -> None:
        while self.suggestions.count():
            widget = self.suggestions.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        video_roots = {v["root_id"] for v in self.catalog.query_items(kind="video")}
        for root in self.catalog.roots():
            if root["id"] in video_roots and root["mode"] != "games":
                button = QPushButton(f"Usar «{Path(root['path']).name}»")
                button.setProperty("primary", True)
                button.setToolTip(root["path"])
                button.clicked.connect(lambda _c=False, r=root["id"]: self.use_root(r))
                self.suggestions.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)

    def use_root(self, root_id: int) -> None:
        self.catalog.set_root_mode(root_id, "favorites")
        self.main.data_changed()
        self.refresh()

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Carpeta de videos favoritos")
        if path:
            self.main.scan([self.catalog.add_root(path, "favorites")])

    def _set_sort(self, name: str) -> None:
        for key, button in self.chips.items():
            button.setChecked(key == name)
        self._apply()

    def sorted_videos(self) -> list[dict]:
        mode = next(name for name, button in self.chips.items() if button.isChecked())
        videos = [v for v in self.videos if matches(v, self.search.text())]
        keys = {
            "Recientes": lambda v: -v["added_at"],
            "Más vistos": lambda v: (-self.activity.get(v["id"], {}).get("play_count", 0), -v["added_at"]),
            "Título": lambda v: display_title(v).casefold(),
            "Duración": lambda v: -seconds(v),
        }
        return sorted(videos, key=keys[mode])

    def _apply(self) -> None:
        self.model.set_items(self.sorted_videos())

    def watch(self, items: list[dict], index: int, resume: bool = True) -> None:
        self.main.now_playing.pause()
        # Como en YouTube: una reproducción cuenta al ver el 95 % del vídeo, no al abrirlo.
        self.player.hooks = PlayerHooks(self.catalog, resume=resume, count_at=0.95, ask_resume=self.ask_resume)
        if self.pages.currentWidget() is not self.watch_page:
            self.return_page = self.pages.currentWidget()
        self.pages.setCurrentWidget(self.watch_page)
        self.player.set_queue(list(items), index)
        # Solo el vídeo elegido puede empezar desde el principio; los siguientes vuelven a preguntar.
        self.player.hooks.resume = True

    def _show_info(self, item: dict) -> None:
        self.watch_title.setText(display_title(item))
        meta = effective(item)
        plays = self.catalog.activity().get(item["id"], {}).get("play_count", 0)
        self.watch_meta.setText(" · ".join(p for p in (f"{plays} reproducciones", ago(item["added_at"]),
                                                       clock(seconds(item)), human_size(item["size"])) if p))
        self.watch_description.setText(meta.get("description", ""))
        self.channel_button.setText(self.channel_of(item))
        self.icons.clear(self.up_next)
        self.up_next.clear()
        for offset, video in enumerate(self.player.items[self.player.index + 1:self.player.index + 30], 1):
            entry = QListWidgetItem(f"{display_title(video)}\n{clock(seconds(video))}")
            self.icons.bind(self.up_next, entry, video, lambda p: QIcon(cover_pixmap(p, 168, 94)))
            entry.setData(Qt.ItemDataRole.UserRole, offset)
            self.up_next.addItem(entry)

    def _edit_playing(self) -> None:
        if self.player.items:
            self.player.player.pause()
            self.main.edit_item(self.player.item["id"])

    def _back(self) -> None:
        self.player.stop()
        self.refresh()
        if self.return_page is self.channel_page and self.current_channel:
            self.show_channel(self.current_channel)
        else:
            self.pages.setCurrentWidget(self.grid_page)

    def release(self, paths: set[str]) -> bool:
        return self.player.release(paths)

    def leave(self) -> None:
        if self.pages.currentWidget() is self.watch_page:
            self.player.player.pause()
            self.player.save_progress()

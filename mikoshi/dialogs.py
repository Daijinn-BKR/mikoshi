"""Ventanas auxiliares: ajustes, edición masiva, búsqueda en internet e informes."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from . import metadata, online
from .db import Catalog

# Hilos cortos en marcha. Se guardan aquí porque pueden terminar después de cerrar su ventana.
_running: set[QThread] = set()


class Task(QThread):
    """Ejecuta una función fuera de la interfaz."""
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, function):
        super().__init__()
        self.function = function
        _running.add(self)
        self.finished.connect(lambda: _running.discard(self))

    def run(self) -> None:
        try:
            self.succeeded.emit(self.function())
        except online.OnlineError as exc:
            self.failed.emit(str(exc))


def wait_for_tasks() -> None:
    for task in list(_running):
        task.wait(25000)


def service_keys(catalog: Catalog) -> dict:
    return {"tmdb_key": catalog.setting("tmdb_key"), "rawg_key": catalog.setting("rawg_key")}


class SettingsDialog(QDialog):
    def __init__(self, catalog: Catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle("Ajustes")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.tmdb = QLineEdit(catalog.setting("tmdb_key"), echoMode=QLineEdit.EchoMode.Password)
        self.rawg = QLineEdit(catalog.setting("rawg_key"), echoMode=QLineEdit.EchoMode.Password)
        self.backup = QLineEdit(catalog.setting("backup_folder"), placeholderText="Desactivada")
        browse = QPushButton("Elegir…")
        browse.clicked.connect(self._browse)
        backup_row = QHBoxLayout()
        backup_row.addWidget(self.backup, 1)
        backup_row.addWidget(browse)
        form.addRow("Clave de TMDB (películas y series)", self.tmdb)
        form.addRow("Clave de RAWG (juegos)", self.rawg)
        form.addRow("Copia automática en", backup_row)
        layout.addLayout(form)
        note = QLabel(
            'Las claves son gratuitas: <a href="https://www.themoviedb.org/settings/api">TMDB</a> y '
            '<a href="https://rawg.io/apidocs">RAWG</a>. Se guardan sin cifrar en el catálogo de este PC, y las '
            "copias del catálogo las incluyen.<br>La música y los libros no necesitan clave.<br><br>"
            "La copia automática se hace al cerrar Mikoshi, como máximo una vez al día.",
            wordWrap=True, openExternalLinks=True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta para la copia automática", self.backup.text())
        if folder:
            self.backup.setText(folder)

    def _save(self) -> None:
        self.catalog.set_setting("tmdb_key", self.tmdb.text().strip())
        self.catalog.set_setting("rawg_key", self.rawg.text().strip())
        self.catalog.set_setting("backup_folder", self.backup.text().strip())
        self.accept()


class BulkEditDialog(QDialog):
    """Solo cambia los campos marcados, y solo en el catálogo."""
    # Título, pista y episodio son propios de cada elemento: repetirlos sería un error.
    EXCLUDED = {"title", "track", "episode"}

    def __init__(self, items: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Editar {len(items)} elementos")
        self.setMinimumWidth(460)
        kinds = {item["kind"] for item in items}
        shared = [(k, label) for k, label in metadata.FIELDS[items[0]["kind"]]
                  if k not in self.EXCLUDED and all(k in dict(metadata.FIELDS[kind]) for kind in kinds)]
        layout = QVBoxLayout(self)
        hint = QLabel("Marca los campos que quieres cambiar. Un campo marcado y vacío se borra en todos.", wordWrap=True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.rows: dict[str, tuple[QCheckBox, QLineEdit]] = {}
        for key, label in shared:
            check, line = QCheckBox(label), QLineEdit()
            line.setEnabled(False)
            check.toggled.connect(line.setEnabled)
            self.rows[key] = (check, line)
            form.addRow(check, line)
        layout.addLayout(form)
        if not shared:
            layout.addWidget(QLabel("Estos elementos no comparten campos editables."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {k: line.text().strip() for k, (check, line) in self.rows.items() if check.isChecked()}


class OnlineDialog(QDialog):
    """Busca datos y deja elegir. Al aceptar expone `fields` y `cover`."""

    def __init__(self, kind: str, query: str, keys: dict, parent=None):
        super().__init__(parent)
        self.kind, self.keys = kind, keys
        self.fields: dict = {}
        self.cover: bytes | None = None
        source = online.SOURCES[kind][0]
        self.setWindowTitle(f"Buscar en internet · {source}")
        self.resize(820, 520)

        self.query = QLineEdit(query)
        self.search_button = QPushButton("Buscar")
        self.results = QListWidget()
        self.preview = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(240, 280)
        self.info = QLabel(wordWrap=True, alignment=Qt.AlignmentFlag.AlignTop)
        self.info.setFixedWidth(240)
        self.info.setProperty("muted", True)
        self.use_cover = QCheckBox("Usar esta portada")
        self.use_cover.setChecked(True)
        self.status = QLabel(f"Se enviará a {source} el texto de búsqueda.")
        self.status.setProperty("muted", True)

        top = QHBoxLayout()
        top.addWidget(self.query, 1)
        top.addWidget(self.search_button)
        side = QVBoxLayout()
        side.addWidget(self.preview)
        side.addWidget(self.use_cover)
        side.addWidget(self.info, 1)
        middle = QHBoxLayout()
        middle.addWidget(self.results, 1)
        middle.addLayout(side)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Usar estos datos")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(middle, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self.search_button.setDefault(True)
        self.search_button.clicked.connect(self.run_search)
        self.query.returnPressed.connect(self.run_search)
        self.results.currentItemChanged.connect(self._selected)
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        self.search_generation = 0
        self.cover_generation = 0
        if query:
            self.run_search()

    def _start(self, function, on_success) -> Task:
        task = Task(function)
        task.succeeded.connect(on_success)
        task.failed.connect(self._failed)
        task.start()
        return task

    def _failed(self, message: str) -> None:
        self.search_button.setEnabled(True)
        self.buttons.setEnabled(True)
        self.status.setText(message)

    def run_search(self) -> None:
        text = self.query.text()
        self.search_generation += 1
        generation = self.search_generation
        self.status.setText("Buscando…")
        self.search_button.setEnabled(False)
        self._start(lambda: (generation, online.search(self.kind, text, self.keys)), self._show_results)

    def _show_results(self, payload) -> None:
        generation, results = payload
        if generation != self.search_generation:
            return
        self.search_button.setEnabled(True)
        self.results.clear()
        for result in results:
            entry = QListWidgetItem(f"{result.title}\n{result.subtitle}")
            entry.setData(Qt.ItemDataRole.UserRole, result)
            self.results.addItem(entry)
        self.status.setText(f"{len(results)} resultados." if results else "Sin resultados. Prueba con otras palabras.")
        if results:
            self.results.setCurrentRow(0)

    def _selected(self, current, _previous) -> None:
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(current is not None)
        self.preview.clear()
        self.cover_data = None
        if current is None:
            self.info.clear()
            return
        result = current.data(Qt.ItemDataRole.UserRole)
        labels = dict(metadata.FIELDS[self.kind])
        self.info.setText("\n".join(f"{labels[k]}: {v[:160]}" for k, v in result.fields.items() if k in labels))
        self.use_cover.setEnabled(bool(result.cover_url))
        self.cover_generation += 1
        generation = self.cover_generation
        if result.cover_url:
            self.preview.setText("Cargando portada…")
            self._start(lambda: (generation, online.download_cover(result.cover_url)), self._show_cover)
        else:
            self.preview.setText("Sin portada")

    cover_data: bytes | None = None

    def _show_cover(self, payload) -> None:
        generation, data = payload
        if generation != self.cover_generation:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.cover_data = data
            self.preview.setPixmap(pixmap.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation))
        else:
            self.preview.setText("Portada no válida")

    def _apply(self) -> None:
        result = self.results.currentItem().data(Qt.ItemDataRole.UserRole)
        self.buttons.setEnabled(False)
        self.status.setText("Pidiendo los datos completos…")
        self._start(lambda: online.details(result, self.keys), self._finish)

    def _finish(self, fields: dict) -> None:
        allowed = dict(metadata.FIELDS[self.kind])
        self.fields = {k: v for k, v in fields.items() if k in allowed}
        self.cover = self.cover_data if self.use_cover.isChecked() and self.use_cover.isEnabled() else None
        self.accept()


class ReportDialog(QDialog):
    """Lista de archivos agrupados. Doble clic: mostrar el archivo en su carpeta."""

    def __init__(self, title: str, summary: str, groups: list[tuple[str, list[str]]], reveal, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(summary, wordWrap=True))
        self.tree = QTreeWidget(headerHidden=True)
        for heading, paths in groups:
            parent_node = QTreeWidgetItem(self.tree, [heading])
            parent_node.setExpanded(True)
            for path in paths:
                node = QTreeWidgetItem(parent_node, [path])
                node.setData(0, Qt.ItemDataRole.UserRole, path)
        self.tree.setVisible(bool(groups))
        self.tree.itemDoubleClicked.connect(
            lambda node, _column: node.data(0, Qt.ItemDataRole.UserRole) and reveal(node.data(0, Qt.ItemDataRole.UserRole)))
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        copy = QPushButton("Copiar lista")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(
            "\n".join(p for _heading, paths in groups for p in paths)))
        copy.setVisible(bool(groups))
        row.addWidget(copy)
        row.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


def confirm(parent, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text) == QMessageBox.StandardButton.Yes

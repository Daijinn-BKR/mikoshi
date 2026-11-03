"""Reproductor de vídeo, música y fotos: incrustable en una página o en su propia ventana."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from .db import display_title, full_path

PLAYABLE = {"video", "audio", "image"}


def format_time(ms: int) -> str:
    total = max(ms, 0) // 1000
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


class PlayerHooks:
    """Lo que el reproductor cuenta al catálogo. Sin catálogo, no hace nada."""

    def __init__(self, catalog=None, resume: bool = False):
        self.catalog, self.resume = catalog, resume

    def start_position(self, item: dict) -> int:
        if not (self.catalog and self.resume):
            return 0
        return self.catalog.activity().get(item["id"], {}).get("position_ms", 0)

    def played(self, item: dict) -> None:
        if self.catalog:
            self.catalog.record_play(item["id"])

    def progress(self, item: dict, position: int, duration: int) -> None:
        if self.catalog and item["kind"] == "video" and duration > 0:
            self.catalog.save_progress(item["id"], position, duration)


class PlayerWidget(QWidget):
    item_changed = Signal(dict)

    def __init__(self, items: list[dict], index: int, open_external, hooks: PlayerHooks | None = None, parent=None):
        super().__init__(parent)
        self.items, self.index, self.open_external = items, index, open_external
        self.hooks = hooks or PlayerHooks()
        self.seeking = False
        self.pending_seek = 0
        self.original: QPixmap | None = None
        self.loaded_item: dict | None = None

        self.video = QVideoWidget()
        self.picture = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumSize(1, 1)
        self.message = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self.message.setProperty("title", True)
        self.stage = QStackedWidget()
        for widget in (self.video, self.picture, self.message):
            self.stage.addWidget(widget)

        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)

        self.previous_button = QPushButton("◀◀")
        self.play_button = QPushButton("Pausa")
        self.next_button = QPushButton("▶▶")
        self.external_button = QPushButton("Abrir fuera…")
        self.position = QSlider(Qt.Orientation.Horizontal)
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setProperty("muted", True)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(100)
        self.audio.setVolume(0.8)

        controls = QHBoxLayout()
        for widget in (self.previous_button, self.play_button, self.next_button):
            controls.addWidget(widget)
        controls.addWidget(self.position, 1)
        controls.addWidget(self.time_label)
        controls.addWidget(QLabel("Vol."))
        controls.addWidget(self.volume)
        controls.addWidget(self.external_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stage, 1)
        layout.addLayout(controls)

        self.previous_button.clicked.connect(lambda: self.go(-1))
        self.next_button.clicked.connect(lambda: self.go(1))
        self.play_button.clicked.connect(self.toggle)
        self.external_button.clicked.connect(self._open_outside)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        self.position.sliderPressed.connect(lambda: setattr(self, "seeking", True))
        self.position.sliderReleased.connect(self._seek)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(lambda ms: self.position.setRange(0, ms))
        self.player.playbackStateChanged.connect(self._state_changed)
        self.player.errorOccurred.connect(self._error)
        self.player.mediaStatusChanged.connect(self._status_changed)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.toggle)
        if items:
            self.load()

    @property
    def item(self) -> dict:
        return self.items[self.index]

    def set_queue(self, items: list[dict], index: int) -> None:
        self.items, self.index = items, index
        self.load()

    def save_progress(self) -> None:
        if self.loaded_item and self.player.duration() > 0:
            self.hooks.progress(self.loaded_item, self.player.position(), self.player.duration())

    def load(self) -> None:
        self.save_progress()
        self.player.stop()
        self.player.setSource(QUrl())
        self.loaded_item = None
        path = full_path(self.item)
        self.previous_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index < len(self.items) - 1)
        kind = self.item["kind"]
        is_media = kind in ("video", "audio")
        for widget in (self.play_button, self.position, self.volume, self.time_label):
            widget.setVisible(is_media)
        self.position.setValue(0)
        if not os.path.exists(path):
            self.item_changed.emit(self.item)
            self._show_message(f"No se encuentra el archivo.\n¿Está encendido el servidor?\n\n{path}")
            return
        # Primero se cuenta la reproducción, para que la página muestre el número ya actualizado.
        self.hooks.played(self.item)
        self.item_changed.emit(self.item)
        if kind == "image":
            self._load_image(path)
        elif is_media:
            if kind == "video":
                self.stage.setCurrentWidget(self.video)
            else:
                self.original = None
                self._show_message(f"♪\n{display_title(self.item)}")
            self.loaded_item = self.item
            self.pending_seek = self.hooks.start_position(self.item)
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()

    def _load_image(self, path: str) -> None:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            self._show_message(f"Este formato de imagen no se puede mostrar aquí.\n\n{Path(path).name}")
            return
        self.original = QPixmap.fromImage(image)
        self.stage.setCurrentWidget(self.picture)
        self._fit_image()

    def _fit_image(self) -> None:
        if self.original:
            self.picture.setPixmap(self.original.scaled(
                self.picture.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_image()

    def _show_message(self, text: str) -> None:
        self.message.setText(text)
        self.stage.setCurrentWidget(self.message)

    def go(self, step: int) -> None:
        target = self.index + step
        if 0 <= target < len(self.items):
            self.index = target
            self.load()

    def toggle(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif self.items and self.item["kind"] != "image":
            if self.player.source().isEmpty():
                # Se soltó el archivo (por ejemplo, para escribir en él): se vuelve a abrir donde iba.
                self.load()
            else:
                self.player.play()

    def _seek(self) -> None:
        self.seeking = False
        self.player.setPosition(self.position.value())

    def _position_changed(self, ms: int) -> None:
        if not self.seeking:
            self.position.setValue(ms)
        self.time_label.setText(f"{format_time(ms)} / {format_time(self.player.duration())}")

    def _state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText("Pausa" if playing else "Reproducir")

    def _status_changed(self, status) -> None:
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and self.pending_seek:
            # Continúa donde se dejó.
            self.player.setPosition(self.pending_seek)
            self.pending_seek = 0
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.loaded_item:
                self.hooks.progress(self.loaded_item, self.player.duration(), self.player.duration())
                self.loaded_item = None
            if self.index < len(self.items) - 1:
                self.go(1)

    def _error(self, _error, text: str) -> None:
        self.loaded_item = None
        self._show_message(f"Este archivo no se puede reproducir aquí.\nUsa «Abrir fuera…».\n\n{text}")

    def _open_outside(self) -> None:
        path = full_path(self.item)
        self.player.pause()
        try:
            self.open_external(path)
        except OSError as exc:
            self._show_message(f"No se pudo abrir:\n{exc}")

    def release(self, paths: set[str]) -> bool:
        """Suelta el archivo si es uno de `paths`, para que se pueda escribir en él."""
        if not self.items or os.path.normcase(full_path(self.item)) not in paths:
            return False
        self.stop()
        self._show_message("Reproducción detenida para escribir los metadatos.\nPulsa «Reproducir» para seguir.")
        return True

    def stop(self) -> None:
        self.save_progress()
        self.loaded_item = None
        self.player.stop()
        self.player.setSource(QUrl())


class PlayerDialog(QDialog):
    def __init__(self, items: list[dict], index: int, open_external, parent=None, hooks: PlayerHooks | None = None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(960, 640)
        self.view = PlayerWidget(items, index, open_external, hooks, self)
        self.view.item_changed.connect(lambda item: self.setWindowTitle(f"{display_title(item)} · Mikoshi"))
        self.setWindowTitle(f"{display_title(self.view.item)} · Mikoshi")
        QVBoxLayout(self).addWidget(self.view)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, lambda: self.view.go(1))
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, lambda: self.view.go(-1))

    def __getattr__(self, name):
        # Acceso directo a las piezas del reproductor: dialog.player, dialog.go(), etc.
        return getattr(self.__dict__.get("view") or super().__getattribute__("view"), name)

    def done(self, result) -> None:
        self.view.stop()
        super().done(result)

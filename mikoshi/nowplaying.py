"""Barra de música fija, como en MusicBee: sigue sonando mientras navegas."""
from __future__ import annotations

import os
import random

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QSlider, QVBoxLayout, QWidget

from .db import display_title, effective, full_path
from .player import SeekSlider, format_time
from .widgets import draw_cover

REPEAT_MODES = [("off", "Repetir: no"), ("all", "Repetir: todo"), ("one", "Repetir: una")]


class NowPlayingBar(QWidget):
    current_changed = Signal(object)

    def __init__(self, catalog, thumbs, parent=None):
        super().__init__(parent)
        self.catalog, self.thumbs = catalog, thumbs
        self.queue: list[dict] = []
        self.order: list[int] = []
        self.position_in_order = 0
        self.shuffle = False
        self.repeat = "off"
        self.failures = 0
        self.seeking = False

        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)

        self.cover = QLabel()
        self.cover.setFixedSize(48, 48)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.subtitle = QLabel()
        self.subtitle.setProperty("muted", True)
        info = QVBoxLayout()
        info.setSpacing(0)
        info.addWidget(self.title)
        info.addWidget(self.subtitle)
        info_box = QWidget()
        info_box.setLayout(info)
        info_box.setFixedWidth(260)

        self.shuffle_button = QPushButton("Aleatorio")
        self.shuffle_button.setCheckable(True)
        self.previous_button = QPushButton("⏮")
        self.play_button = QPushButton("⏸")
        self.play_button.setProperty("primary", True)
        self.next_button = QPushButton("⏭")
        self.repeat_button = QPushButton(REPEAT_MODES[0][1])
        self.position = SeekSlider()
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setProperty("muted", True)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self.queue_button = QPushButton("Cola")

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)
        row.addWidget(self.cover)
        row.addWidget(info_box)
        for widget in (self.shuffle_button, self.previous_button, self.play_button, self.next_button,
                       self.repeat_button):
            row.addWidget(widget)
        row.addWidget(self.position, 1)
        row.addWidget(self.time_label)
        row.addWidget(QLabel("Vol."))
        row.addWidget(self.volume)
        row.addWidget(self.queue_button)

        self.shuffle_button.toggled.connect(self.set_shuffle)
        self.previous_button.clicked.connect(self.previous)
        self.play_button.clicked.connect(self.toggle)
        self.next_button.clicked.connect(lambda: self.next(manual=True))
        self.repeat_button.clicked.connect(self._cycle_repeat)
        self.queue_button.clicked.connect(self._show_queue)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        self.position.sliderPressed.connect(lambda: setattr(self, "seeking", True))
        self.position.sliderReleased.connect(self._seek)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(lambda ms: self.position.setRange(0, ms))
        self.player.playbackStateChanged.connect(
            lambda state: self.play_button.setText("⏸" if state == QMediaPlayer.PlaybackState.PlayingState else "▶"))
        self.player.mediaStatusChanged.connect(self._status_changed)
        self.player.errorOccurred.connect(self._error)
        thumbs.updated.connect(self._thumb_updated)
        self.hide()

    @property
    def current(self) -> dict | None:
        if not self.queue:
            return None
        return self.queue[self.order[self.position_in_order]]

    # Cola

    def play(self, items: list[dict], index: int = 0) -> None:
        self.queue = list(items)
        self.order = list(range(len(self.queue)))
        if self.shuffle:
            self._shuffle_from(index)
        else:
            self.position_in_order = index
        self.failures = 0
        self.show()
        self._load()

    def enqueue(self, items: list[dict]) -> None:
        if not self.queue:
            self.play(items)
            return
        start = len(self.queue)
        self.queue.extend(items)
        self.order.extend(range(start, len(self.queue)))
        self._update_queue_label()

    def play_next(self, items: list[dict]) -> None:
        if not self.queue:
            self.play(items)
            return
        start = len(self.queue)
        self.queue.extend(items)
        new = list(range(start, len(self.queue)))
        self.order[self.position_in_order + 1:self.position_in_order + 1] = new
        self._update_queue_label()

    def upcoming(self) -> list[dict]:
        return [self.queue[i] for i in self.order[self.position_in_order + 1:]]

    def set_shuffle(self, enabled: bool) -> None:
        self.shuffle = enabled
        if self.shuffle_button.isChecked() != enabled:
            self.shuffle_button.setChecked(enabled)
        if not self.queue:
            return
        current = self.order[self.position_in_order]
        if enabled:
            self._shuffle_from(current)
        else:
            self.order = list(range(len(self.queue)))
            self.position_in_order = current

    def _shuffle_from(self, index: int) -> None:
        rest = [i for i in range(len(self.queue)) if i != index]
        random.shuffle(rest)
        self.order = [index] + rest
        self.position_in_order = 0

    def _cycle_repeat(self) -> None:
        modes = [m for m, _label in REPEAT_MODES]
        self.repeat = modes[(modes.index(self.repeat) + 1) % len(modes)]
        self.repeat_button.setText(dict(REPEAT_MODES)[self.repeat])

    # Reproducción

    def _load(self) -> None:
        item = self.current
        self.player.stop()
        self.title.setText(display_title(item))
        meta = effective(item)
        self.subtitle.setText(" · ".join(p for p in (meta.get("artist"), meta.get("album")) if p))
        self._paint_cover()
        self._update_queue_label()
        self.current_changed.emit(item)
        path = full_path(item)
        if not os.path.exists(path):
            self._error(None, "No se encuentra el archivo")
            return
        self.catalog.record_play(item["id"])
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()

    def toggle(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif self.queue and self.player.source().isEmpty():
            self._load()
        elif self.queue:
            self.player.play()

    def release(self, paths: set[str]) -> bool:
        """Suelta la canción actual si es uno de `paths`, para que se pueda escribir en ella."""
        item = self.current
        if not item or os.path.normcase(full_path(item)) not in paths:
            return False
        self.player.stop()
        self.player.setSource(QUrl())
        self.subtitle.setText("Detenida para escribir sus metadatos. Pulsa ▶ para seguir.")
        return True

    def pause(self) -> None:
        self.player.pause()

    def next(self, manual: bool = False) -> None:
        if not self.queue:
            return
        if self.repeat == "one" and not manual:
            self._load()
            return
        if self.position_in_order + 1 < len(self.order):
            self.position_in_order += 1
        elif self.repeat == "all" or manual:
            self.position_in_order = 0
        else:
            self.player.stop()
            return
        self._load()

    def previous(self) -> None:
        if not self.queue:
            return
        # Como en los reproductores habituales: pasados 3 segundos, vuelve al principio de la pista.
        if self.player.position() > 3000 or self.position_in_order == 0:
            self.player.setPosition(0)
            return
        self.position_in_order -= 1
        self._load()

    def jump_to(self, offset: int) -> None:
        self.position_in_order += offset
        self._load()

    def stop(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.queue, self.order = [], []
        self.hide()

    def _status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.failures = 0
            self.next()

    def _error(self, _error, text: str) -> None:
        self.subtitle.setText(f"No se puede reproducir: {text}")
        self.failures += 1
        # Salta las pistas rotas, sin dar vueltas para siempre si fallan todas.
        if self.failures < len(self.queue):
            self.next(manual=self.repeat == "one")

    def _seek(self) -> None:
        self.seeking = False
        self.player.setPosition(self.position.value())

    def _position_changed(self, ms: int) -> None:
        if not self.seeking:
            self.position.setValue(ms)
        self.time_label.setText(f"{format_time(ms)} / {format_time(self.player.duration())}")

    def _paint_cover(self) -> None:
        item = self.current
        if not item:
            return
        pixmap = QPixmap(QSize(48, 48))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_cover(painter, self.thumbs.get(item), pixmap.rect(), 4)
        painter.end()
        self.cover.setPixmap(pixmap)

    def _thumb_updated(self, item_id: int) -> None:
        if self.current and self.current["id"] == item_id:
            self._paint_cover()

    def _update_queue_label(self) -> None:
        self.queue_button.setText(f"Cola ({len(self.upcoming())})")

    def _show_queue(self) -> None:
        menu = QMenu(self)
        upcoming = self.upcoming()
        if not upcoming:
            menu.addAction("No hay nada después de esta pista").setEnabled(False)
        for offset, item in enumerate(upcoming[:30], 1):
            menu.addAction(f"{offset}. {display_title(item)}", lambda o=offset: self.jump_to(o))
        if len(upcoming) > 30:
            menu.addAction(f"… y {len(upcoming) - 30} más").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Vaciar y detener", self.stop)
        menu.exec(self.queue_button.mapToGlobal(self.queue_button.rect().topLeft()))

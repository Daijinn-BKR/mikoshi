"""Reproductor de vídeo, música y fotos: incrustable en una página o en su propia ventana."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QIcon, QImageReader, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
    QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSlider,
    QStackedWidget, QStyle, QVBoxLayout, QWidget,
)

from .db import display_title, full_path

PLAYABLE = {"video", "audio", "image"}
HIDE_CONTROLS_AFTER_MS = 2500

# Teclas que no deben mostrar los controles: volumen, multimedia y brillo del teclado del portátil
# (Fn + F3/F5/F6 y similares) y los modificadores pulsados solos.
SILENT_KEYS = {
    Qt.Key.Key_VolumeUp, Qt.Key.Key_VolumeDown, Qt.Key.Key_VolumeMute, Qt.Key.Key_MicMute,
    Qt.Key.Key_MediaPlay, Qt.Key.Key_MediaPause, Qt.Key.Key_MediaTogglePlayPause, Qt.Key.Key_MediaStop,
    Qt.Key.Key_MediaNext, Qt.Key.Key_MediaPrevious, Qt.Key.Key_MonBrightnessUp, Qt.Key.Key_MonBrightnessDown,
    Qt.Key.Key_KeyboardBrightnessUp, Qt.Key.Key_KeyboardBrightnessDown, Qt.Key.Key_KeyboardLightOnOff,
    Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr,
    Qt.Key.Key_unknown,
}
# Los mismos, según el código de Windows, por si el teclado no los traduce a una tecla de Qt.
SILENT_WINDOWS_KEYS = set(range(0xAD, 0xB8))


def is_silent_key(event) -> bool:
    return event.key() in SILENT_KEYS or event.nativeVirtualKey() in SILENT_WINDOWS_KEYS

# Controles sobre el vídeo, como en YouTube: transparentes, con un degradado oscuro para leerse.
OVERLAY_STYLE = """
#overlay { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(0,0,0,0), stop:1 rgba(0,0,0,200)); }
#overlay QPushButton { background: transparent; border: none; color: white; padding: 6px 10px; font-weight: 600; }
#overlay QPushButton:hover { background: rgba(255,255,255,40); border-radius: 4px; }
#overlay QPushButton:disabled { color: rgba(255,255,255,80); }
#overlay QLabel { color: white; background: transparent; }
"""


def _draw_icon(name: str, color: QColor) -> QPixmap:
    """Iconos del reproductor con las formas de YouTube, dibujados en una cuadrícula de 48 × 48."""
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    triangle = lambda *points: painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
    if name == "play":
        triangle((15, 10), (15, 38), (38, 24))
    elif name == "pause":
        painter.drawRect(QRectF(13, 10, 8, 28))
        painter.drawRect(QRectF(27, 10, 8, 28))
    elif name == "next":
        triangle((12, 12), (12, 36), (31, 24))
        painter.drawRect(QRectF(32, 12, 4, 24))
    elif name == "previous":
        triangle((36, 12), (36, 36), (17, 24))
        painter.drawRect(QRectF(12, 12, 4, 24))
    else:
        # Esquinas hacia fuera para entrar en pantalla completa; hacia dentro para salir.
        pen = QPen(color, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        outward = [((10, 19), (10, 10), (19, 10)), ((29, 10), (38, 10), (38, 19)),
                   ((38, 29), (38, 38), (29, 38)), ((19, 38), (10, 38), (10, 29))]
        inward = [((10, 18), (18, 18), (18, 10)), ((30, 10), (30, 18), (38, 18)),
                  ((38, 30), (30, 30), (30, 38)), ((18, 38), (18, 30), (10, 30))]
        for a, b, c in outward if name == "fullscreen" else inward:
            path = QPainterPath(QPointF(*a))
            path.lineTo(QPointF(*b))
            path.lineTo(QPointF(*c))
            painter.drawPath(path)
    painter.end()
    return pixmap


def player_icon(name: str) -> QIcon:
    icon = QIcon(_draw_icon(name, QColor("white")))
    icon.addPixmap(_draw_icon(name, QColor(255, 255, 255, 80)), QIcon.Mode.Disabled)
    return icon


def format_time(ms: int) -> str:
    total = max(ms, 0) // 1000
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


class PlayerHooks:
    """Lo que el reproductor cuenta al catálogo. Sin catálogo, no hace nada.

    `count_at` cuenta la reproducción solo al llegar a esa fracción del vídeo, y
    `ask_resume(item, ms)` decide si se continúa desde donde se dejó.
    """

    def __init__(self, catalog=None, resume: bool = False, count_at: float | None = None, ask_resume=None):
        self.catalog, self.resume, self.count_at, self.ask_resume = catalog, resume, count_at, ask_resume

    def start_position(self, item: dict) -> int:
        if not (self.catalog and self.resume):
            return 0
        stats = self.catalog.activity().get(item["id"], {})
        position = 0 if stats.get("watched") else stats.get("position_ms", 0)
        if position and self.ask_resume and not self.ask_resume(item, position):
            return 0
        return position

    def played(self, item: dict) -> None:
        if self.catalog and self.count_at is None:
            self.catalog.record_play(item["id"])

    def reached(self, position: int, duration: int) -> bool:
        return self.count_at is not None and duration > 0 and position >= duration * self.count_at

    def count(self, item: dict) -> None:
        if self.catalog:
            self.catalog.record_play(item["id"])

    def progress(self, item: dict, position: int, duration: int) -> None:
        if self.catalog and item["kind"] == "video" and duration > 0:
            self.catalog.save_progress(item["id"], position, duration)


class SeekSlider(QSlider):
    """Barra de progreso: un clic salta a ese punto, como en YouTube, y también se puede arrastrar."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                   int(event.position().x()), self.width())
            self.setValue(value)
        # El asa ya está bajo el cursor: el comportamiento normal permite seguir arrastrando.
        super().mousePressEvent(event)


class VideoView(QGraphicsView):
    """Imagen del vídeo. A diferencia de una superficie nativa, admite controles dibujados encima.

    Doble clic, F o F11 cambian a pantalla completa; Esc sale; Espacio pausa; las flechas saltan 10 s.
    """

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsVideoItem()
        self.scene().addItem(self.item)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(QColor("black")))
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.item.nativeSizeChanged.connect(lambda _size: self._fit())

    def _fit(self) -> None:
        size = self.viewport().size()
        self.item.setSize(QSizeF(size))
        self.scene().setSceneRect(0, 0, size.width(), size.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def mouseDoubleClickEvent(self, event) -> None:
        self.owner.toggle_fullscreen()

    def mousePressEvent(self, event) -> None:
        self.setFocus()
        self.owner.poke()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_F, Qt.Key.Key_F11) or (key == Qt.Key.Key_Escape and self.owner.immersive):
            self.owner.toggle_fullscreen()
        elif key == Qt.Key.Key_Space:
            self.owner.toggle()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = 10_000 if key == Qt.Key.Key_Right else -10_000
            self.owner.player.setPosition(max(0, self.owner.player.position() + step))
        else:
            super().keyPressEvent(event)


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
        self.counted = False
        self.immersive = False
        self.hidden_for_fullscreen: list[QWidget] = []
        self.saved_margins: list = []
        self.saved_window_state = None

        self.video = VideoView(self)
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
        self.player.setVideoOutput(self.video.item)

        self.icons = {name: player_icon(name)
                      for name in ("play", "pause", "previous", "next", "fullscreen", "exit_fullscreen")}
        self.previous_button = QPushButton(toolTip="Vídeo anterior")
        self.play_button = QPushButton(toolTip="Pausa (Espacio)")
        self.next_button = QPushButton(toolTip="Siguiente vídeo")
        self.fullscreen_button = QPushButton(toolTip="Pantalla completa (F o doble clic)")
        for button, icon in ((self.previous_button, "previous"), (self.play_button, "pause"),
                             (self.next_button, "next"), (self.fullscreen_button, "fullscreen")):
            button.setIcon(self.icons[icon])
            button.setIconSize(QSize(28, 28))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.position = SeekSlider()
        self.time_label = QLabel("0:00 / 0:00")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(100)
        self.audio.setVolume(0.8)

        # La barra de progreso ocupa todo el ancho y los botones van debajo, sobre el propio vídeo.
        self.stage_box = QWidget()
        self.stage.setParent(self.stage_box)
        self.controls = QWidget(self.stage_box)
        self.controls.setObjectName("overlay")
        self.controls.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.controls.setStyleSheet(OVERLAY_STYLE)
        buttons = QHBoxLayout()
        for widget in (self.previous_button, self.play_button, self.next_button, self.time_label):
            buttons.addWidget(widget)
        buttons.addStretch()
        buttons.addWidget(QLabel("Vol."))
        buttons.addWidget(self.volume)
        buttons.addWidget(self.fullscreen_button)
        overlay = QVBoxLayout(self.controls)
        overlay.setContentsMargins(12, 24, 12, 6)
        overlay.setSpacing(2)
        overlay.addWidget(self.position)
        overlay.addLayout(buttons)
        self.stage_box.installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stage_box, 1)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(HIDE_CONTROLS_AFTER_MS)
        self.hide_timer.timeout.connect(self._auto_hide)

        self.previous_button.clicked.connect(lambda: self.go(-1))
        self.next_button.clicked.connect(lambda: self.go(1))
        self.play_button.clicked.connect(self.toggle)
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        self.position.sliderPressed.connect(lambda: setattr(self, "seeking", True))
        self.position.sliderReleased.connect(self._seek)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(lambda ms: self.position.setRange(0, ms))
        self.player.playbackStateChanged.connect(self._state_changed)
        self.player.errorOccurred.connect(self._error)
        self.player.mediaStatusChanged.connect(self._status_changed)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.toggle)
        QShortcut(QKeySequence(Qt.Key.Key_F), self, self.toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key.Key_F11), self, self.toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, lambda: self.immersive and self.toggle_fullscreen())
        if items:
            self.load()

    @property
    def item(self) -> dict:
        return self.items[self.index]

    # Controles que se ocultan solos. Solo se escucha el teclado y el ratón mientras el reproductor
    # está a la vista: así no frena al resto de la aplicación.

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QApplication.instance().installEventFilter(self)

    def hideEvent(self, event) -> None:
        QApplication.instance().removeEventFilter(self)
        self.hide_timer.stop()
        super().hideEvent(event)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if watched is self.stage_box and kind == QEvent.Type.Resize:
            self._place_controls()
        elif kind == QEvent.Type.KeyPress and self.isVisible() and not is_silent_key(event):
            self.poke()
        elif kind == QEvent.Type.MouseMove and self.isVisible() and self._mouse_over_video():
            self.poke()
        return False

    def _mouse_over_video(self) -> bool:
        return self.stage_box.rect().contains(self.stage_box.mapFromGlobal(QCursor.pos()))

    def _place_controls(self) -> None:
        rect = self.stage_box.rect()
        self.stage.setGeometry(rect)
        height = self.controls.sizeHint().height()
        self.controls.setGeometry(0, rect.height() - height, rect.width(), height)
        self.controls.raise_()

    def _should_hide(self) -> bool:
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        return (playing and self.stage.currentWidget() is self.video and not self.seeking
                and not self.controls.underMouse())

    def poke(self) -> None:
        """Hubo actividad: se muestran los controles y vuelve a contar el tiempo para ocultarlos."""
        self.controls.show()
        self.video.viewport().unsetCursor()
        if self._should_hide() or self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.hide_timer.start()

    def _auto_hide(self) -> None:
        if self._should_hide():
            self.controls.hide()
            self.video.viewport().setCursor(Qt.CursorShape.BlankCursor)
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            # El ratón está sobre los controles o se está arrastrando la barra: se vuelve a intentar.
            self.hide_timer.start()

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
        self.fullscreen_button.setVisible(kind == "video")
        self.counted = False
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

    def toggle_fullscreen(self) -> None:
        """Como YouTube: la propia ventana ocupa la pantalla y solo queda el reproductor.

        No se abre otra ventana: el vídeo sigue en su sitio y al salir todo vuelve a estar como antes.
        """
        if self.immersive:
            self._leave_fullscreen()
        elif self.items and self.item["kind"] == "video":
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        window = self.window()
        self.hidden_for_fullscreen, self.saved_margins = [], []
        node = self
        while node is not window:
            parent = node.parentWidget()
            for child in parent.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
                if child is not node and child.isVisible() and not child.isWindow():
                    child.hide()
                    self.hidden_for_fullscreen.append(child)
            if parent.layout() is not None:
                self.saved_margins.append((parent.layout(), parent.layout().contentsMargins()))
                parent.layout().setContentsMargins(0, 0, 0, 0)
            node = parent
        self.saved_window_state = window.windowState()
        self.immersive = True
        self.fullscreen_button.setIcon(self.icons["exit_fullscreen"])
        self.fullscreen_button.setToolTip("Salir de pantalla completa (Esc)")
        window.showFullScreen()
        self.video.setFocus()

    def _leave_fullscreen(self) -> None:
        window = self.window()
        for widget in self.hidden_for_fullscreen:
            widget.show()
        for layout, margins in self.saved_margins:
            layout.setContentsMargins(margins)
        self.hidden_for_fullscreen, self.saved_margins = [], []
        self.immersive = False
        self.fullscreen_button.setIcon(self.icons["fullscreen"])
        self.fullscreen_button.setToolTip("Pantalla completa (F o doble clic)")
        window.setWindowState(self.saved_window_state or Qt.WindowState.WindowNoState)
        window.show()

    def _count_if_reached(self, position: int) -> None:
        if self.loaded_item and not self.counted and self.hooks.reached(position, self.player.duration()):
            self.counted = True
            self.hooks.count(self.loaded_item)

    def _position_changed(self, ms: int) -> None:
        self._count_if_reached(ms)
        if not self.seeking:
            self.position.setValue(ms)
        self.time_label.setText(f"{format_time(ms)} / {format_time(self.player.duration())}")

    def _state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(self.icons["pause" if playing else "play"])
        self.play_button.setToolTip("Pausa (Espacio)" if playing else "Reproducir (Espacio)")
        # En pausa los controles se quedan a la vista; al reproducir, se ocultan tras un rato.
        self.poke()
        if not playing:
            self.hide_timer.stop()

    def _status_changed(self, status) -> None:
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and self.pending_seek:
            # Continúa donde se dejó.
            self.player.setPosition(self.pending_seek)
            self.pending_seek = 0
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._count_if_reached(self.player.duration())
            if self.loaded_item:
                self.hooks.progress(self.loaded_item, self.player.duration(), self.player.duration())
                self.loaded_item = None
            if self.index < len(self.items) - 1:
                self.go(1)

    def _error(self, _error, text: str) -> None:
        self.loaded_item = None
        self._show_message(f"Este archivo no se puede reproducir aquí.\n"
                           f"Ábrelo con otro programa desde «Ver en carpeta».\n\n{text}")

    def release(self, paths: set[str]) -> bool:
        """Suelta el archivo si es uno de `paths`, para que se pueda escribir en él."""
        if not self.items or os.path.normcase(full_path(self.item)) not in paths:
            return False
        self.stop()
        self._show_message("Reproducción detenida para escribir los metadatos.\nPulsa ▶ para seguir.")
        return True

    def stop(self) -> None:
        if self.immersive:
            self._leave_fullscreen()
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

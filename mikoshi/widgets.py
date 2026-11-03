"""Piezas compartidas por todas las secciones: formato, miniaturas y tarjetas."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections import OrderedDict

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QRect, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

from . import metadata, thumbnails
from .db import display_title, effective

KIND_STYLE = {
    "video": ("#3f6fb5", "VÍDEO"),
    "audio": ("#8a4fb0", "AUDIO"),
    "image": ("#3a9276", "IMAGEN"),
    "document": ("#b5793f", "LIBRO"),
    "game": ("#b54f5c", "JUEGO"),
}
POSTER, WIDE, SQUARE = 1.5, 9 / 16, 1.0


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def stars(rating: int) -> str:
    return "★" * rating + "☆" * (5 - rating)


def number(value, missing: int = 10 ** 6) -> int:
    match = re.match(r"\d+", str(value or ""))
    return int(match.group(0)) if match else missing


def creator(item: dict) -> str:
    meta = effective(item)
    return next((meta[k] for k in metadata.CREATOR_KEYS if meta.get(k)), "")


def year(item: dict) -> str:
    meta = effective(item)
    return meta.get("year") or meta.get("date", "")[:4]


def seconds(item: dict) -> int:
    """Duración en segundos a partir del texto técnico «h:mm:ss»."""
    parts = item["technical"].get("Duración", "").split(":")
    try:
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return total
    except ValueError:
        return 0


def clock(total: int) -> str:
    if total <= 0:
        return ""
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def ago(timestamp: float) -> str:
    days = int((time.time() - timestamp) // 86400)
    if days < 1:
        return "hoy"
    if days == 1:
        return "ayer"
    if days < 30:
        return f"hace {days} días"
    if days < 365:
        return f"hace {days // 30} meses"
    return f"hace {days // 365} años"


def video_category(item: dict, favorite_roots: set[int]) -> str:
    """'favorite', 'series' o 'movie'. Lo elegido a mano manda sobre lo deducido."""
    if item["root_id"] in favorite_roots:
        return "favorite"
    meta = effective(item)
    if meta.get("category") in ("movie", "series"):
        return meta["category"]
    return "series" if meta.get("series") or meta.get("episode") else "movie"


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


# Miniaturas compartidas: se crean en segundo plano una sola vez para todas las vistas.

class ThumbSignals(QObject):
    ready = Signal(int, str, QImage)


class ThumbJob(QRunnable):
    def __init__(self, item: dict, key: str, signals: ThumbSignals):
        super().__init__()
        self.item, self.key, self.signals = item, key, signals

    def run(self) -> None:
        path = thumbnails.thumbnail_for(self.item)
        self.signals.ready.emit(self.item["id"], self.key, QImage(path) if path else QImage())


class Thumbs(QObject):
    updated = Signal(int)
    MAX_CACHED = 3000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.placeholders = {kind: placeholder(kind) for kind in KIND_STYLE}
        self.pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self.requested: set[str] = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.signals = ThumbSignals()
        self.signals.ready.connect(self._ready)

    @staticmethod
    def key(item: dict) -> str:
        return f"{item['id']}|{item['mtime']}|{item['cover']}"

    def get(self, item: dict) -> QPixmap:
        key = self.key(item)
        cached = self.pixmaps.get(key)
        if cached is not None:
            self.pixmaps.move_to_end(key)
            return cached
        if key not in self.requested and item["status"] == "ok":
            self.requested.add(key)
            self.pool.start(ThumbJob(item, key, self.signals))
        return self.placeholders[item["kind"]]

    def has(self, item: dict) -> bool:
        return self.key(item) in self.pixmaps

    def _ready(self, item_id: int, key: str, image: QImage) -> None:
        if image.isNull():
            return
        self.pixmaps[key] = QPixmap.fromImage(image)
        while len(self.pixmaps) > self.MAX_CACHED:
            self.pixmaps.popitem(last=False)
        self.updated.emit(item_id)

    def shutdown(self) -> None:
        self.pool.clear()
        self.pool.waitForDone()


# Tarjetas: cuadrícula de pósteres, carátulas o miniaturas de vídeo

SUBTITLE_ROLE = Qt.ItemDataRole.UserRole + 1
BADGE_ROLE = Qt.ItemDataRole.UserRole + 2
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 3


class CardModel(QAbstractListModel):
    """Lista de tarjetas. `subtitle`, `badge` y `progress` son funciones que reciben el elemento."""

    def __init__(self, thumbs: Thumbs, subtitle=None, badge=None, progress=None, title=display_title, parent=None):
        super().__init__(parent)
        self.thumbs, self.items = thumbs, []
        self.title, self.subtitle, self.badge, self.progress = title, subtitle, badge, progress
        self.cover_of = lambda item: item
        thumbs.updated.connect(self._thumb_updated)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self.title(item)
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbs.get(self.cover_of(item))
        if role == SUBTITLE_ROLE and self.subtitle:
            return self.subtitle(item)
        if role == BADGE_ROLE and self.badge:
            return self.badge(item)
        if role == PROGRESS_ROLE and self.progress:
            return self.progress(item)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.title(item)
        return None

    def set_items(self, items: list) -> None:
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def _thumb_updated(self, item_id: int) -> None:
        for row, item in enumerate(self.items):
            if self.cover_of(item)["id"] == item_id:
                index = self.index(row)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])


def draw_cover(painter: QPainter, pixmap: QPixmap, rect: QRect, radius: int = 6) -> None:
    """Rellena el rectángulo recortando la imagen, sin deformarla."""
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.save()
    painter.setClipPath(path)
    scaled = pixmap.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    x = rect.x() + (rect.width() - scaled.width()) // 2
    y = rect.y() + (rect.height() - scaled.height()) // 2
    painter.drawPixmap(x, y, scaled)
    painter.restore()


class CardDelegate(QStyledItemDelegate):
    def __init__(self, width: int, ratio: float, parent=None):
        super().__init__(parent)
        self.width, self.ratio = width, ratio

    def image_height(self) -> int:
        return int(self.width * self.ratio)

    def sizeHint(self, option, index):
        return QSize(self.width + 16, self.image_height() + 62)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        image = QRect(rect.x() + 8, rect.y() + 8, self.width, self.image_height())
        draw_cover(painter, index.data(Qt.ItemDataRole.DecorationRole), image)
        if option.state & QStyle.StateFlag.State_Selected:
            pen = painter.pen()
            pen.setColor(option.palette.highlight().color())
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(image.adjusted(-2, -2, 2, 2), 7, 7)
        progress = index.data(PROGRESS_ROLE)
        if progress:
            bar = QRect(image.x(), image.bottom() - 4, int(image.width() * min(progress, 1.0)), 4)
            painter.fillRect(QRect(image.x(), image.bottom() - 4, image.width(), 4), QColor(0, 0, 0, 150))
            painter.fillRect(bar, option.palette.highlight())
        badge = index.data(BADGE_ROLE)
        if badge:
            metrics = painter.fontMetrics()
            box = QRect(0, 0, metrics.horizontalAdvance(badge) + 10, 20)
            box.moveBottomRight(image.bottomRight() - QRect(0, 0, 6, 8).bottomRight())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 190))
            painter.drawRoundedRect(box, 4, 4)
            painter.setPen(QColor("white"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, badge)
        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(option.palette.text().color())
        text = QRect(image.x(), image.bottom() + 6, self.width, 34)
        title = painter.fontMetrics().elidedText(index.data() or "", Qt.TextElideMode.ElideRight, self.width * 2 - 10)
        flags = Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
        # El subtítulo va justo debajo de las líneas que ocupe el título, sean una o dos.
        used = painter.boundingRect(text, flags, title)
        painter.drawText(text, flags, title)
        subtitle = index.data(SUBTITLE_ROLE)
        if subtitle:
            painter.setFont(option.font)
            painter.setPen(option.palette.placeholderText().color())
            line = QRect(image.x(), min(used.bottom(), text.bottom()) + 3, self.width, 18)
            painter.drawText(line, Qt.AlignmentFlag.AlignLeft,
                             painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight, self.width))
        painter.restore()


def card_view(model: CardModel, width: int, ratio: float, row: bool = False) -> QListView:
    """Cuadrícula de tarjetas; con `row` es una sola fila desplazable, como «Continuar viendo»."""
    view = QListView()
    view.setModel(model)
    view.setItemDelegate(CardDelegate(width, ratio, view))
    view.setViewMode(QListView.ViewMode.IconMode)
    view.setMovement(QListView.Movement.Static)
    view.setResizeMode(QListView.ResizeMode.Adjust)
    view.setUniformItemSizes(True)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    if row:
        view.setFlow(QListView.Flow.LeftToRight)
        view.setWrapping(False)
        view.setFixedHeight(int(width * ratio) + 84)
        view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    return view

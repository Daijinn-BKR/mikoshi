"""Recorre las carpetas raíz y sincroniza el catálogo.

Regla de seguridad: un servidor apagado o una carpeta ilegible no significan que
los archivos se hayan borrado. Solo se marcan como faltantes tras un recorrido
completo y sin errores, y nunca se elimina nada del catálogo automáticamente.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import metadata
from .db import Catalog, full_path

EXTENSIONS = {
    "video": {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".flv"},
    "audio": {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".wav", ".wma", ".aac", ".aiff", ".ape"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".cr2", ".nef", ".arw", ".dng"},
    "document": {".pdf", ".epub", ".mobi", ".azw3", ".cbz", ".cbr", ".djvu", ".doc", ".docx", ".odt", ".txt", ".rtf"},
    "game": {".nes", ".sfc", ".smc", ".gb", ".gbc", ".gba", ".n64", ".z64", ".v64", ".nds", ".3ds", ".gcm",
             ".wbfs", ".rvz", ".xci", ".nsp", ".chd", ".md", ".gen", ".sms", ".pce"},
}
# En una carpeta marcada como de juegos, estos también cuentan. Fuera de ella un
# .iso o un .zip podría ser cualquier cosa.
GAME_ROOT_EXTENSIONS = EXTENSIONS["game"] | {".iso", ".cue", ".exe", ".lnk", ".url", ".zip", ".7z", ".rar", ".pkg"}
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "@eaDir", "#recycle", "#snapshot", "lost+found"}

_EXT_TO_KIND = {ext: kind for kind, exts in EXTENSIONS.items() for ext in exts}


class RootOffline(Exception):
    """La carpeta raíz no está accesible (servidor apagado, red caída...)."""


@dataclass
class ScanResult:
    root: str = ""
    added: int = 0
    updated: int = 0
    moved: int = 0
    missing: int = 0
    restored: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


def classify(name: str) -> str | None:
    return _EXT_TO_KIND.get(Path(name).suffix.lower())


def _folder_stat(path: str) -> tuple[int, float]:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total, os.path.getmtime(path)


def _iter_games(root: str, errors: list[str]):
    """En una raíz de juegos, cada hijo directo es un juego: archivo o carpeta completa."""
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name.startswith(".") or entry.name in SKIP_DIRS:
                continue
            try:
                if entry.is_dir():
                    size, mtime = _folder_stat(entry.path)
                    yield entry.name, "game", size, mtime
                elif Path(entry.name).suffix.lower() in GAME_ROOT_EXTENSIONS:
                    stat = entry.stat()
                    yield entry.name, "game", stat.st_size, stat.st_mtime
            except OSError as exc:
                errors.append(f"{entry.path}: {exc}")


def _iter_auto(root: str, errors: list[str]):
    def on_error(exc: OSError) -> None:
        errors.append(f"{exc.filename}: {exc.strerror}")

    for base, dirs, files in os.walk(root, onerror=on_error):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            kind = classify(name)
            if not kind or name.startswith(metadata.TEMP_PREFIX):
                continue
            path = os.path.join(base, name)
            try:
                stat = os.stat(path)
            except OSError as exc:
                errors.append(f"{path}: {exc.strerror}")
                continue
            yield os.path.relpath(path, root), kind, stat.st_size, stat.st_mtime


def apply_edits_to_file(catalog: Catalog, item: dict) -> list[str]:
    """Escribe las ediciones pendientes en el archivo y sincroniza el catálogo.

    Solo deja de ser edición lo que se relee igual del archivo: si un formato no
    conserva un campo, el valor sigue a salvo en el catálogo y se devuelve su nombre.
    """
    path = full_path(item)
    pending = metadata.pending_edits(item, path)
    if not pending:
        return []
    metadata.write_file(path, item["kind"], pending)
    stat = os.stat(path)
    fields, technical = metadata.read_file(path, item["kind"])
    catalog.refresh_from_file(item["id"], stat.st_size, stat.st_mtime, fields, technical)
    edits = {k: v for k, v in item["edits"].items() if k not in pending or fields.get(k, "") != v}
    catalog.save_user_data(item["id"], edits=edits)
    return [k for k in pending if k in edits]


def scan_root(catalog: Catalog, root_id: int, progress=None, cancelled=None) -> ScanResult:
    root = catalog.root(root_id)
    path = root["path"]
    result = ScanResult(root=path)
    if not os.path.isdir(path):
        raise RootOffline(path)

    known = dict(catalog.known_items(root_id))
    seen: set[str] = set()
    new_entries = []
    iterator = _iter_games if root["mode"] == "games" else _iter_auto
    count = 0
    for rel, kind, size, mtime in iterator(path, result.errors):
        if cancelled and cancelled():
            result.cancelled = True
            break
        seen.add(rel)
        count += 1
        if progress and count % 20 == 0:
            progress(f"{Path(path).name}: {count} archivos revisados")
        row = known.get(rel)
        if row is None:
            new_entries.append((rel, kind, size, mtime))
        elif row["size"] != size or abs(row["mtime"] - mtime) > 1:
            fields, technical = metadata.read_file(os.path.join(path, rel), kind)
            catalog.refresh_from_file(row["id"], size, mtime, fields, technical)
            result.updated += 1
        elif row["status"] != "ok":
            catalog.set_status(row["id"], "ok")
            result.restored += 1
        if count % 200 == 0:
            catalog.commit()

    complete = not result.cancelled and not result.errors
    absent = [known[rel] for rel in known if rel not in seen] if complete else []

    # Un archivo que desaparece y reaparece con igual nombre y tamaño se ha movido:
    # se conservan sus etiquetas y ediciones.
    candidates: dict[tuple[str, int], list] = {}
    for row in absent:
        candidates.setdefault((Path(row["rel_path"]).name, row["size"]), []).append(row)

    for index, (rel, kind, size, mtime) in enumerate(new_entries, 1):
        if cancelled and cancelled():
            result.cancelled = True
            break
        matches = candidates.get((Path(rel).name, size))
        if matches and complete:
            catalog.move_item(matches.pop()["id"], rel, mtime)
            result.moved += 1
            continue
        if progress:
            progress(f"{Path(path).name}: leyendo {index}/{len(new_entries)} · {Path(rel).name}")
        fields, technical = metadata.read_file(os.path.join(path, rel), kind)
        catalog.insert_item(root_id, rel, kind, size, mtime, fields, technical)
        result.added += 1
        if index % 50 == 0:
            catalog.commit()

    for rows in candidates.values():
        for row in rows:
            if row["status"] != "missing":
                catalog.set_status(row["id"], "missing")
                result.missing += 1
    catalog.commit()
    return result

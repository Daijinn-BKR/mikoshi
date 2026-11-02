"""Comprobaciones de la colección. Solo leen los archivos: nunca los cambian ni borran."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .db import Catalog, full_path

CHUNK = 1024 * 1024


class Cancelled(Exception):
    pass


def hash_file(path: str, cancelled=None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            if cancelled and cancelled():
                raise Cancelled
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class VerifyResult:
    ok: int = 0
    baseline: int = 0
    skipped: int = 0
    cancelled: bool = False
    corrupted: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def verify_integrity(catalog: Catalog, item_ids=None, progress=None, cancelled=None) -> VerifyResult:
    """Detecta archivos dañados en silencio.

    La primera vez guarda la huella de cada archivo. Después, una huella distinta con
    el mismo tamaño y la misma fecha indica daño, porque una edición normal cambiaría
    la fecha. Un archivo editado solo renueva su huella.
    """
    result = VerifyResult()
    items = catalog.query_items(status="ok")
    if item_ids is not None:
        wanted = set(item_ids)
        items = [i for i in items if i["id"] in wanted]
    for index, item in enumerate(items, 1):
        path = full_path(item)
        if os.path.isdir(path):
            result.skipped += 1
            continue
        if progress:
            progress(f"Comprobando {index}/{len(items)} · {Path(path).name}")
        try:
            stat = os.stat(path)
            checksum = hash_file(path, cancelled)
        except Cancelled:
            result.cancelled = True
            break
        except OSError as exc:
            result.unreadable.append(f"{path}: {exc.strerror or exc}")
            continue
        same_version = item["checksum"] and stat.st_size == item["size"] and stat.st_mtime == item["checksum_mtime"]
        if same_version and checksum != item["checksum"]:
            # La huella buena se conserva para poder comparar con una copia de respaldo.
            result.corrupted.append(path)
        elif same_version:
            result.ok += 1
        else:
            catalog.set_checksum(item["id"], checksum, stat.st_mtime)
            result.baseline += 1
    return result


@dataclass
class DuplicateResult:
    groups: list[list[str]] = field(default_factory=list)
    wasted: int = 0
    cancelled: bool = False
    unreadable: list[str] = field(default_factory=list)


def find_duplicates(catalog: Catalog, progress=None, cancelled=None) -> DuplicateResult:
    """Busca contenido idéntico. Solo calcula huellas de archivos con el mismo tamaño."""
    result = DuplicateResult()
    by_size: dict[int, list[dict]] = {}
    for item in catalog.query_items(status="ok"):
        if item["size"] > 0 and not os.path.isdir(full_path(item)):
            by_size.setdefault(item["size"], []).append(item)
    candidates = [group for group in by_size.values() if len(group) > 1]
    total = sum(len(group) for group in candidates)
    done = 0
    for group in candidates:
        by_hash: dict[str, list[str]] = {}
        for item in group:
            done += 1
            path = full_path(item)
            if progress:
                progress(f"Comparando {done}/{total} · {Path(path).name}")
            try:
                mtime = os.stat(path).st_mtime
                if item["checksum"] and item["checksum_mtime"] == mtime:
                    checksum = item["checksum"]
                else:
                    checksum = hash_file(path, cancelled)
                    catalog.set_checksum(item["id"], checksum, mtime)
            except Cancelled:
                result.cancelled = True
                return result
            except OSError as exc:
                result.unreadable.append(f"{path}: {exc.strerror or exc}")
                continue
            by_hash.setdefault(checksum, []).append(path)
        for paths in by_hash.values():
            if len(paths) > 1:
                result.groups.append(sorted(paths))
                result.wasted += group[0]["size"] * (len(paths) - 1)
    return result

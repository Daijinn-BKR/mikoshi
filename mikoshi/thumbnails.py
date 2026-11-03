"""Miniaturas guardadas en el disco local para no releer la red cada vez."""
from __future__ import annotations

import hashlib
import io
import posixpath
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote

from .db import covers_dir, data_dir, full_path

SIZE = (400, 400)
CACHE_VERSION = "2"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _save(source, dest: Path) -> bool:
    from PIL import Image, ImageOps

    with Image.open(source) as img:
        img.draft("RGB", SIZE)
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail(SIZE)
        img.save(dest, "JPEG", quality=85)
    return True


def _audio_art(path: str) -> bytes | None:
    import mutagen

    audio = mutagen.File(path)
    if audio is None:
        return None
    pictures = getattr(audio, "pictures", None)
    if pictures:
        return pictures[0].data
    for key, value in (audio.tags or {}).items():
        if str(key).startswith("APIC"):
            return value.data
        if key == "covr" and value:
            return bytes(value[0])
    return None


IMAGE_NAMES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _epub_cover(path: str) -> bytes | None:
    """La portada que declara el libro; si no declara ninguna, la imagen llamada «cover»."""
    with zipfile.ZipFile(path) as book:
        container = ET.fromstring(book.read("META-INF/container.xml"))
        opf_path = next(e for e in container.iter() if e.tag.endswith("rootfile")).attrib["full-path"]
        opf = ET.fromstring(book.read(opf_path))
        items = [e for e in opf.iter() if e.tag.endswith("}item") or e.tag == "item"]
        cover_ids = [e.attrib.get("content") for e in opf.iter()
                     if e.tag.endswith("meta") and e.attrib.get("name") == "cover"]
        href = next((i.attrib.get("href") for i in items if i.attrib.get("id") in cover_ids
                     and i.attrib.get("href", "").lower().endswith(IMAGE_NAMES)), None)
        href = href or next((i.attrib.get("href") for i in items
                             if "cover-image" in i.attrib.get("properties", "").split()), None)
        if href:
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), unquote(href)))
            if name in book.namelist():
                return book.read(name)
        images = [n for n in book.namelist() if n.lower().endswith(IMAGE_NAMES)]
        named = [n for n in images if "cover" in posixpath.basename(n).lower()]
        return book.read(named[0]) if named else None


def _comic_cover(path: str) -> bytes | None:
    with zipfile.ZipFile(path) as comic:
        pages = sorted(n for n in comic.namelist() if n.lower().endswith(IMAGE_NAMES))
        return comic.read(pages[0]) if pages else None


def _pdf_cover(path: str) -> bytes | None:
    """La imagen más grande de la primera página: en libros escaneados o con portada, es la portada."""
    from pypdf import PdfReader

    images = PdfReader(path).pages[0].images
    best = max(images, key=lambda image: len(image.data), default=None)
    return best.data if best else None


def _book_cover(path: str) -> bytes | None:
    reader = {".epub": _epub_cover, ".cbz": _comic_cover, ".pdf": _pdf_cover}.get(Path(path).suffix.lower())
    return reader(path) if reader else None


def _video_frame(path: str, dest: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    out = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-ss", "30", "-i", path, "-frames:v", "1",
         "-vf", f"scale={SIZE[0]}:-2", str(dest)],
        capture_output=True, timeout=60, creationflags=_NO_WINDOW,
    )
    if out.returncode or not dest.exists():
        # Vídeo de menos de 30 segundos: se usa el primer fotograma.
        out = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", path, "-frames:v", "1", "-vf", f"scale={SIZE[0]}:-2", str(dest)],
            capture_output=True, timeout=60, creationflags=_NO_WINDOW,
        )
    return out.returncode == 0 and dest.exists()


def save_cover(catalog, item: dict, data: bytes, suffix: str = ".jpg") -> str:
    """Guarda una portada propia y borra la anterior. Un nombre nuevo fuerza otra miniatura."""
    dest = covers_dir() / f"{item['id']}-{time.time_ns()}{suffix}"
    dest.write_bytes(data)
    if item["cover"]:
        (covers_dir() / Path(item["cover"]).name).unlink(missing_ok=True)
    catalog.save_user_data(item["id"], cover=str(dest))
    return str(dest)


def thumbnail_for(item: dict) -> str | None:
    """Ruta de la miniatura, creándola si hace falta. None si no hay imagen posible."""
    cache = data_dir() / "miniaturas"
    cache.mkdir(exist_ok=True)
    key = f"{CACHE_VERSION}|{item['id']}|{item['mtime']}|{item['cover']}"
    dest = cache / (hashlib.sha1(key.encode()).hexdigest() + ".jpg")
    if dest.exists():
        return str(dest)
    path = full_path(item)
    try:
        # La portada se busca por nombre: la carpeta de datos pudo cambiar de sitio.
        cover = covers_dir() / Path(item["cover"]).name if item["cover"] else None
        if cover and cover.exists():
            _save(cover, dest)
        elif item["kind"] == "image":
            _save(path, dest)
        elif item["kind"] == "audio":
            art = _audio_art(path)
            if not art:
                return None
            _save(io.BytesIO(art), dest)
        elif item["kind"] == "video":
            if not _video_frame(path, dest):
                return None
        elif item["kind"] == "document":
            data = _book_cover(path)
            if not data:
                return None
            _save(io.BytesIO(data), dest)
        else:
            return None
    except Exception:
        Path(dest).unlink(missing_ok=True)
        return None
    return str(dest)

"""Miniaturas guardadas en el disco local para no releer la red cada vez."""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
        else:
            return None
    except Exception:
        Path(dest).unlink(missing_ok=True)
        return None
    return str(dest)

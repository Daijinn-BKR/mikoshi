"""Lectura y escritura de metadatos en los archivos.

Toda escritura se hace sobre una copia temporal en la misma carpeta, y el archivo
original solo se sustituye al final. Si algo falla, el original queda intacto.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

TEMP_PREFIX = ".tmp-mikoshi-"

KIND_LABELS = {
    "video": "Películas y series",
    "audio": "Música",
    "image": "Fotos e imágenes",
    "document": "Libros y documentos",
    "game": "Juegos",
}

# Campos editables de cada tipo, en orden de formulario.
FIELDS = {
    "video": [("title", "Título"), ("year", "Año"), ("genre", "Género"), ("director", "Director"),
              ("series", "Serie"), ("season", "Temporada"), ("episode", "Episodio"),
              ("description", "Descripción")],
    "audio": [("title", "Título"), ("artist", "Artista"), ("album", "Álbum"), ("year", "Año"),
              ("track", "Pista"), ("genre", "Género")],
    "image": [("title", "Título"), ("artist", "Autor"), ("date", "Fecha"), ("description", "Descripción")],
    "document": [("title", "Título"), ("author", "Autor"), ("year", "Año"), ("publisher", "Editorial"),
                 ("genre", "Género"), ("description", "Descripción")],
    "game": [("title", "Título"), ("platform", "Plataforma"), ("year", "Año"), ("developer", "Desarrollador"),
             ("genre", "Género"), ("description", "Descripción")],
}

CREATOR_KEYS = ("artist", "author", "director", "developer")

_EASY_AUDIO = {"title": "title", "artist": "artist", "album": "album", "year": "date",
               "track": "tracknumber", "genre": "genre"}
_AUDIO_WRITABLE = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a"}
_VIDEO_KEYS = {"title": "title", "year": "date", "genre": "genre", "description": "description",
               "director": "director", "series": "show", "season": "season_number", "episode": "episode_sort"}
_VIDEO_WRITABLE = {
    ".mkv": set(_VIDEO_KEYS),
    ".mp4": set(_VIDEO_KEYS) - {"director"},
    ".m4v": set(_VIDEO_KEYS) - {"director"},
    ".mov": set(_VIDEO_KEYS) - {"director"},
}
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class MetadataError(Exception):
    pass


def writable_fields(path: str, kind: str) -> set[str]:
    """Campos que se pueden guardar dentro de este archivo."""
    ext = Path(path).suffix.lower()
    if os.path.isdir(path):
        return set()
    if kind == "audio" and ext in _AUDIO_WRITABLE:
        return set(_EASY_AUDIO)
    if kind == "video":
        return _VIDEO_WRITABLE.get(ext, set())
    if kind == "image" and ext in {".jpg", ".jpeg"}:
        return {"title", "artist", "description"}
    return set()


def pending_edits(item: dict, path: str) -> dict:
    """Ediciones del catálogo que aún pueden escribirse en el archivo."""
    can_write = writable_fields(path, item["kind"])
    return {k: v for k, v in item["edits"].items() if k in can_write}


def clean_name(stem: str) -> str:
    if " " not in stem:
        stem = stem.replace(".", " ").replace("_", " ")
    return stem.strip()


def guess_video(stem: str) -> dict:
    """Deduce título, año, temporada y episodio de nombres habituales."""
    name = clean_name(stem)
    result: dict[str, str] = {}
    episode = re.search(r"\b[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})\b", name)
    if episode:
        series = name[: episode.start()].strip(" -")
        if series:
            result["series"] = series
        result["season"] = str(int(episode.group(1)))
        result["episode"] = str(int(episode.group(2)))
        result["title"] = name
        return result
    year = re.search(r"[\(\[ ]((?:19|20)\d{2})(?=[\)\] ]|$)", name)
    if year and year.start() > 0:
        result["year"] = year.group(1)
        result["title"] = name[: year.start()].strip(" -")
    else:
        result["title"] = name
    return result


def _year(value: str) -> str:
    match = re.search(r"\d{4}", str(value))
    return match.group(0) if match else str(value)


def _duration(seconds) -> str:
    total = int(float(seconds))
    return f"{total // 3600:d}:{total % 3600 // 60:02d}:{total % 60:02d}"


# Lectura

def read_file(path: str, kind: str) -> tuple[dict, dict]:
    """Devuelve (metadatos, datos técnicos). Un archivo ilegible no detiene el escaneo."""
    readers = {"video": _read_video, "audio": _read_audio, "image": _read_image, "document": _read_document}
    fields: dict = {}
    technical: dict = {}
    reader = readers.get(kind)
    if reader and not os.path.isdir(path):
        try:
            fields, technical = reader(path)
        except Exception as exc:
            technical = {"Error de lectura": str(exc)[:200]}
    if kind == "video":
        fields = {**guess_video(Path(path).stem), **fields}
    elif not fields.get("title"):
        fields["title"] = clean_name(Path(path).stem if not os.path.isdir(path) else Path(path).name)
    return {k: v for k, v in fields.items() if v}, technical


def _read_audio(path: str) -> tuple[dict, dict]:
    import mutagen

    audio = mutagen.File(path, easy=True)
    if audio is None:
        return {}, {}
    fields = {}
    for field, key in _EASY_AUDIO.items():
        try:
            values = (audio.tags or {}).get(key)
        except Exception:
            values = None
        if values:
            fields[field] = str(values[0])
    if "year" in fields:
        fields["year"] = _year(fields["year"])
    technical = {}
    info = audio.info
    if getattr(info, "length", None):
        technical["Duración"] = _duration(info.length)
    if getattr(info, "bitrate", None):
        technical["Bitrate"] = f"{info.bitrate // 1000} kbps"
    if getattr(info, "sample_rate", None):
        technical["Muestreo"] = f"{info.sample_rate} Hz"
    return fields, technical


def _read_video(path: str) -> tuple[dict, dict]:
    probe = shutil.which("ffprobe")
    if not probe:
        return {}, {"Aviso": "ffprobe no está instalado"}
    out = subprocess.run(
        [probe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, timeout=120, creationflags=_NO_WINDOW,
    )
    if out.returncode:
        raise MetadataError(out.stderr.decode("utf-8", "replace"))
    data = json.loads(out.stdout.decode("utf-8", "replace"))
    fmt = data.get("format", {})
    tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    fields = {}
    for field, key in _VIDEO_KEYS.items():
        value = tags.get(key)
        if value:
            fields[field] = _year(value) if field == "year" else str(value)
    technical = {}
    if fmt.get("duration"):
        technical["Duración"] = _duration(fmt["duration"])
    if fmt.get("bit_rate"):
        technical["Bitrate"] = f"{int(fmt['bit_rate']) // 1000} kbps"
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and "Resolución" not in technical:
            technical["Resolución"] = f"{stream.get('width')}×{stream.get('height')}"
            technical["Vídeo"] = stream.get("codec_name", "")
        elif stream.get("codec_type") == "audio" and "Audio" not in technical:
            technical["Audio"] = stream.get("codec_name", "")
    return fields, technical


def _read_image(path: str) -> tuple[dict, dict]:
    from PIL import ExifTags, Image

    with Image.open(path) as img:
        technical = {"Resolución": f"{img.width}×{img.height}", "Formato": img.format or ""}
        exif = img.getexif()
        sub = exif.get_ifd(ExifTags.IFD.Exif)
    fields = {}
    if exif.get(315):
        fields["artist"] = _exif_text(exif[315])
    if exif.get(270):
        fields["description"] = _exif_text(exif[270])
    if exif.get(0x9C9B):
        raw = exif[0x9C9B]
        fields["title"] = bytes(raw).decode("utf-16-le", "replace").rstrip("\0")
    date = sub.get(36867) or exif.get(306)
    if date:
        fields["date"] = str(date)
    if exif.get(272):
        technical["Cámara"] = str(exif[272]).strip()
    return fields, technical


def _exif_text(value) -> str:
    # Pillow decodifica el texto EXIF como latin-1; aquí se escribe en UTF-8.
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").rstrip("\0")
    try:
        return value.encode("latin-1").decode("utf-8").rstrip("\0")
    except (UnicodeError, AttributeError):
        return str(value).rstrip("\0")


def _read_document(path: str) -> tuple[dict, dict]:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        info = reader.metadata or {}
        fields = {"title": str(info.title or ""), "author": str(info.author or "")}
        return fields, {"Páginas": str(len(reader.pages))}
    if ext == ".epub":
        return _read_epub(path), {}
    return {}, {}


def _read_epub(path: str) -> dict:
    with zipfile.ZipFile(path) as book:
        container = ET.fromstring(book.read("META-INF/container.xml"))
        rootfile = next(e for e in container.iter() if e.tag.endswith("rootfile"))
        opf = ET.fromstring(book.read(rootfile.attrib["full-path"]))
    wanted = {"title": "title", "creator": "author", "publisher": "publisher", "date": "year",
              "description": "description"}
    fields = {}
    for element in opf.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name in wanted and element.text and wanted[name] not in fields:
            fields[wanted[name]] = element.text.strip()
    if "year" in fields:
        fields["year"] = _year(fields["year"])
    return fields


# Escritura

def write_file(path: str, kind: str, fields: dict) -> None:
    """Escribe los campos en el archivo. Lanza MetadataError si no se pudo."""
    allowed = writable_fields(path, kind)
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        raise MetadataError("Este formato no admite escribir estos campos en el archivo.")
    source = Path(path)
    temp = source.with_name(f"{TEMP_PREFIX}{uuid.uuid4().hex}{source.suffix}")
    try:
        if kind == "video":
            _write_video(path, str(temp), fields)
        else:
            shutil.copy2(path, temp)
            (_write_audio if kind == "audio" else _write_image)(str(temp), fields)
        os.replace(temp, path)
    except MetadataError:
        raise
    except Exception as exc:
        raise MetadataError(str(exc)) from exc
    finally:
        temp.unlink(missing_ok=True)


def _write_audio(temp: str, fields: dict) -> None:
    import mutagen

    audio = mutagen.File(temp, easy=True)
    if audio is None:
        raise MetadataError("No se reconoce el formato de audio.")
    if audio.tags is None:
        audio.add_tags()
    for field, value in fields.items():
        key = _EASY_AUDIO[field]
        if value:
            audio[key] = value
        elif key in audio:
            del audio[key]
    audio.save()


def _write_image(temp: str, fields: dict) -> None:
    import piexif

    exif = piexif.load(temp)
    zeroth = exif["0th"]
    tags = {"artist": piexif.ImageIFD.Artist, "description": piexif.ImageIFD.ImageDescription}
    for field, tag in tags.items():
        if field in fields:
            if fields[field]:
                zeroth[tag] = fields[field].encode("utf-8")
            else:
                zeroth.pop(tag, None)
    if "title" in fields:
        if fields["title"]:
            zeroth[piexif.ImageIFD.XPTitle] = tuple((fields["title"] + "\0").encode("utf-16-le"))
        else:
            zeroth.pop(piexif.ImageIFD.XPTitle, None)
    # insert sustituye solo el bloque EXIF: los píxeles no se recodifican.
    piexif.insert(piexif.dump(exif), temp)


def _write_video(path: str, temp: str, fields: dict) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MetadataError("ffmpeg no está instalado.")
    command = [ffmpeg, "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy"]
    for field, value in fields.items():
        command += ["-metadata", f"{_VIDEO_KEYS[field]}={value}"]
    out = subprocess.run(command + [temp], capture_output=True, creationflags=_NO_WINDOW)
    if out.returncode:
        raise MetadataError(out.stderr.decode("utf-8", "replace")[-500:])
    before = _read_video(path)[1].get("Duración")
    after = _read_video(temp)[1].get("Duración")
    if before != after:
        raise MetadataError(f"La duración cambió ({before} → {after}); no se sustituye el original.")

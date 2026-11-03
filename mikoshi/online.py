"""Búsqueda de datos en internet. Solo se conecta cuando el usuario pide una búsqueda."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "Mikoshi/0.2 (https://github.com/Daijinn-BKR/mikoshi)"
TIMEOUT = 20
MAX_COVER_BYTES = 20 * 1024 * 1024
TMDB_IMAGES = "https://image.tmdb.org/t/p/w500"

# Qué servicio atiende a cada tipo y qué clave necesita.
SOURCES = {
    "video": ("TMDB", "tmdb_key"),
    "audio": ("MusicBrainz", None),
    "document": ("Open Library", None),
    "game": ("RAWG", "rawg_key"),
}


class OnlineError(Exception):
    pass


@dataclass
class Result:
    title: str
    subtitle: str = ""
    fields: dict = field(default_factory=dict)
    cover_url: str = ""
    source: str = ""
    ref: tuple = ()


def _get(url: str, params: dict | None = None, headers: dict | None = None) -> bytes:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read(MAX_COVER_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise OnlineError("El servicio rechazó la clave. Revísala en Archivo > Ajustes.") from exc
        if exc.code == 429 or exc.code == 503:
            raise OnlineError("El servicio pide esperar. Prueba de nuevo en unos segundos.") from exc
        raise OnlineError(f"El servicio respondió con un error ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OnlineError("No se pudo conectar. Revisa tu conexión a internet.") from exc


def _get_json(url: str, params: dict | None = None) -> dict:
    try:
        return json.loads(_get(url, params))
    except ValueError as exc:
        raise OnlineError("El servicio devolvió una respuesta que no se entiende.") from exc


def _year(value) -> str:
    match = re.match(r"\d{4}", str(value or ""))
    return match.group(0) if match else ""


def _clean(fields: dict) -> dict:
    return {k: str(v).strip() for k, v in fields.items() if v not in (None, "")}


def search(kind: str, query: str, keys: dict) -> list[Result]:
    if kind not in SOURCES:
        raise OnlineError("No hay un servicio de búsqueda para este tipo de archivo.")
    source, key_name = SOURCES[kind]
    key = keys.get(key_name, "") if key_name else ""
    if key_name and not key:
        raise OnlineError(f"{source} necesita una clave gratuita. Añádela en Archivo > Ajustes.")
    query = query.strip()
    if not query:
        return []
    return {"video": _search_tmdb, "audio": _search_musicbrainz,
            "document": _search_openlibrary, "game": _search_rawg}[kind](query, key)


def details(result: Result, keys: dict) -> dict:
    """Datos que solo se piden al elegir un resultado, como director o desarrollador."""
    if result.source == "TMDB":
        return {**result.fields, **_tmdb_details(result, keys.get("tmdb_key", ""))}
    if result.source == "RAWG":
        return {**result.fields, **_rawg_details(result, keys.get("rawg_key", ""))}
    return result.fields


def download_cover(url: str) -> bytes:
    if not url.startswith("https://"):
        raise OnlineError("La portada no tiene una dirección segura.")
    data = _get(url)
    if len(data) > MAX_COVER_BYTES:
        raise OnlineError("La portada es demasiado grande.")
    return data


# TMDB: películas y series

def _search_tmdb(query: str, key: str) -> list[Result]:
    data = _get_json("https://api.themoviedb.org/3/search/multi",
                     {"api_key": key, "query": query, "language": "es-ES", "include_adult": "false"})
    results = []
    for row in data.get("results", [])[:15]:
        media = row.get("media_type")
        if media not in ("movie", "tv"):
            continue
        name = row.get("title") or row.get("name") or ""
        year = _year(row.get("release_date") or row.get("first_air_date"))
        fields = {"year": year, "description": row.get("overview"), "category": media.replace("tv", "series")}
        # En una serie, el título del archivo es el del episodio: el nombre va en «Serie».
        fields["title" if media == "movie" else "series"] = name
        poster = row.get("poster_path")
        results.append(Result(
            title=name, subtitle=" · ".join(p for p in (year, "Película" if media == "movie" else "Serie") if p),
            fields=_clean(fields), cover_url=f"{TMDB_IMAGES}{poster}" if poster else "",
            source="TMDB", ref=(media, row.get("id")),
        ))
    return results


def _tmdb_details(result: Result, key: str) -> dict:
    media, item_id = result.ref
    data = _get_json(f"https://api.themoviedb.org/3/{media}/{item_id}",
                     {"api_key": key, "language": "es-ES", "append_to_response": "credits"})
    fields = {"genre": ", ".join(g["name"] for g in data.get("genres", []))}
    crew = data.get("credits", {}).get("crew", [])
    directors = [c["name"] for c in crew if c.get("job") == "Director"]
    if media == "tv":
        directors = [c["name"] for c in data.get("created_by", [])]
    fields["director"] = ", ".join(directors)
    return _clean(fields)


# MusicBrainz y Cover Art Archive: música

def _search_musicbrainz(query: str, _key: str) -> list[Result]:
    safe = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', " ", query)
    data = _get_json("https://musicbrainz.org/ws/2/recording", {"query": safe, "fmt": "json", "limit": 15})
    results = []
    for row in data.get("recordings", []):
        artist = "".join(c.get("name", "") + c.get("joinphrase", "") for c in row.get("artist-credit", []))
        release = (row.get("releases") or [{}])[0]
        year = _year(row.get("first-release-date") or release.get("date"))
        tags = sorted(row.get("tags", []), key=lambda t: -t.get("count", 0))
        release_id = release.get("id")
        results.append(Result(
            title=row.get("title", ""), subtitle=" · ".join(p for p in (artist, release.get("title"), year) if p),
            fields=_clean({"title": row.get("title"), "artist": artist, "album": release.get("title"),
                           "year": year, "genre": tags[0]["name"] if tags else ""}),
            cover_url=f"https://coverartarchive.org/release/{release_id}/front-500" if release_id else "",
            source="MusicBrainz",
        ))
    return results


# Open Library: libros

def _search_openlibrary(query: str, _key: str) -> list[Result]:
    data = _get_json("https://openlibrary.org/search.json",
                     {"q": query, "limit": 15, "fields": "title,author_name,first_publish_year,publisher,cover_i"})
    results = []
    for row in data.get("docs", []):
        author = ", ".join(row.get("author_name", [])[:3])
        year = str(row.get("first_publish_year") or "")
        publisher = (row.get("publisher") or [""])[0]
        cover = row.get("cover_i")
        results.append(Result(
            title=row.get("title", ""), subtitle=" · ".join(p for p in (author, year) if p),
            fields=_clean({"title": row.get("title"), "author": author, "year": year, "publisher": publisher}),
            cover_url=f"https://covers.openlibrary.org/b/id/{cover}-L.jpg" if cover else "",
            source="Open Library",
        ))
    return results


# RAWG: juegos

def _search_rawg(query: str, key: str) -> list[Result]:
    data = _get_json("https://api.rawg.io/api/games", {"key": key, "search": query, "page_size": 15})
    results = []
    for row in data.get("results", []):
        year = _year(row.get("released"))
        platforms = [p["platform"]["name"] for p in row.get("platforms") or []]
        results.append(Result(
            title=row.get("name", ""), subtitle=" · ".join(p for p in (year, ", ".join(platforms[:3])) if p),
            fields=_clean({"title": row.get("name"), "year": year, "platform": ", ".join(platforms[:3]),
                           "genre": ", ".join(g["name"] for g in row.get("genres", []))}),
            cover_url=row.get("background_image") or "", source="RAWG", ref=(row.get("id"),),
        ))
    return results


def steam_app_id(path: str) -> str | None:
    """El número de juego de Steam de un acceso directo .url, si lo es."""
    if not path.lower().endswith(".url"):
        return None
    try:
        text = open(path, encoding="utf-8", errors="replace").read(4096)
    except OSError:
        return None
    match = re.search(r"steam://rungameid/(\d+)", text)
    return match.group(1) if match else None


def steam_cover(app_id: str) -> bytes:
    """Portada vertical de la biblioteca de Steam. No necesita clave."""
    if not app_id.isdigit():
        raise OnlineError("Número de juego de Steam no válido.")
    base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}"
    try:
        return download_cover(f"{base}/library_600x900.jpg")
    except OnlineError:
        return download_cover(f"{base}/header.jpg")


def _rawg_details(result: Result, key: str) -> dict:
    data = _get_json(f"https://api.rawg.io/api/games/{result.ref[0]}", {"key": key})
    return _clean({"developer": ", ".join(d["name"] for d in data.get("developers", [])),
                   "description": data.get("description_raw")})

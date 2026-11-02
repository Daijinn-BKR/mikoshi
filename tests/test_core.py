import os
import shutil
import subprocess

import pytest
from PIL import Image

from catalogo import metadata, scanner
from catalogo.db import Catalog, effective

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requiere ffmpeg")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture
def catalog(tmp_path):
    cat = Catalog(tmp_path / "test.db")
    yield cat
    cat.close()


@pytest.fixture
def lib(tmp_path):
    path = tmp_path / "lib"
    path.mkdir()
    return path


def test_scan_classifies_and_ignores_unknown(catalog, lib):
    Image.new("RGB", (10, 10)).save(lib / "foto.jpg")
    (lib / "juego.gba").write_bytes(b"x")
    (lib / "notas.xyz").write_bytes(b"x")
    (lib / (metadata.TEMP_PREFIX + "a.jpg")).write_bytes(b"x")
    result = scanner.scan_root(catalog, catalog.add_root(str(lib)))
    assert result.added == 2
    assert {i["kind"] for i in catalog.query_items()} == {"image", "game"}


def test_offline_root_keeps_items(catalog, lib):
    (lib / "juego.gba").write_bytes(b"x")
    root_id = catalog.add_root(str(lib))
    scanner.scan_root(catalog, root_id)
    moved_away = lib.with_name("apagado")
    lib.rename(moved_away)
    with pytest.raises(scanner.RootOffline):
        scanner.scan_root(catalog, root_id)
    assert catalog.query_items()[0]["status"] == "ok"


def test_read_errors_do_not_mark_missing(catalog, lib, monkeypatch):
    (lib / "juego.gba").write_bytes(b"x")
    root_id = catalog.add_root(str(lib))
    scanner.scan_root(catalog, root_id)

    def broken(root, errors):
        errors.append("sin permiso")
        return iter(())

    monkeypatch.setattr(scanner, "_iter_auto", broken)
    scanner.scan_root(catalog, root_id)
    assert catalog.query_items()[0]["status"] == "ok"


def test_missing_and_restored(catalog, lib):
    game = lib / "juego.gba"
    game.write_bytes(b"x")
    root_id = catalog.add_root(str(lib))
    scanner.scan_root(catalog, root_id)
    game.rename(lib / "temporal.bin")
    assert scanner.scan_root(catalog, root_id).missing == 1
    assert catalog.query_items()[0]["status"] == "missing"
    (lib / "temporal.bin").rename(game)
    assert scanner.scan_root(catalog, root_id).restored == 1


def test_moved_file_keeps_user_data(catalog, lib):
    (lib / "juego.gba").write_bytes(b"x")
    root_id = catalog.add_root(str(lib))
    scanner.scan_root(catalog, root_id)
    item = catalog.query_items()[0]
    catalog.save_user_data(item["id"], rating=5, edits={"platform": "GBA"})
    catalog.set_tags(item["id"], ["retro"])
    (lib / "sub").mkdir()
    (lib / "juego.gba").rename(lib / "sub" / "juego.gba")
    assert scanner.scan_root(catalog, root_id).moved == 1
    item = catalog.query_items()[0]
    assert item["rel_path"] == os.path.join("sub", "juego.gba")
    assert (item["rating"], item["tags"], item["edits"]["platform"]) == (5, "retro", "GBA")


def test_games_root_treats_folder_as_one_item(catalog, lib):
    game = lib / "Mi Juego"
    game.mkdir()
    (game / "juego.exe").write_bytes(b"12345")
    (game / "datos.bin").write_bytes(b"123")
    (lib / "Otro.iso").write_bytes(b"1")
    (lib / "leeme.txt").write_bytes(b"1")
    scanner.scan_root(catalog, catalog.add_root(str(lib), "games"))
    items = {i["rel_path"]: i for i in catalog.query_items(kind="game")}
    assert set(items) == {"Mi Juego", "Otro.iso"}
    assert items["Mi Juego"]["size"] == 8


def test_relocate_keeps_data(catalog, lib, tmp_path):
    (lib / "juego.gba").write_bytes(b"x")
    root_id = catalog.add_root(str(lib))
    scanner.scan_root(catalog, root_id)
    catalog.save_user_data(catalog.query_items()[0]["id"], rating=4)
    new = tmp_path / "servidor"
    lib.rename(new)
    catalog.relocate_root(root_id, str(new))
    result = scanner.scan_root(catalog, root_id)
    assert (result.added, result.missing) == (0, 0)
    assert catalog.query_items()[0]["rating"] == 4


def test_search_and_tags(catalog, lib):
    (lib / "zelda.gba").write_bytes(b"x")
    (lib / "mario.gba").write_bytes(b"x")
    scanner.scan_root(catalog, catalog.add_root(str(lib)))
    zelda = catalog.query_items(text="zelda")[0]
    catalog.add_tag([zelda["id"]], "favoritos")
    assert len(catalog.query_items()) == 2
    assert [i["id"] for i in catalog.query_items(tag="favoritos")] == [zelda["id"]]
    assert [i["id"] for i in catalog.query_items(text="favoritos")] == [zelda["id"]]


def test_backup_and_csv(catalog, lib, tmp_path):
    (lib / "ñandú.gba").write_bytes(b"x")
    scanner.scan_root(catalog, catalog.add_root(str(lib)))
    dest = tmp_path / "copia"
    dest.mkdir()
    copy = Catalog(catalog.backup_to(str(dest)))
    assert len(copy.query_items()) == 1
    copy.close()
    csv_path = tmp_path / "a.csv"
    assert catalog.export_csv(str(csv_path)) == 1
    assert "ñandú" in csv_path.read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("stem,expected", [
    ("Blade.Runner.1982.1080p.BluRay", {"title": "Blade Runner", "year": "1982"}),
    ("El Padrino (1972)", {"title": "El Padrino", "year": "1972"}),
    ("Dark.S02E05.720p", {"series": "Dark", "season": "2", "episode": "5"}),
    ("2001", {"title": "2001"}),
])
def test_guess_video(stem, expected):
    guess = metadata.guess_video(stem)
    assert {k: guess[k] for k in expected} == expected


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".mp3", ".flac", ".ogg", ".m4a", ".opus"])
def test_audio_round_trip(lib, ext):
    path = lib / f"a{ext}"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=1", str(path))
    fields = {"title": "Canción", "artist": "Ñu", "album": "Año", "year": "1999", "track": "3", "genre": "Rock"}
    metadata.write_file(str(path), "audio", fields)
    read, technical = metadata.read_file(str(path), "audio")
    assert {k: read.get(k) for k in fields} == fields
    assert "Duración" in technical or ext == ".opus"
    metadata.write_file(str(path), "audio", {"genre": ""})
    assert "genre" not in metadata.read_file(str(path), "audio")[0]
    assert [p.name for p in lib.iterdir()] == [path.name]


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".mp4", ".mkv"])
def test_video_round_trip(lib, ext):
    path = lib / f"v{ext}"
    ffmpeg("-f", "lavfi", "-i", "testsrc=duration=1:size=64x64", str(path))
    fields = {"title": "Película", "year": "2001", "genre": "Drama", "description": "Descripción",
              "series": "Serie", "season": "1", "episode": "2"}
    metadata.write_file(str(path), "video", fields)
    read, technical = metadata.read_file(str(path), "video")
    assert {k: read.get(k) for k in fields} == fields
    assert technical["Resolución"] == "64×64"
    assert [p.name for p in lib.iterdir()] == [path.name]


def test_jpeg_write_keeps_pixels(lib):
    path = lib / "f.jpg"
    Image.effect_noise((64, 64), 50).convert("RGB").save(path, quality=90)
    with Image.open(path) as img:
        before = img.tobytes()
    metadata.write_file(str(path), "image", {"title": "Título", "artist": "Peña", "description": "Año"})
    read, _ = metadata.read_file(str(path), "image")
    assert (read["title"], read["artist"], read["description"]) == ("Título", "Peña", "Año")
    with Image.open(path) as img:
        assert img.tobytes() == before


def test_failed_write_leaves_original(lib):
    path = lib / "roto.mp3"
    path.write_bytes(b"esto no es audio")
    with pytest.raises(metadata.MetadataError):
        metadata.write_file(str(path), "audio", {"title": "x"})
    assert path.read_bytes() == b"esto no es audio"
    assert [p.name for p in lib.iterdir()] == ["roto.mp3"]


def test_unwritable_format_rejected(lib):
    path = lib / "a.pdf"
    path.write_bytes(b"x")
    with pytest.raises(metadata.MetadataError):
        metadata.write_file(str(path), "document", {"title": "x"})


def test_effective_overlays_edits():
    item = {"file_meta": {"title": "a", "year": "1"}, "edits": {"title": "b"}}
    assert effective(item) == {"title": "b", "year": "1"}

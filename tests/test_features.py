import sqlite3

import pytest
from PIL import Image

from mikoshi import maintenance, online, scanner
from mikoshi.db import Catalog, effective


@pytest.fixture
def catalog(tmp_path):
    cat = Catalog(tmp_path / "t.db")
    yield cat
    cat.close()


@pytest.fixture
def lib(tmp_path):
    path = tmp_path / "lib"
    path.mkdir()
    return path


def scan(catalog, lib):
    return catalog.add_root(str(lib)), scanner.scan_root(catalog, catalog.add_root(str(lib)))


def test_old_database_is_upgraded_keeping_data(tmp_path):
    path = tmp_path / "vieja.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE roots (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, mode TEXT NOT NULL DEFAULT 'auto');
        CREATE TABLE items (id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL, rel_path TEXT NOT NULL,
            kind TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok', added_at REAL NOT NULL, rating INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '', cover TEXT NOT NULL DEFAULT '', file_meta TEXT NOT NULL DEFAULT '{}',
            edits TEXT NOT NULL DEFAULT '{}', technical TEXT NOT NULL DEFAULT '{}', UNIQUE (root_id, rel_path));
        INSERT INTO roots VALUES (1, 'C:\\x', 'auto');
        INSERT INTO items (root_id, rel_path, kind, added_at, rating) VALUES (1, 'a.gba', 'game', 0, 4);
    """)
    conn.commit()
    conn.close()
    cat = Catalog(path)
    item = cat.query_items()[0]
    assert (item["rating"], item["checksum"]) == (4, "")
    cat.close()
    Catalog(path).close()


def test_settings_round_trip(catalog):
    assert catalog.setting("x", "d") == "d"
    catalog.set_setting("x", "1")
    catalog.set_setting("x", "2")
    assert catalog.setting("x") == "2"


def test_bulk_edit_keeps_other_fields_and_clears_equal_values(catalog, lib):
    (lib / "a.gba").write_bytes(b"1")
    (lib / "b.gba").write_bytes(b"2")
    scan(catalog, lib)
    a, b = catalog.query_items()
    catalog.save_user_data(a["id"], edits={"platform": "GBA"})
    catalog.bulk_edit([a["id"], b["id"]], {"genre": "RPG", "title": a["file_meta"]["title"]})
    a, b = catalog.get_item(a["id"]), catalog.get_item(b["id"])
    assert a["edits"] == {"platform": "GBA", "genre": "RPG"}
    assert effective(b)["title"] == a["file_meta"]["title"]


def test_collections(catalog, lib):
    (lib / "a.gba").write_bytes(b"1")
    (lib / "b.gba").write_bytes(b"2")
    scan(catalog, lib)
    a, b = catalog.query_items()
    cid = catalog.create_collection("Favoritos")
    assert catalog.create_collection("favoritos") == cid
    catalog.add_to_collection(cid, [a["id"]])
    assert [i["id"] for i in catalog.query_items(collection=cid)] == [a["id"]]
    catalog.remove_from_collection(cid, [a["id"]])
    assert catalog.query_items(collection=cid) == []
    catalog.delete_collection(cid)
    assert len(catalog.query_items()) == 2


def test_old_lists_are_upgraded_keeping_items(tmp_path):
    path = tmp_path / "v02.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE collections (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
        CREATE TABLE collection_items (collection_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
            PRIMARY KEY (collection_id, item_id));
        INSERT INTO collections VALUES (1, 'Favoritos');
        INSERT INTO collection_items VALUES (1, 7);
    """)
    conn.commit()
    conn.close()
    cat = Catalog(path)
    info = cat.collection(1)
    assert (info["kind"], info["folder_id"]) == ("collection", None)
    assert cat.list_order(1) == [7]
    cat.close()


def test_playlist_keeps_order_and_moves(catalog, lib):
    for name in "abc":
        (lib / f"{name}.gba").write_bytes(name.encode())
    scan(catalog, lib)
    ids = {i["rel_path"][0]: i["id"] for i in catalog.query_items()}
    pid = catalog.create_collection("Mix", "playlist")
    catalog.add_to_collection(pid, [ids["c"], ids["a"]])
    catalog.add_to_collection(pid, [ids["b"], ids["a"]])
    assert catalog.list_order(pid) == [ids["c"], ids["a"], ids["b"]]
    catalog.move_in_list(pid, ids["b"], -1)
    assert catalog.list_order(pid) == [ids["c"], ids["b"], ids["a"]]
    catalog.move_in_list(pid, ids["c"], -1)
    assert catalog.list_order(pid)[0] == ids["c"]


def test_deleting_folder_keeps_contents_one_level_up(catalog):
    top = catalog.create_folder("Música")
    inner = catalog.create_folder("Rock", top)
    deep = catalog.create_folder("80s", inner)
    pid = catalog.create_collection("Mix", "playlist", inner)
    catalog.delete_folder(inner)
    assert catalog.collection(pid)["folder_id"] == top
    assert next(f for f in catalog.list_folders() if f["id"] == deep)["parent_id"] == top


def test_folder_cannot_move_inside_itself(catalog):
    top = catalog.create_folder("A")
    inner = catalog.create_folder("B", top)
    with pytest.raises(ValueError):
        catalog.move_folder(top, inner)
    with pytest.raises(ValueError):
        catalog.move_folder(top, top)
    catalog.move_folder(inner, None)
    assert next(f for f in catalog.list_folders() if f["id"] == inner)["parent_id"] is None


def test_rename_tag_merges_and_delete_keeps_items(catalog, lib):
    (lib / "a.gba").write_bytes(b"1")
    (lib / "b.gba").write_bytes(b"2")
    scan(catalog, lib)
    a, b = catalog.query_items()
    catalog.add_tag([a["id"], b["id"]], "rpg")
    catalog.add_tag([a["id"]], "RPG clásico")
    catalog.rename_tag("RPG clásico", "rpg")
    assert catalog.all_tags() == ["rpg"]
    assert len(catalog.query_items(tag="rpg")) == 2
    catalog.rename_tag("rpg", "Rol")
    assert catalog.all_tags() == ["Rol"]
    catalog.delete_tag("Rol")
    assert catalog.all_tags() == [] and len(catalog.query_items()) == 2


def test_group_names_respect_edits(catalog, lib):
    (lib / "Dark.S01E02.gba").write_bytes(b"1")
    (lib / "c.gba").write_bytes(b"2")
    scan(catalog, lib)
    a, b = catalog.query_items()
    catalog.save_user_data(a["id"], edits={"series": "Dark"})
    catalog.save_user_data(b["id"], edits={"series": ""})
    assert catalog.group_names("series") == ["Dark"]
    assert [i["id"] for i in catalog.query_items(group=("series", "Dark"))] == [a["id"]]
    with pytest.raises(ValueError):
        catalog.group_names("x'; DROP TABLE items; --")


def test_backup_failure_keeps_previous_copy(catalog, lib, tmp_path, monkeypatch):
    (lib / "a.gba").write_bytes(b"1")
    scan(catalog, lib)
    dest = tmp_path / "copia"
    dest.mkdir()
    catalog.backup_to(str(dest))
    previous = (dest / "catalogo.db").read_bytes()

    def fail(*_a):
        raise OSError("red caída")

    monkeypatch.setattr("os.replace", fail)
    with pytest.raises(OSError):
        catalog.backup_to(str(dest))
    assert (dest / "catalogo.db").read_bytes() == previous


def test_integrity_detects_silent_damage_but_not_edits(catalog, lib):
    import os

    path = lib / "a.gba"
    path.write_bytes(b"contenido")
    root_id = catalog.add_root(str(lib))
    scanner.scan_root(catalog, root_id)
    assert maintenance.verify_integrity(catalog).baseline == 1
    assert maintenance.verify_integrity(catalog).ok == 1

    stat = os.stat(path)
    path.write_bytes(b"contenidX")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    result = maintenance.verify_integrity(catalog)
    assert result.corrupted == [str(path)] and result.ok == 0
    assert maintenance.verify_integrity(catalog).corrupted == [str(path)]

    path.write_bytes(b"editado de verdad")
    scanner.scan_root(catalog, root_id)
    result = maintenance.verify_integrity(catalog)
    assert result.corrupted == [] and result.baseline == 1


def test_integrity_reports_unreadable_and_skips_game_folders(catalog, lib):
    (lib / "a.gba").write_bytes(b"1")
    root_id = catalog.add_root(str(lib))
    scanner.scan_root(catalog, root_id)
    (lib / "a.gba").unlink()
    assert len(maintenance.verify_integrity(catalog).unreadable) == 1


def test_integrity_can_be_cancelled(catalog, lib):
    (lib / "a.gba").write_bytes(b"1")
    scan(catalog, lib)
    result = maintenance.verify_integrity(catalog, cancelled=lambda: True)
    assert result.cancelled and catalog.query_items()[0]["checksum"] == ""


def test_duplicates_need_same_content_and_touch_nothing(catalog, lib):
    (lib / "a.gba").write_bytes(b"igual")
    (lib / "copia.gba").write_bytes(b"igual")
    (lib / "otro.gba").write_bytes(b"disti")
    (lib / "solo.gba").write_bytes(b"tamano unico")
    scan(catalog, lib)
    result = maintenance.find_duplicates(catalog)
    assert result.groups == [[str(lib / "a.gba"), str(lib / "copia.gba")]]
    assert result.wasted == 5
    assert (lib / "copia.gba").read_bytes() == b"igual"


def test_search_requires_key_where_needed():
    with pytest.raises(online.OnlineError, match="clave"):
        online.search("video", "x", {})
    with pytest.raises(online.OnlineError, match="clave"):
        online.search("game", "x", {"tmdb_key": "a"})
    with pytest.raises(online.OnlineError):
        online.search("image", "x", {})


def test_tmdb_parsing_separates_movie_and_series(monkeypatch):
    def fake(url, params=None):
        if "search" in url:
            assert params["api_key"] == "k"
            return {"results": [
                {"media_type": "movie", "id": 1, "title": "Blade Runner", "release_date": "1982-06-25",
                 "overview": "Texto", "poster_path": "/p.jpg"},
                {"media_type": "tv", "id": 2, "name": "Dark", "first_air_date": "2017-12-01"},
                {"media_type": "person", "id": 3, "name": "Alguien"},
            ]}
        return {"genres": [{"name": "Drama"}], "credits": {"crew": [{"job": "Director", "name": "Ridley Scott"}]}}

    monkeypatch.setattr(online, "_get_json", fake)
    movie, show = online.search("video", "x", {"tmdb_key": "k"})
    assert movie.fields["title"] == "Blade Runner" and movie.cover_url.endswith("/p.jpg")
    assert show.fields["series"] == "Dark" and "title" not in show.fields
    full = online.details(movie, {"tmdb_key": "k"})
    assert (full["director"], full["genre"], full["year"]) == ("Ridley Scott", "Drama", "1982")


def test_rawg_parsing(monkeypatch):
    def fake(url, params=None):
        if url.endswith("/games"):
            return {"results": [{"id": 9, "name": "Hades", "released": "2020-09-17", "background_image": "https://x/a.jpg",
                                 "platforms": [{"platform": {"name": "PC"}}], "genres": [{"name": "Action"}]}]}
        return {"developers": [{"name": "Supergiant"}], "description_raw": "Texto"}

    monkeypatch.setattr(online, "_get_json", fake)
    game = online.search("game", "x", {"rawg_key": "k"})[0]
    assert (game.fields["platform"], game.fields["genre"]) == ("PC", "Action")
    assert online.details(game, {"rawg_key": "k"})["developer"] == "Supergiant"


def test_cover_rejects_insecure_url():
    with pytest.raises(online.OnlineError):
        online.download_cover("http://example.com/a.jpg")
    with pytest.raises(online.OnlineError):
        online.download_cover("file:///C:/secreto.jpg")


def test_http_key_error_message_does_not_expose_key(monkeypatch):
    import urllib.error

    def fail(*_a, **_k):
        raise urllib.error.HTTPError("https://x/?api_key=SECRETA", 401, "no", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(online.OnlineError) as info:
        online._get("https://x/", {"api_key": "SECRETA"})
    assert "SECRETA" not in str(info.value)

import os
import subprocess

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PIL import Image
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from mikoshi import dialogs, metadata, online, scanner
from mikoshi.db import Catalog, effective


def wait_until(condition, ms=20000):
    loop = QEventLoop()
    timer = QTimer(interval=20)
    timer.timeout.connect(lambda: condition() and loop.quit())
    timer.start()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    timer.stop()
    assert condition()


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def song(path, title, artist, album, track, seconds=1):
    ffmpeg("-f", "lavfi", "-i", f"sine=duration={seconds}", str(path))
    metadata.write_file(str(path), "audio", {"title": title, "artist": artist, "album": album,
                                             "track": str(track), "year": "2001"})


def video(path, seconds=2):
    ffmpeg("-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=64x36", str(path))


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_modal_popups(monkeypatch):
    monkeypatch.setattr(dialogs.ReportDialog, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    base = tmp_path_factory.mktemp("biblioteca")
    music, cinema, favorites, books, games = (base / n for n in ("musica", "cine", "favoritos", "libros", "juegos"))
    for folder in (music, cinema, favorites, books, games):
        folder.mkdir()
    song(music / "b.mp3", "Segunda", "Zeta", "Álbum A", 2)
    song(music / "a.mp3", "Primera", "Zeta", "Álbum A", 1)
    song(music / "c.mp3", "Única", "Alfa", "Álbum B", 1)
    video(cinema / "Blade.Runner.1982.mp4")
    for name in ("Dark.S01E02", "Dark.S01E01", "Dark.S02E01"):
        video(cinema / f"{name}.mp4")
    video(favorites / "clip uno.mp4")
    video(favorites / "clip dos.mp4", 3)
    (books / "dune.epub").write_bytes(b"no es un epub real")
    (games / "Hades.url").write_text("[InternetShortcut]\nURL=steam://rungameid/1145360\n")
    (games / "Celeste.lnk").write_bytes(b"x")
    return base


@pytest.fixture
def window(app, library, tmp_path):
    from mikoshi.ui import MainWindow

    win = MainWindow(tmp_path / "s.db")
    for name in ("musica", "cine", "libros"):
        win.catalog.add_root(str(library / name))
    win.catalog.add_root(str(library / "favoritos"))
    win.catalog.add_root(str(library / "juegos"), "games")
    win.scan_all()
    wait_until(lambda: win.worker is None, 60000)
    win.show()
    yield win
    win.close()


def section(win, title):
    found = next(s for s in win.sections if s.title == title)
    win.tabs.setCurrentWidget(found)
    return found


def titles(items):
    from mikoshi.db import display_title
    return [display_title(i) for i in items]


def test_tabs_exist_in_order_and_remember_last(window, tmp_path):
    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert names == ["Películas y series", "Música", "Videos favoritos", "Libros", "Juegos", "Todo"]
    section(window, "Libros")
    window.close()
    assert window.catalog.path and Catalog(window.catalog.path).setting("tab") == "Libros"


def test_music_sorts_by_album_and_track(window):
    music = section(window, "Música")
    music.sort.setCurrentText("Álbum")
    assert titles(music.model.items) == ["Primera", "Segunda", "Única"]
    music.sort.setCurrentText("Artista")
    assert titles(music.model.items)[0] == "Única"


def test_music_browse_by_album_filters(window):
    music = section(window, "Música")
    music.browse_mode.setCurrentIndex(1)
    entry = next(music.browser.item(i) for i in range(music.browser.count())
                 if music.browser.item(i).text().startswith("Álbum B"))
    music.browser.setCurrentItem(entry)
    assert titles(music.model.items) == ["Única"]
    assert "Alfa" in entry.text()


def test_album_list_replaces_placeholder_when_cover_arrives(window, tmp_path):
    cover = tmp_path / "portada.jpg"
    Image.new("RGB", (64, 64), "red").save(cover)
    track = tmp_path / "con_portada.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-i", str(cover), "-map", "0", "-map", "1", "-c:v", "mjpeg",
           "-id3v2_version", "3", "-disposition:v", "attached_pic", str(track))
    root = next(r for r in window.catalog.roots() if r["path"].endswith("musica"))
    target = os.path.join(root["path"], "con_portada.mp3")
    __import__("shutil").copy(track, target)
    metadata.write_file(target, "audio", {"album": "Rojo", "artist": "Nadie", "title": "Con portada"})
    scanner.scan_root(window.catalog, root["id"])
    window.thumbs.pixmaps.clear()
    window.thumbs.requested.clear()
    music = section(window, "Música")
    music.browse_mode.setCurrentIndex(1)
    entry = next(music.browser.item(i) for i in range(music.browser.count())
                 if music.browser.item(i).text().startswith("Rojo"))
    placeholder_key = window.thumbs.placeholders["audio"].cacheKey()
    item = next(t for t in music.tracks if t["rel_path"] == "con_portada.mp3")
    wait_until(lambda: window.thumbs.has(item))
    pixmap = entry.icon().pixmap(40, 40).toImage()
    red = pixmap.pixelColor(20, 20)
    assert red.red() > 200 and red.green() < 60, (red.name(), placeholder_key)
    os.remove(target)
    scanner.scan_root(window.catalog, root["id"])


def test_icon_binder_forgets_cleared_lists(window):
    music = section(window, "Música")
    music.browse_mode.setCurrentIndex(1)
    music.browse_mode.setCurrentIndex(0)
    music.browse_mode.setCurrentIndex(1)
    bound = sum(1 for entries in music.icons.entries.values() for e in entries if e[0] is music.browser)
    assert bound == music.browser.count() - 1
    for item_id in list(music.icons.entries):
        music.icons._updated(item_id)


def test_music_plays_in_bar_with_queue_controls(window):
    music = section(window, "Música")
    music.sort.setCurrentText("Álbum")
    window.play_items(music.model.items, 0)
    bar = window.now_playing
    assert bar.isVisible() and titles([bar.current]) == ["Primera"]
    wait_until(lambda: bar.player.duration() > 0)
    assert titles(bar.upcoming()) == ["Segunda", "Única"]
    bar.play_next([music.model.items[2]])
    assert titles(bar.upcoming())[0] == "Única"
    bar.next(manual=True)
    assert titles([bar.current]) == ["Única"]
    assert window.catalog.activity()[bar.current["id"]]["play_count"] == 1
    bar.set_shuffle(True)
    assert bar.current["id"] == bar.queue[bar.order[bar.position_in_order]]["id"]
    assert sorted(bar.order) == list(range(len(bar.queue)))


def test_bar_advances_at_end_and_stops_without_repeat(window):
    bar = window.now_playing
    tracks = section(window, "Música").model.items
    bar.play(tracks, len(tracks) - 1)
    bar.next()
    assert bar.position_in_order == len(tracks) - 1
    bar._cycle_repeat()
    assert bar.repeat == "all"
    bar.next()
    assert bar.position_in_order == 0


def test_bar_skips_missing_tracks(window, tmp_path):
    bar = window.now_playing
    tracks = [dict(t) for t in section(window, "Música").model.items]
    tracks[0]["rel_path"] = "no-existe.mp3"
    bar.play(tracks, 0)
    assert bar.position_in_order == 1


def test_cinema_separates_movies_series_and_favorites(window):
    cinema = section(window, "Películas y series")
    root = next(r for r in window.catalog.roots() if r["path"].endswith("favoritos"))
    window.catalog.set_root_mode(root["id"], "auto")
    cinema.refresh()
    # Sin carpeta de favoritos asignada, esos clips cuentan como películas.
    assert sorted(titles(cinema.movie_model.items)) == ["Blade Runner", "clip dos", "clip uno"]
    window.catalog.set_root_mode(root["id"], "favorites")
    cinema.refresh()
    assert titles(cinema.movie_model.items) == ["Blade Runner"]
    assert [s["name"] for s in cinema.series_model.items] == ["Dark"]
    episodes = cinema.series["Dark"]
    assert [cinema.episode_key(e)[:2] for e in episodes] == [(1, 1), (1, 2), (2, 1)]


def test_manual_category_overrides_detection(window):
    cinema = section(window, "Películas y series")
    movie = next(m for m in cinema.movie_model.items if "Blade" in m["rel_path"])
    window.catalog.save_user_data(movie["id"], edits={"category": "series", "series": "Replicantes"})
    cinema.refresh()
    assert cinema.movie_model.items == [] or "Blade" not in titles(cinema.movie_model.items)[0]
    assert "Replicantes" in cinema.series


def test_series_page_next_episode_and_watched(window):
    cinema = section(window, "Películas y series")
    cinema.show_series("Dark")
    assert cinema.season.count() == 2
    assert [cinema.episode_key(e)[1] for e in cinema.season_episodes()] == [1, 2]
    first = cinema.next_episode()
    assert cinema.episode_key(first)[:2] == (1, 1)
    window.catalog.set_watched(first["id"], True)
    cinema.refresh()
    assert cinema.episode_key(cinema.next_episode())[:2] == (1, 2)
    assert "T1E2" in cinema.next_up.text()
    cinema._mark_season()
    assert cinema.episode_key(cinema.next_episode())[:2] == (2, 1)


def test_resume_progress_appears_in_continue_watching(window):
    cinema = section(window, "Películas y series")
    movie = next(m for m in cinema.movie_model.items if "Blade" in m["rel_path"])
    window.catalog.save_progress(movie["id"], 30_000, 100_000)
    cinema.refresh()
    assert titles(cinema.continue_model.items) == ["Blade Runner"]
    assert cinema.continue_view.isVisibleTo(cinema)
    cinema.show_movie(movie)
    assert cinema.movie_play.text().startswith("Continuar desde 0:30")
    window.catalog.save_progress(movie["id"], 95_000, 100_000)
    cinema.refresh()
    assert cinema.continue_model.items == []
    assert window.catalog.activity()[movie["id"]]["watched"] == 1


def test_player_resumes_and_saves_position(window):
    from mikoshi.player import PlayerHooks, PlayerWidget

    movie = next(m for m in section(window, "Películas y series").movie_model.items if "Blade" in m["rel_path"])
    window.catalog.save_progress(movie["id"], 1000, 2000)
    widget = PlayerWidget([movie], 0, lambda p: None, PlayerHooks(window.catalog, resume=True))
    widget.show()
    wait_until(lambda: widget.player.position() >= 900)
    widget.player.pause()
    widget.stop()
    saved = window.catalog.activity()[movie["id"]]
    assert saved["position_ms"] >= 900 or saved["watched"]


def test_folder_named_favorites_is_adopted_automatically(window):
    favorites = section(window, "Videos favoritos")
    assert favorites.pages.currentIndex() == 0
    root = next(r for r in window.catalog.roots() if r["path"].endswith("favoritos"))
    assert window.catalog.root(root["id"])["mode"] == "favorites"


def test_favorites_setup_when_no_folder_matches(window):
    root = next(r for r in window.catalog.roots() if r["path"].endswith("favoritos"))
    window.catalog.relocate_root(root["id"], root["path"].replace("favoritos", "clips"))
    favorites = section(window, "Videos favoritos")
    assert favorites.pages.currentWidget() is favorites.setup_page
    assert favorites.suggestions.count() >= 1


def test_favorites_youtube_grid_and_watch(window):
    favorites = section(window, "Videos favoritos")
    assert favorites.pages.currentIndex() == 0
    assert sorted(titles(favorites.model.items)) == ["clip dos", "clip uno"]
    favorites._set_sort("Duración")
    assert titles(favorites.model.items)[0] == "clip dos"
    assert favorites.model.badge(favorites.model.items[0]) == "0:03"
    favorites.watch(favorites.model.items, 0)
    assert favorites.pages.currentWidget() is favorites.watch_page
    assert favorites.watch_title.text() == "clip dos" and favorites.up_next.count() == 1
    # Abrir un vídeo todavía no cuenta como reproducción.
    assert favorites.watch_meta.text().startswith("0 reproducciones")
    wait_until(lambda: favorites.player.player.duration() > 0)
    favorites._back()
    cinema = section(window, "Películas y series")
    assert "clip" not in " ".join(titles(cinema.movie_model.items))


def test_root_menu_assigns_favorites(window):
    root = next(r for r in window.catalog.roots() if r["path"].endswith("favoritos"))
    window._set_root_mode(root["id"], "auto")
    labels = [a[0] for a in window._sidebar_actions(("root", root["id"])) if a]
    assert "Usar como Videos favoritos" in labels
    window._set_root_mode(root["id"], "favorites")
    labels = [a[0] for a in window._sidebar_actions(("root", root["id"])) if a]
    assert "Dejar de usar como Videos favoritos" in labels


def test_books_calibre_browser_and_details(window):
    books = section(window, "Libros")
    item = books.books[0]
    window.catalog.save_user_data(item["id"], edits={"author": "Frank Herbert", "saga": "Dune", "genre": "Ciencia ficción"})
    books.refresh()
    labels = [books.browser.topLevelItem(i).text(0) for i in range(books.browser.topLevelItemCount())]
    assert labels[0].startswith("Todos los libros") and "Autores" in labels and "Series" in labels
    books.table.selectRow(0)
    assert books.book_author.text() == "Frank Herbert" and "Serie: Dune" in books.book_meta.text()
    opened = []
    monkey_open = lambda path: opened.append(path)
    import mikoshi.sections as sections
    original, sections.open_path = sections.open_path, monkey_open
    try:
        books._read()
    finally:
        sections.open_path = original
    assert opened and window.catalog.activity()[item["id"]]["play_count"] == 1


def test_games_steam_library_launch_and_cover(window, monkeypatch):
    games = section(window, "Juegos")
    assert sorted(titles(games.grid_model.items)) == ["Celeste", "Hades"]
    hades = next(g for g in games.games if "Hades" in g["rel_path"])
    assert online.steam_app_id(os.path.join(hades["root_path"], hades["rel_path"])) == "1145360"
    games.show_game(hades)
    assert games.pages.currentIndex() == 1 and games.steam_button.isVisibleTo(games)
    launched = []
    monkeypatch.setattr("mikoshi.sections.open_path", lambda path: launched.append(path))
    games.launch(hades)
    assert launched and "1 veces" in games.stats.text()
    games.sort.setCurrentText("Jugados recientemente")
    assert titles(games.grid_model.items)[0] == "Hades"

    jpeg = __import__("io").BytesIO()
    Image.new("RGB", (6, 9), "red").save(jpeg, "JPEG")
    monkeypatch.setattr(online, "steam_cover", lambda app_id: jpeg.getvalue())
    games.fetch_steam([hades])
    wait_until(lambda: window.catalog.get_item(hades["id"])["cover"] != "")
    dialogs.wait_for_tasks()


def test_edit_dialog_saves_category_from_section(window, monkeypatch):
    from PySide6.QtWidgets import QComboBox, QDialog

    movie = next(m for m in section(window, "Películas y series").movie_model.items if "Blade" in m["rel_path"])

    def fake_exec(dialog):
        panel = window.editor
        combo = panel.inputs["category"]
        assert isinstance(combo, QComboBox)
        combo.setCurrentIndex(combo.findData("series"))
        panel.inputs["series"].setText("Blade")
        panel._mark_dirty()
        dialog.done(0)
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window.edit_item(movie["id"])
    assert effective(window.catalog.get_item(movie["id"]))["category"] == "series"


def test_write_report_lists_values_read_back(window, monkeypatch):
    shown = []
    monkeypatch.setattr(dialogs.ReportDialog, "exec", lambda self: shown.append(self) or 0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    track = section(window, "Música").model.items[0]
    window.catalog.save_user_data(track["id"], edits={"genre": "Rock"})
    window._write_files([track["id"]])
    wait_until(lambda: window.worker is None, 30000)
    text = "\n".join(shown[0].tree.topLevelItem(0).child(i).text(0)
                     for i in range(shown[0].tree.topLevelItem(0).childCount()))
    assert "Género = «Rock» ✓" in text
    assert metadata.read_file(os.path.join(track["root_path"], track["rel_path"]), "audio")[0]["genre"] == "Rock"


def test_verify_button_reports_file_contents(window, monkeypatch):
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: messages.append(a[2]))
    track = section(window, "Música").model.items[0]
    panel_item = window.catalog.get_item(track["id"])
    window.detail.show_item(panel_item)
    window.detail._verify_file()
    assert "Todo lo que ves está escrito en el archivo" in messages[-1]
    window.catalog.save_user_data(track["id"], edits={"album": "Otro"})
    window.detail.show_item(window.catalog.get_item(track["id"]))
    window.detail._verify_file()
    assert "1 campo(s) solo están en el catálogo" in messages[-1] and "✗ Álbum" in messages[-1]


def test_rescan_keeps_activity(window):
    track = section(window, "Música").model.items[0]
    window.catalog.record_play(track["id"])
    scanner.scan_root(window.catalog, track["root_id"])
    assert window.catalog.activity()[track["id"]]["play_count"] == 1



def test_reloading_all_view_does_not_cancel_other_covers(window, tmp_path):
    cover = tmp_path / "azul.jpg"
    Image.new("RGB", (64, 64), "blue").save(cover)
    root = next(r for r in window.catalog.roots() if r["path"].endswith("musica"))
    target = os.path.join(root["path"], "azul.mp3")
    ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-i", str(cover), "-map", "0", "-map", "1", "-c:v", "mjpeg",
           "-id3v2_version", "3", "-disposition:v", "attached_pic", target)
    metadata.write_file(target, "audio", {"album": "Azul", "title": "Azul"})
    scanner.scan_root(window.catalog, root["id"])
    window.thumbs.pixmaps.clear()
    window.thumbs.requested.clear()
    music = section(window, "Música")
    music.browse_mode.setCurrentIndex(1)
    # Antes, recargar la vista «Todo» cancelaba las carátulas pedidas por otras listas.
    for _ in range(5):
        window.reload()
    item = next(t for t in music.tracks if t["rel_path"] == "azul.mp3")
    wait_until(lambda: window.thumbs.has(item))
    os.remove(target)
    scanner.scan_root(window.catalog, root["id"])


def test_writing_a_video_open_in_favorites_player_releases_it(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    favorites = section(window, "Videos favoritos")
    video = favorites.model.items[0]
    favorites.watch(favorites.model.items, 0)
    wait_until(lambda: favorites.player.player.duration() > 0)
    favorites.player.player.pause()
    window.catalog.save_user_data(video["id"], edits={"genre": "Humor"})
    window._write_files([video["id"]])
    wait_until(lambda: window.worker is None, 30000)
    path = os.path.join(video["root_path"], video["rel_path"])
    assert metadata.read_file(path, "video")[0]["genre"] == "Humor"
    assert "Reproducir" in favorites.player.message.text()
    favorites.player.toggle()
    wait_until(lambda: favorites.player.player.duration() > 0)
    favorites._back()


def test_writing_the_song_in_the_bar_releases_it(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    track = section(window, "Música").model.items[0]
    window.play_items([track], 0)
    bar = window.now_playing
    wait_until(lambda: bar.player.duration() > 0)
    bar.pause()
    window.catalog.save_user_data(track["id"], edits={"genre": "Jazz"})
    window._write_files([track["id"]])
    wait_until(lambda: window.worker is None, 30000)
    assert metadata.read_file(os.path.join(track["root_path"], track["rel_path"]), "audio")[0]["genre"] == "Jazz"
    bar.toggle()
    wait_until(lambda: bar.player.duration() > 0)


def test_file_locked_by_other_program_gives_clear_message(tmp_path, monkeypatch):
    path = tmp_path / "v.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=duration=1:size=64x36", str(path))

    def locked(src, dst):
        raise PermissionError(5, "Acceso denegado")

    monkeypatch.setattr(metadata.os, "replace", locked)
    monkeypatch.setattr(metadata, "REPLACE_WAIT", 0)
    with pytest.raises(metadata.MetadataError, match="otro programa lo tiene abierto"):
        metadata.write_file(str(path), "video", {"title": "x"})
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["v.mp4"]


def test_book_html_description_is_rendered_not_shown_raw(window):
    books = section(window, "Libros")
    item = books.books[0]
    window.catalog.save_user_data(item["id"], edits={"description": "<p>Primera <cite>edición</cite></p>"})
    books.refresh()
    books.table.selectRow(0)
    assert books.description.toPlainText().strip() == "Primera edición"



def favorites_with_root(window):
    favorites = section(window, "Videos favoritos")
    favorites.refresh()
    return favorites


def test_favorite_edit_form_has_channel_and_no_film_fields(window):
    favorites = favorites_with_root(window)
    video = favorites.model.items[0]
    window.detail.show_item(window.catalog.get_item(video["id"]))
    assert list(window.detail.inputs) == ["title", "artist", "year", "genre", "description"]
    movie = next(v for v in window.catalog.query_items(kind="video") if "Blade" in v["rel_path"])
    window.detail.show_item(movie)
    assert {"director", "series", "season", "episode"} <= set(window.detail.inputs)


def test_channel_is_written_to_file_and_groups_videos(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    favorites = favorites_with_root(window)
    videos = favorites.model.items
    for video in videos:
        window.catalog.save_user_data(video["id"], edits={"artist": "Canal Uno"})
    window._write_files([v["id"] for v in videos])
    wait_until(lambda: window.worker is None, 30000)
    for video in videos:
        path = os.path.join(video["root_path"], video["rel_path"])
        assert metadata.read_file(path, "video")[0]["artist"] == "Canal Uno"
    favorites.refresh()
    assert list(favorites.channels()) == ["Canal Uno"]
    favorites.show_channels()
    assert [c["name"] for c in favorites.channels_model.items] == ["Canal Uno"]
    favorites.show_channel("Canal Uno")
    assert favorites.pages.currentWidget() is favorites.channel_page
    assert len(favorites.channel_model.items) == 2 and "2 videos" in favorites.channel_meta.text()
    favorites.watch(favorites.channel_model.items, 0)
    assert favorites.channel_button.text() == "Canal Uno"
    favorites._back()
    assert favorites.pages.currentWidget() is favorites.channel_page
    # Los vídeos de ejemplo se comparten entre pruebas: se deja el autor como estaba.
    for video in videos:
        window.catalog.save_user_data(video["id"], edits={"artist": ""})
    window._write_files([v["id"] for v in videos])
    wait_until(lambda: window.worker is None, 30000)


def test_videos_without_author_share_a_channel(window):
    favorites = favorites_with_root(window)
    assert list(favorites.channels()) == ["Sin autor"]


def test_play_counts_only_after_95_percent(window):
    from mikoshi.player import PlayerHooks

    favorites = favorites_with_root(window)
    video = favorites.model.items[0]
    hooks = PlayerHooks(window.catalog, count_at=0.95)
    hooks.played(video)
    assert window.catalog.activity().get(video["id"], {}).get("play_count", 0) == 0
    assert not hooks.reached(940, 1000) and hooks.reached(950, 1000)
    favorites.watch(favorites.model.items, 0)
    player = favorites.player
    wait_until(lambda: player.player.duration() > 0)
    player._position_changed(int(player.player.duration() * 0.5))
    assert window.catalog.activity().get(video["id"], {}).get("play_count", 0) == 0
    player._position_changed(int(player.player.duration() * 0.96))
    player._position_changed(player.player.duration())
    assert window.catalog.activity()[video["id"]]["play_count"] == 1
    favorites._back()


def test_unfinished_video_asks_to_resume_or_restart(window, monkeypatch):
    favorites = favorites_with_root(window)
    video = favorites.model.items[0]
    window.catalog.save_progress(video["id"], 1500, 3000)
    asked = []
    monkeypatch.setattr(favorites, "ask_resume", lambda item, ms: asked.append(ms) or False)
    favorites.watch(favorites.model.items, 0)
    assert asked == [1500] and favorites.player.pending_seek == 0
    favorites._back()
    window.catalog.save_progress(video["id"], 1500, 3000)
    monkeypatch.setattr(favorites, "ask_resume", lambda item, ms: asked.append(ms) or True)
    favorites.watch(favorites.model.items, 0)
    assert favorites.player.pending_seek == 1500 or favorites.player.player.position() >= 1000
    favorites._back()
    asked.clear()
    window.catalog.set_watched(video["id"], True)
    favorites.watch(favorites.model.items, 0)
    assert asked == []
    favorites._back()


def test_restart_option_skips_question(window, monkeypatch):
    favorites = favorites_with_root(window)
    video = favorites.model.items[0]
    window.catalog.save_progress(video["id"], 1500, 3000)
    monkeypatch.setattr(favorites, "ask_resume", lambda item, ms: pytest.fail("no debía preguntar"))
    favorites.refresh()
    menu = favorites.video_menu(favorites.model.items, 0)
    next(a for a in menu.actions() if a.text() == "Reproducir desde el principio").trigger()
    assert favorites.player.pending_seek == 0
    favorites._back()


def test_right_click_menu_offers_video_actions(window):
    favorites = favorites_with_root(window)
    labels = [a.text() for a in favorites.video_menu(favorites.model.items, 0).actions() if a.text()]
    for expected in ("Reproducir", "Editar metadatos…", "Ver canal «Sin autor»", "Marcar como visto",
                     "Ver en carpeta", "Más opciones"):
        assert expected in labels
    menu = favorites.video_menu(favorites.model.items, 0)
    next(a for a in menu.actions() if a.text() == "Marcar como visto").trigger()
    assert window.catalog.activity()[favorites.model.items[0]["id"]]["watched"] == 1 or any(
        s.get("watched") for s in window.catalog.activity().values())


def test_back_button_is_top_left_on_watch_page(window):
    favorites = favorites_with_root(window)
    favorites.watch(favorites.model.items, 0)
    favorites.resize(1200, 800)
    window.resize(1400, 900)
    back = next(b for b in favorites.watch_page.findChildren(QPushButton) if b.text() == "← Volver")
    position = back.mapTo(favorites.watch_page, back.rect().topLeft())
    assert position.y() < 60 and position.x() < 60
    favorites._back()


def test_fullscreen_fills_the_window_and_restores_everything(window):
    favorites = favorites_with_root(window)
    favorites.watch(favorites.model.items, 0)
    player = favorites.player
    others = [window.tabs.tabBar(), window.menuBar(), favorites.up_next, favorites.watch_title,
              favorites.channel_button, window.statusBar()]
    assert all(w.isVisible() for w in others)
    player.toggle_fullscreen()
    # No se abre ninguna ventana nueva: es la de Mikoshi la que ocupa la pantalla.
    assert player.immersive and not player.video.isWindow()
    assert window.windowState() & Qt.WindowState.WindowFullScreen
    assert not any(w.isVisible() for w in others)
    assert player.video.isVisible() and player.position.isVisible()
    assert player.fullscreen_button.text() == "Salir de pantalla completa"
    player.toggle_fullscreen()
    assert not player.immersive and all(w.isVisible() for w in others)
    assert not window.windowState() & Qt.WindowState.WindowFullScreen
    player.toggle_fullscreen()
    favorites._back()
    assert not player.immersive and window.tabs.tabBar().isVisible()


def test_escape_and_double_click_toggle_fullscreen(window):
    from PySide6.QtTest import QTest

    favorites = favorites_with_root(window)
    favorites.watch(favorites.model.items, 0)
    player = favorites.player
    QTest.mouseDClick(player.video, Qt.MouseButton.LeftButton)
    assert player.immersive
    QTest.keyClick(player.video, Qt.Key.Key_Escape)
    assert not player.immersive
    favorites._back()


def test_clicking_progress_bar_jumps_there(window):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from mikoshi.player import SeekSlider

    slider = SeekSlider()
    slider.setRange(0, 1000)
    slider.resize(400, 20)
    slider.show()
    jumped = []
    slider.sliderReleased.connect(lambda: jumped.append(slider.value()))
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(300, 10))
    assert 700 <= slider.value() <= 800 and jumped == [slider.value()]

    favorites = favorites_with_root(window)
    favorites.watch(favorites.model.items, 0)
    player = favorites.player
    wait_until(lambda: player.player.duration() > 0)
    bar = player.position
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=QPoint(bar.width() // 2, bar.height() // 2))
    wait_until(lambda: abs(player.player.position() - player.player.duration() // 2) < player.player.duration() * 0.15)
    favorites._back()

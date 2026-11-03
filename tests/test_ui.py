import io
import os
import shutil
import subprocess

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PIL import Image
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from mikoshi import metadata, scanner
from mikoshi.db import Catalog
from mikoshi.ui import stars


def wait_until(condition, ms=30000):
    loop = QEventLoop()
    timer = QTimer(interval=20)
    timer.timeout.connect(lambda: condition() and loop.quit())
    timer.start()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    timer.stop()
    assert condition()


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_modal_popups(monkeypatch):
    """Las ventanas modales bloquearían las pruebas: se registran en vez de mostrarse."""
    from mikoshi import dialogs

    shown = []
    monkeypatch.setattr(dialogs.ReportDialog, "exec", lambda self: shown.append(self) or 0)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a) or QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a) or QMessageBox.StandardButton.Ok)
    return shown


@pytest.fixture
def window(app, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    Image.new("RGB", (32, 32), "red").save(lib / "foto.jpg")
    (lib / "zelda.gba").write_bytes(b"x")
    from mikoshi.ui import MainWindow

    win = MainWindow(tmp_path / "ui.db")
    win.catalog.add_root(str(lib))
    win.show()
    win.lib = lib
    yield win
    win.close()


def scan(win):
    win.scan_all()
    wait_until(lambda: win.worker is None)


def select(win, name):
    row = next(i for i, item in enumerate(win.model.items) if name in item["rel_path"])
    win.grid.setCurrentIndex(win.model.index(row, 0))


def choose(win, kind, value=None):
    for t in range(win.sidebar.topLevelItemCount()):
        section = win.sidebar.topLevelItem(t)
        for c in range(section.childCount()):
            node = section.child(c)
            if tuple(node.data(0, Qt.ItemDataRole.UserRole)) == (kind, value):
                win.sidebar.clearSelection()
                node.setSelected(True)
                return
    raise AssertionError((kind, value))


def test_scan_fills_views(window):
    scan(window)
    assert len(window.model.items) == 2
    choose(window, "kind", "game")
    assert [i["rel_path"] for i in window.model.items] == ["zelda.gba"]
    choose(window, "all")
    window.search.setText("foto")
    assert [i["rel_path"] for i in window.model.items] == ["foto.jpg"]


def test_edit_saved_on_selection_change_without_touching_file(window):
    scan(window)
    before = (window.lib / "foto.jpg").read_bytes()
    select(window, "foto")
    window.detail.inputs["title"].setText("Atardecer")
    window.detail._mark_dirty()
    window.detail.tags.setText("viaje, verano")
    select(window, "zelda")
    item = next(i for i in window.catalog.query_items() if i["rel_path"] == "foto.jpg")
    assert item["edits"]["title"] == "Atardecer"
    assert set(item["tags"].split(", ")) == {"viaje", "verano"}
    assert (window.lib / "foto.jpg").read_bytes() == before
    choose(window, "tag", "viaje")
    assert [i["rel_path"] for i in window.model.items] == ["foto.jpg"]


def test_write_to_file_clears_pending(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    scan(window)
    select(window, "foto")
    window.detail.inputs["artist"].setText("Peña")
    window.detail._mark_dirty()
    window.detail.commit()
    assert window.detail.write_button.isEnabled()
    window._write_files(window._selected_ids())
    wait_until(lambda: window.worker is None)
    assert metadata.read_file(str(window.lib / "foto.jpg"), "image")[0]["artist"] == "Peña"
    item = next(i for i in window.catalog.query_items() if i["rel_path"] == "foto.jpg")
    assert item["edits"] == {} and item["file_meta"]["artist"] == "Peña"
    assert scanner.scan_root(window.catalog, item["root_id"]).updated == 0


def test_write_button_enabled_while_typing_and_write_commits(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    scan(window)
    select(window, "foto")
    assert not window.detail.write_button.isEnabled()
    window.detail.inputs["artist"].setText("Ana")
    window.detail._mark_dirty()
    assert window.detail.write_button.isEnabled()
    window.detail.write_button.click()
    wait_until(lambda: window.worker is None)
    assert metadata.read_file(str(window.lib / "foto.jpg"), "image")[0]["artist"] == "Ana"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requiere ffmpeg")
def test_mp4_keeps_valid_fields_and_reports_rejected(tmp_path):
    cat = Catalog(tmp_path / "x.db")
    lib = tmp_path / "l"
    lib.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64",
                    str(lib / "v.mp4")], check=True)
    scanner.scan_root(cat, cat.add_root(str(lib)))
    item = cat.query_items()[0]
    cat.save_user_data(item["id"], edits={"genre": "Drama", "season": "awf"})
    assert scanner.apply_edits_to_file(cat, cat.get_item(item["id"])) == ["season"]
    assert metadata.read_file(str(lib / "v.mp4"), "video")[0]["genre"] == "Drama"
    assert cat.get_item(item["id"])["edits"] == {"season": "awf"}
    cat.close()


def test_grid_paints_with_rating_and_follows_zoom(window):
    scan(window)
    window.catalog.set_rating([window.model.items[0]["id"]], 4)
    window.reload()
    assert not window.grab().isNull()
    window.zoom.setValue(200)
    assert window.grid.iconSize().width() == 200
    assert window.grid.itemDelegate().sizeHint(None, None).height() > 200


def test_welcome_page_until_a_folder_exists(app, tmp_path):
    from mikoshi.ui import MainWindow

    win = MainWindow(tmp_path / "vacio.db")
    assert win.stack.currentIndex() == win.WELCOME
    win.close()


def test_filters_by_rating_genre_and_year(window):
    (window.lib / "b.gba").write_bytes(b"xx")
    scan(window)
    items = {i["rel_path"]: i for i in window.catalog.query_items()}
    window.catalog.save_user_data(items["zelda.gba"]["id"], rating=5, edits={"genre": "RPG, Acción", "year": "1998"})
    window.catalog.save_user_data(items["b.gba"]["id"], rating=2, edits={"genre": "RPG", "year": "2005"})
    window.reload()
    window.rating_filter.setCurrentIndex(3)
    assert [i["rel_path"] for i in window.model.items] == ["zelda.gba"]
    window.rating_filter.setCurrentIndex(0)
    window.genre_filter.setCurrentText("RPG")
    assert len(window.model.items) == 2
    window.year_from.setText("2000")
    assert [i["rel_path"] for i in window.model.items] == ["b.gba"]
    window.year_from.clear()
    window.year_to.setText("2000")
    assert [i["rel_path"] for i in window.model.items] == ["zelda.gba"]


def test_series_group_uses_episode_order(window):
    for name in ("Dark.S01E10", "Dark.S01E02", "Dark.S02E01"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64",
                        str(window.lib / f"{name}.mp4")], check=True)
    scan(window)
    choose(window, "series", "Dark")
    assert [i["rel_path"] for i in window.model.items] == [
        "Dark.S01E02.mp4", "Dark.S01E10.mp4", "Dark.S02E01.mp4"]


def test_sort_box_orders_grid_by_rating(window):
    scan(window)
    target = next(i for i in window.model.items if i["rel_path"] == "zelda.gba")
    window.catalog.set_rating([target["id"]], 5)
    window.reload()
    window.sort_box.setCurrentIndex(3)
    assert window.model.items[0]["rel_path"] == "zelda.gba"


def test_bulk_edit_dialog_only_changes_checked_fields(window, app):
    from mikoshi.dialogs import BulkEditDialog

    scan(window)
    games = window.catalog.query_items(kind="game")
    dialog = BulkEditDialog(games)
    assert "title" not in dialog.rows and "genre" in dialog.rows
    dialog.rows["genre"][0].setChecked(True)
    dialog.rows["genre"][1].setText("RPG")
    dialog.rows["year"][1].setText("ignorado")
    assert dialog.values() == {"genre": "RPG"}


def test_bulk_dialog_with_mixed_kinds_offers_shared_fields_only(window):
    from mikoshi.dialogs import BulkEditDialog

    scan(window)
    dialog = BulkEditDialog(window.catalog.query_items())
    assert "platform" not in dialog.rows and "description" in dialog.rows


def test_online_results_fill_form_without_saving(window, monkeypatch):
    scan(window)
    select(window, "zelda")
    item_id = window.detail.item["id"]
    window.detail.apply_online({"developer": "Nintendo", "year": "1998"}, None)
    assert window.detail.inputs["developer"].text() == "Nintendo" and window.detail.dirty
    assert window.catalog.get_item(item_id)["edits"] == {}
    window.detail.commit()
    assert window.catalog.get_item(item_id)["edits"]["developer"] == "Nintendo"


def test_online_cover_is_stored_and_replaces_previous(window):
    from mikoshi.db import covers_dir

    scan(window)
    select(window, "zelda")
    item_id = window.detail.item["id"]
    jpeg = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(jpeg, "JPEG")
    window.detail.apply_online({}, jpeg.getvalue())
    first = window.catalog.get_item(item_id)["cover"]
    window.detail.apply_online({}, jpeg.getvalue())
    second = window.catalog.get_item(item_id)["cover"]
    assert first != second and os.path.exists(second) and not os.path.exists(first)
    assert len(list(covers_dir().iterdir())) == 1
    wait_until(lambda: any(k.startswith(f"{item_id}|") for k in window.model.pixmaps))


def test_online_dialog_searches_and_returns_details(app, monkeypatch):
    from mikoshi import online
    from mikoshi.dialogs import OnlineDialog, wait_for_tasks

    result = online.Result("Dune", "Frank Herbert", {"title": "Dune"}, "", "Open Library")
    monkeypatch.setattr(online, "search", lambda kind, query, keys: [result])
    monkeypatch.setattr(online, "details", lambda r, keys: {"title": "Dune", "author": "F. H.", "platform": "no"})
    dialog = OnlineDialog("document", "dune", {})
    wait_until(lambda: dialog.results.count() == 1)
    dialog._apply()
    wait_until(lambda: dialog.fields != {})
    assert dialog.fields == {"title": "Dune", "author": "F. H."}
    wait_for_tasks()


def test_online_dialog_shows_missing_key_message(app):
    from mikoshi.dialogs import OnlineDialog, wait_for_tasks

    dialog = OnlineDialog("game", "hades", {"rawg_key": ""})
    wait_until(lambda: "clave" in dialog.status.text())
    assert dialog.search_button.isEnabled()
    wait_for_tasks()


def test_settings_dialog_saves_values(window):
    from mikoshi.dialogs import SettingsDialog

    dialog = SettingsDialog(window.catalog)
    dialog.tmdb.setText(" abc ")
    dialog.backup.setText("C:\\copia")
    dialog._save()
    assert window.catalog.setting("tmdb_key") == "abc"
    assert window.catalog.setting("backup_folder") == "C:\\copia"


def test_collection_menu_flow_and_delete(window, monkeypatch):
    scan(window)
    ids = [i["id"] for i in window.model.items]
    monkeypatch.setattr("mikoshi.ui.QInputDialog.getText", lambda *a, **k: ("Favoritos", True))
    window._add_to_collection(None, ids[:1])
    collection = window.catalog.collections()[0]["id"]
    choose(window, "collection", collection)
    assert [i["id"] for i in window.model.items] == ids[:1]
    window._remove_from_collection(collection, ids[:1])
    assert window.model.items == []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._delete_collection(collection, "Favoritos")
    assert window.catalog.collections() == [] and len(window.model.items) == 2


def labels(actions):
    return [a[0] for a in actions if a is not None]


def fake_name(monkeypatch, text):
    monkeypatch.setattr("mikoshi.ui.QInputDialog.getText", lambda *a, **k: (text, True))


def yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)


def sidebar_data(win):
    found = []

    def walk(node):
        found.append(tuple(node.data(0, Qt.ItemDataRole.UserRole)))
        for n in range(node.childCount()):
            walk(node.child(n))

    for t in range(win.sidebar.topLevelItemCount()):
        walk(win.sidebar.topLevelItem(t))
    return found


def test_every_sidebar_node_has_valid_menu(window, monkeypatch):
    scan(window)
    item = window.catalog.query_items()[0]
    window.catalog.add_tag([item["id"]], "retro")
    folder = window.catalog.create_folder("Viajes")
    window.catalog.create_collection("Mix", "playlist", folder)
    window.catalog.save_user_data(item["id"], edits={"series": "Dark", "album": "Uno"})
    window._after_sidebar_change()
    data = sidebar_data(window)
    expected = {("list_folder", folder), ("tag", "retro"), ("series", "Dark"), ("album", "Uno"),
                ("section", "lists"), ("section", "roots"), ("all", None), ("pending", None), ("missing", None)}
    assert expected <= set(data)
    for node in data:
        actions = window._sidebar_actions(node)
        menu = QMenu()
        window._fill_menu(menu, actions)
    assert "Nueva playlist…" in labels(window._sidebar_actions(("section", "lists")))
    assert "Nueva carpeta…" in labels(window._sidebar_actions(("section", "lists")))


def test_create_rename_move_delete_folder_and_playlist(window, monkeypatch):
    scan(window)
    fake_name(monkeypatch, "Vacaciones")
    window._create_folder()
    folder = window.catalog.list_folders()[0]["id"]
    fake_name(monkeypatch, "Mix")
    pid = window._create_list("playlist", folder)
    assert ("collection", pid) in sidebar_data(window)
    assert window.catalog.collection(pid)["folder_id"] == folder

    fake_name(monkeypatch, "Mix 2")
    window._rename_list(pid)
    assert window.catalog.collection(pid)["name"] == "Mix 2"
    window._move_list(pid, None)
    assert window.catalog.collection(pid)["folder_id"] is None
    window._move_list(pid, folder)

    yes(monkeypatch)
    window._delete_folder(folder, "Vacaciones")
    assert window.catalog.list_folders() == [] and window.catalog.collection(pid)["folder_id"] is None
    window._delete_collection(pid, "Mix 2")
    assert window.catalog.collections() == []
    assert len(window.catalog.query_items()) == 2


def test_duplicate_list_name_is_refused(window, monkeypatch):
    window.catalog.create_collection("Mix")
    fake_name(monkeypatch, "mix")
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    assert window._create_list("playlist") is None
    assert len(window.catalog.collections()) == 1 and shown


def test_playlist_view_keeps_order_despite_title_sort(window):
    (window.lib / "a.gba").write_bytes(b"xx")
    scan(window)
    ids = {i["rel_path"]: i["id"] for i in window.catalog.query_items()}
    pid = window.catalog.create_collection("Mix", "playlist")
    window.catalog.add_to_collection(pid, [ids["zelda.gba"], ids["a.gba"]])
    window._after_sidebar_change()
    choose(window, "collection", pid)
    assert [i["rel_path"] for i in window.model.items] == ["zelda.gba", "a.gba"]
    window._move_in_list(pid, ids["a.gba"], -1)
    assert [i["rel_path"] for i in window.model.items] == ["a.gba", "zelda.gba"]
    assert window.detail.item["id"] == ids["a.gba"]


def test_play_list_opens_only_playable_in_order(window, monkeypatch):
    scan(window)
    ids = {i["rel_path"]: i["id"] for i in window.catalog.query_items()}
    pid = window.catalog.create_collection("Mix", "playlist")
    window.catalog.add_to_collection(pid, [ids["zelda.gba"], ids["foto.jpg"]])
    opened = []
    monkeypatch.setattr("mikoshi.ui.PlayerDialog.exec", lambda self: opened.append([i["rel_path"] for i in self.items]))
    window._play_list(pid)
    assert opened == [["foto.jpg"]]
    empty = window.catalog.create_collection("Vacía", "playlist")
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[1]))
    window._play_list(empty)
    assert shown == ["Nada que reproducir"]


def test_sidebar_rename_series_and_remove_root_touch_no_files(window, monkeypatch):
    scan(window)
    item = window.catalog.query_items()[0]
    window.catalog.save_user_data(item["id"], edits={"series": "Dark"})
    fake_name(monkeypatch, "Oscuro")
    window._rename_group("series", "Dark")
    assert window.catalog.group_names("series") == ["Oscuro"]

    yes(monkeypatch)
    root = window.catalog.roots()[0]["id"]
    window._remove_root(root)
    assert window.catalog.query_items() == []
    assert (window.lib / "zelda.gba").exists() and (window.lib / "foto.jpg").exists()


def find_action(menu, text):
    for action in menu.actions():
        if action.text() == text:
            return action
        if action.menu():
            found = find_action(action.menu(), text)
            if found:
                return found
    return None


def test_real_sidebar_menu_actions_get_correct_arguments(window, monkeypatch):
    fake_name(monkeypatch, "Raíz")
    menu = QMenu()
    window._fill_menu(menu, window._sidebar_actions(("section", "lists")))
    find_action(menu, "Nueva carpeta…").trigger()
    top = window.catalog.list_folders()[0]
    assert top["parent_id"] is None

    other = window.catalog.create_folder("Destino")
    pid = window.catalog.create_collection("Mix", "playlist", other)
    window._after_sidebar_change()
    menu = QMenu()
    window._fill_menu(menu, window._sidebar_actions(("collection", pid)))
    find_action(menu, "Raíz").trigger()
    assert window.catalog.collection(pid)["folder_id"] == top["id"]
    menu = QMenu()
    window._fill_menu(menu, window._sidebar_actions(("collection", pid)))
    find_action(menu, "Sin carpeta").trigger()
    assert window.catalog.collection(pid)["folder_id"] is None


def test_real_item_menu_rates_and_adds_to_list(window, monkeypatch):
    scan(window)
    select(window, "zelda")
    ids = window._selected_ids()
    pid = window.catalog.create_collection("Mix", "playlist")
    menu = window._item_menu(ids)
    find_action(menu, stars(4)).trigger()
    assert window.catalog.get_item(ids[0])["rating"] == 4
    select(window, "zelda")
    menu = window._item_menu(ids)
    find_action(menu, "♫ Mix").trigger()
    assert window.catalog.list_order(pid) == ids


def test_right_click_on_empty_space_offers_creating_lists(window):
    assert "Nueva playlist…" in labels(window._sidebar_actions(("section", "lists")))


def test_automatic_backup_runs_once_a_day_and_reports_failure(window, tmp_path):
    copy = tmp_path / "copia"
    copy.mkdir()
    window.catalog.set_setting("backup_folder", str(copy))
    window._automatic_backup()
    assert (copy / "catalogo.db").exists()
    (copy / "catalogo.db").unlink()
    window._automatic_backup()
    assert not (copy / "catalogo.db").exists()
    window.catalog.set_setting("last_backup", "0")
    window.catalog.set_setting("backup_folder", str(tmp_path / "no-existe"))
    window._automatic_backup()
    assert "no-existe" in window.catalog.setting("backup_error")


def test_maintenance_reports_run_from_window(window, monkeypatch):
    from mikoshi import dialogs

    shown = []
    monkeypatch.setattr(dialogs.ReportDialog, "exec", lambda self: shown.append(self.windowTitle()))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    shutil.copy(window.lib / "foto.jpg", window.lib / "copia.jpg")
    scan(window)
    window._find_duplicates()
    wait_until(lambda: window.worker is None)
    window._verify()
    wait_until(lambda: window.worker is None)
    assert shown == ["Duplicados", "Integridad de la colección"]


def player_for(window, name):
    from mikoshi.player import PlayerDialog

    items = [i for i in window.catalog.query_items() if name in i["rel_path"]]
    dialog = PlayerDialog(items, 0, lambda path: None)
    dialog.show()
    return dialog


def test_player_loads_video_and_can_seek(window):
    from PySide6.QtMultimedia import QMediaPlayer

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=64x64",
                    str(window.lib / "clip.mp4")], check=True)
    scan(window)
    dialog = player_for(window, "clip")
    wait_until(lambda: dialog.player.duration() > 0)
    assert dialog.player.error() == QMediaPlayer.Error.NoError
    dialog.toggle()
    dialog.close()


def test_player_shows_image_and_navigates(window):
    from mikoshi.player import PlayerDialog

    shutil.copy(window.lib / "foto.jpg", window.lib / "foto2.jpg")
    scan(window)
    items = window.catalog.query_items(kind="image")
    dialog = PlayerDialog(items, 0, lambda path: None)
    dialog.show()
    assert dialog.stage.currentWidget() is dialog.picture and not dialog.previous_button.isEnabled()
    dialog.go(1)
    assert dialog.index == 1 and not dialog.next_button.isEnabled()
    dialog.go(1)
    assert dialog.index == 1
    dialog.close()


def test_player_handles_missing_and_broken_files(window):
    scan(window)
    window.model.pool.waitForDone()
    (window.lib / "foto.jpg").unlink()
    dialog = player_for(window, "foto")
    assert dialog.stage.currentWidget() is dialog.message and "servidor" in dialog.message.text()
    dialog.close()
    (window.lib / "roto.mp4").write_bytes(b"no es video")
    scan(window)
    dialog = player_for(window, "roto")
    wait_until(lambda: dialog.stage.currentWidget() is dialog.message and "reproducir" in dialog.message.text())
    dialog.close()


def test_catalog_only_field_survives_file_write(tmp_path):
    cat = Catalog(tmp_path / "x.db")
    lib = tmp_path / "l"
    lib.mkdir()
    Image.new("RGB", (8, 8)).save(lib / "a.jpg")
    scanner.scan_root(cat, cat.add_root(str(lib)))
    item = cat.query_items()[0]
    cat.save_user_data(item["id"], edits={"title": "T", "date": "2020"})
    scanner.apply_edits_to_file(cat, cat.get_item(item["id"]))
    assert cat.get_item(item["id"])["edits"] == {"date": "2020"}
    cat.close()


def test_thumbnail_arrives(window):
    scan(window)
    select(window, "foto")
    wait_until(lambda: len(window.model.pixmaps) >= 1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requiere ffmpeg")
def test_short_video_gets_thumbnail(window):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64",
                    str(window.lib / "corto.mp4")], check=True)
    scan(window)
    select(window, "corto")
    wait_until(lambda: any(k.startswith(str(window.detail.item["id"]) + "|") for k in window.model.pixmaps))

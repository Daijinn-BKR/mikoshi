import os
import shutil
import subprocess

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PIL import Image
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from catalogo import metadata, scanner
from catalogo.db import Catalog


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


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGO_DATA", str(tmp_path / "datos"))
    (tmp_path / "datos").mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    Image.new("RGB", (32, 32), "red").save(lib / "foto.jpg")
    (lib / "zelda.gba").write_bytes(b"x")
    from catalogo.ui import MainWindow

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


def test_grid_renders_long_title(window):
    scan(window)
    window.grab()
    assert window.grid.itemDelegate().sizeHint(None, None) == window.grid.itemDelegate().CELL


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

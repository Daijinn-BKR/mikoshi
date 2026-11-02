"""Catálogo en SQLite.

La base vive en el disco local del PC, no en la carpeta de red: SQLite no es
fiable sobre recursos compartidos. Las rutas se guardan relativas a cada carpeta
raíz, así que si los archivos cambian de servidor basta con cambiar la raíz.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL DEFAULT 'auto'
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    added_at REAL NOT NULL,
    rating INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    cover TEXT NOT NULL DEFAULT '',
    file_meta TEXT NOT NULL DEFAULT '{}',
    edits TEXT NOT NULL DEFAULT '{}',
    technical TEXT NOT NULL DEFAULT '{}',
    UNIQUE (root_id, rel_path)
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
"""


def data_dir() -> Path:
    base = os.environ.get("CATALOGO_DATA") or os.environ.get("LOCALAPPDATA")
    path = Path(base) if base else Path.home() / ".local" / "share"
    if not os.environ.get("CATALOGO_DATA"):
        path = path / "CatalogoMultimedia"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_db_path() -> Path:
    return data_dir() / "catalogo.db"


def covers_dir() -> Path:
    path = data_dir() / "portadas"
    path.mkdir(parents=True, exist_ok=True)
    return path


def effective(item: dict) -> dict:
    """Metadatos que ve el usuario: los del archivo con sus ediciones encima."""
    return {**item["file_meta"], **item["edits"]}


def display_title(item: dict) -> str:
    title = effective(item).get("title")
    return title or Path(item["rel_path"]).stem


def full_path(item: dict) -> str:
    return os.path.join(item["root_path"], item["rel_path"])


class Catalog:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or default_db_path())
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # Carpetas raíz

    def add_root(self, path: str, mode: str = "auto") -> int:
        path = os.path.abspath(path)
        self.conn.execute(
            "INSERT INTO roots (path, mode) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET mode = excluded.mode",
            (path, mode),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM roots WHERE path = ?", (path,)).fetchone()
        return row["id"]

    def roots(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM roots ORDER BY path")]

    def root(self, root_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM roots WHERE id = ?", (root_id,)).fetchone())

    def relocate_root(self, root_id: int, new_path: str) -> None:
        self.conn.execute("UPDATE roots SET path = ? WHERE id = ?", (os.path.abspath(new_path), root_id))
        self.conn.commit()

    def remove_root(self, root_id: int) -> None:
        """Quita la carpeta del catálogo. Nunca toca los archivos."""
        self.conn.execute("DELETE FROM roots WHERE id = ?", (root_id,))
        self._drop_orphan_tags()
        self.conn.commit()

    # Elementos

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("file_meta", "edits", "technical"):
            item[key] = json.loads(item[key])
        return item

    def known_items(self, root_id: int) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT id, rel_path, size, mtime, status FROM items WHERE root_id = ?", (root_id,)
        )
        return {r["rel_path"]: r for r in rows}

    def insert_item(self, root_id, rel_path, kind, size, mtime, file_meta, technical) -> int:
        cur = self.conn.execute(
            "INSERT INTO items (root_id, rel_path, kind, size, mtime, added_at, file_meta, technical) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (root_id, rel_path, kind, size, mtime, time.time(),
             json.dumps(file_meta, ensure_ascii=False), json.dumps(technical, ensure_ascii=False)),
        )
        return cur.lastrowid

    def refresh_from_file(self, item_id, size, mtime, file_meta, technical) -> None:
        self.conn.execute(
            "UPDATE items SET size = ?, mtime = ?, status = 'ok', file_meta = ?, technical = ? WHERE id = ?",
            (size, mtime, json.dumps(file_meta, ensure_ascii=False),
             json.dumps(technical, ensure_ascii=False), item_id),
        )

    def set_status(self, item_id: int, status: str) -> None:
        self.conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))

    def move_item(self, item_id: int, rel_path: str, mtime: float) -> None:
        self.conn.execute(
            "UPDATE items SET rel_path = ?, mtime = ?, status = 'ok' WHERE id = ?",
            (rel_path, mtime, item_id),
        )

    def commit(self) -> None:
        self.conn.commit()

    def get_item(self, item_id: int) -> dict | None:
        row = self.conn.execute(self._SELECT + " WHERE i.id = ?", (item_id,)).fetchone()
        return self._decode(row) if row else None

    _SELECT = (
        "SELECT i.*, r.path AS root_path, "
        "(SELECT group_concat(t.name, ', ') FROM item_tags it JOIN tags t ON t.id = it.tag_id "
        " WHERE it.item_id = i.id) AS tags "
        "FROM items i JOIN roots r ON r.id = i.root_id"
    )

    def query_items(self, kind=None, tag=None, root_id=None, status=None, text=None) -> list[dict]:
        where, args = [], []
        if kind:
            where.append("i.kind = ?")
            args.append(kind)
        if root_id:
            where.append("i.root_id = ?")
            args.append(root_id)
        if status:
            where.append("i.status = ?")
            args.append(status)
        if tag:
            where.append(
                "i.id IN (SELECT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE t.name = ?)"
            )
            args.append(tag)
        for word in (text or "").split():
            where.append(
                "(i.rel_path LIKE ? OR i.file_meta LIKE ? OR i.edits LIKE ? OR i.notes LIKE ? OR i.id IN "
                "(SELECT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE t.name LIKE ?))"
            )
            args.extend([f"%{word}%"] * 5)
        sql = self._SELECT + (" WHERE " + " AND ".join(where) if where else "")
        return [self._decode(r) for r in self.conn.execute(sql, args)]

    def save_user_data(self, item_id, edits=None, rating=None, notes=None, cover=None) -> None:
        for column, value in (("rating", rating), ("notes", notes), ("cover", cover)):
            if value is not None:
                self.conn.execute(f"UPDATE items SET {column} = ? WHERE id = ?", (value, item_id))
        if edits is not None:
            self.conn.execute(
                "UPDATE items SET edits = ? WHERE id = ?",
                (json.dumps(edits, ensure_ascii=False), item_id),
            )
        self.conn.commit()

    def set_rating(self, item_ids, rating: int) -> None:
        self.conn.executemany("UPDATE items SET rating = ? WHERE id = ?", [(rating, i) for i in item_ids])
        self.conn.commit()

    def remove_items(self, item_ids) -> None:
        """Quita elementos del catálogo. Nunca toca los archivos."""
        self.conn.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in item_ids])
        self._drop_orphan_tags()
        self.conn.commit()

    # Etiquetas

    def _tag_id(self, name: str) -> int:
        self.conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        return self.conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]

    def _drop_orphan_tags(self) -> None:
        self.conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM item_tags)")

    def set_tags(self, item_id: int, names: list[str]) -> None:
        self.conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
        for name in {n.strip() for n in names if n.strip()}:
            self.conn.execute("INSERT OR IGNORE INTO item_tags VALUES (?, ?)", (item_id, self._tag_id(name)))
        self._drop_orphan_tags()
        self.conn.commit()

    def add_tag(self, item_ids, name: str) -> None:
        tag_id = self._tag_id(name.strip())
        self.conn.executemany("INSERT OR IGNORE INTO item_tags VALUES (?, ?)", [(i, tag_id) for i in item_ids])
        self.conn.commit()

    def all_tags(self) -> list[str]:
        return [r["name"] for r in self.conn.execute("SELECT name FROM tags ORDER BY name COLLATE NOCASE")]

    # Copias y exportación

    def backup_to(self, folder: str) -> str:
        """Copia consistente del catálogo y las portadas, por ejemplo al servidor."""
        dest = os.path.join(folder, "catalogo.db")
        target = sqlite3.connect(dest)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        covers = covers_dir()
        if any(covers.iterdir()):
            shutil.copytree(covers, os.path.join(folder, "portadas"), dirs_exist_ok=True)
        return dest

    def export_csv(self, path: str, items: list[dict] | None = None) -> int:
        items = self.query_items() if items is None else items
        keys = sorted({k for item in items for k in effective(item)})
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ruta", "tipo", "estado", "valoracion", "etiquetas", "notas", *keys])
            for item in items:
                meta = effective(item)
                writer.writerow([
                    full_path(item), item["kind"], item["status"], item["rating"],
                    item["tags"] or "", item["notes"], *[meta.get(k, "") for k in keys],
                ])
        return len(items)

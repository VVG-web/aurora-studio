#!/usr/bin/env python3
"""store.py — хранилище индекса кода: SQLite, WAL, FTS5 и запасной путь.

Один файл на репозиторий: `.aurora/cache/code/<имя>/index.sqlite`. Это кэш, а не знание:
его можно удалить и построить заново. Схема версионирована (`PRAGMA user_version`), поэтому
несовместимая версия не читается вслепую, а заменяется новой.

Таблицы повторяют контракт записей индекса v1 (приложение A плана Code). В CR-08 заполняются
`files`, `secrets`, `meta` и поисковые таблицы путей; остальные таблицы готовы для CR-09.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

try:
    from . import model
except ImportError:  # запуск файла напрямую из scripts/codeidx
    import model  # type: ignore


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    blob TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'src',
    module TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    lines INTEGER NOT NULL DEFAULT 0,
    enc TEXT NOT NULL DEFAULT '',
    skipped TEXT NOT NULL DEFAULT '',
    role_reason TEXT NOT NULL DEFAULT '',
    text_hash TEXT NOT NULL DEFAULT '',
    extractor_ver TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_files_blob ON files(blob);
CREATE INDEX IF NOT EXISTS idx_files_role ON files(role);
CREATE INDEX IF NOT EXISTS idx_files_skipped ON files(skipped);
CREATE TABLE IF NOT EXISTS extracted (
    blob TEXT NOT NULL,
    extractor TEXT NOT NULL,
    extractor_ver TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (blob, extractor, extractor_ver)
);
CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY,
    file TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qname TEXT NOT NULL DEFAULT '',
    line_start INTEGER NOT NULL DEFAULT 0,
    line_end INTEGER NOT NULL DEFAULT 0,
    sig TEXT NOT NULL DEFAULT '',
    doc TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT '',
    annotations TEXT NOT NULL DEFAULT '[]',
    parent TEXT NOT NULL DEFAULT '',
    frag_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols(qname);
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    file TEXT NOT NULL,
    src_id TEXT NOT NULL,
    dst TEXT NOT NULL,
    rel TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0,
    conf TEXT NOT NULL DEFAULT 'EXTRACTED',
    src TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel);
CREATE TABLE IF NOT EXISTS routes (
    id TEXT PRIMARY KEY,
    file TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    handler TEXT NOT NULL DEFAULT '',
    request TEXT NOT NULL DEFAULT '',
    response TEXT NOT NULL DEFAULT '',
    auth TEXT NOT NULL DEFAULT '',
    status_codes TEXT NOT NULL DEFAULT '[]',
    line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_routes_file ON routes(file);
CREATE TABLE IF NOT EXISTS db_tables (
    table_name TEXT NOT NULL,
    file TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (table_name, file, source)
);
CREATE TABLE IF NOT EXISTS db_columns (
    table_name TEXT NOT NULL,
    name TEXT NOT NULL,
    file TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT '',
    pk INTEGER NOT NULL DEFAULT 0,
    fk TEXT NOT NULL DEFAULT '',
    nullable INTEGER NOT NULL DEFAULT 1,
    is_unique INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topics (
    name TEXT NOT NULL,
    file TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS configs (
    key TEXT NOT NULL,
    file TEXT NOT NULL,
    default_value TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0,
    read_by TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS strings (
    text TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0,
    symbol TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tests (
    symbol TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS commit_files (
    sha TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    old_path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS commit_keys (
    sha TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS secrets (
    rule TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    mask TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (rule, path, line)
);
CREATE TABLE IF NOT EXISTS tokens (
    term TEXT NOT NULL,
    sym_id TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_tokens_term ON tokens(term);
"""

FTS_WORDS = "CREATE VIRTUAL TABLE IF NOT EXISTS fts_words USING fts5(path UNINDEXED, text, tokenize='unicode61')"
FTS_IDENT = "CREATE VIRTUAL TABLE IF NOT EXISTS fts_ident USING fts5(path UNINDEXED, text, tokenize='trigram')"


class Store:
    """Хранилище одного репозитория. Использовать как контекстный менеджер."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = self._open()
        self.fts_words = "off"
        self.fts_ident = "off"
        self._prepare()

    # ------------------------------------------------------------ открытие и схема

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _prepare(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version and version != model.SCHEMA_VERSION:
            self.conn.close()
            try:
                os.remove(str(self.path))
            except OSError:
                pass
            self.conn = self._open()
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version={model.SCHEMA_VERSION}")
        self.conn.commit()
        self._probe_fts()

    def _probe_fts(self) -> None:
        """FTS5 и trigram проверяются на месте: их может не быть в сборке SQLite."""
        try:
            self.conn.execute(FTS_WORDS)
            self.fts_words = "fts5-unicode61"
        except sqlite3.Error:
            self.fts_words = "off"
        try:
            self.conn.execute(FTS_IDENT)
            self.fts_ident = "fts5-trigram"
        except sqlite3.Error:
            self.fts_ident = "off"
        self.conn.commit()
        self.meta_set("fts_words", self.fts_words)
        self.meta_set("fts_ident", self.fts_ident)
        self.conn.commit()

    # ------------------------------------------------------------ meta и транзакции

    def meta_get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def meta_set(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def meta_json(self, key: str, default=None):
        raw = self.meta_get(key, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def meta_set_json(self, key: str, value) -> None:
        self.meta_set(key, json.dumps(value, ensure_ascii=False, sort_keys=True))

    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False

    # ------------------------------------------------------------ файлы

    def get_file(self, path: str):
        return self.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()

    def put_file(self, rec: model.FileRecord) -> None:
        self.conn.execute(
            "INSERT INTO files(path, blob, lang, role, module, bytes, lines, enc, skipped, role_reason, "
            "text_hash, extractor_ver) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET blob=excluded.blob, lang=excluded.lang, role=excluded.role, "
            "module=excluded.module, bytes=excluded.bytes, lines=excluded.lines, enc=excluded.enc, "
            "skipped=excluded.skipped, role_reason=excluded.role_reason, text_hash=excluded.text_hash, "
            "extractor_ver=excluded.extractor_ver",
            (rec.path, rec.blob, rec.lang, rec.role, rec.module, rec.size, rec.lines, rec.enc,
             rec.skipped, rec.role_reason, rec.text_hash, model.INVENTORY_VER),
        )

    def replace_inventory(self, files, rev: str, stats: model.ScanStats) -> None:
        """Записать инвентарь и убрать файлы, которых больше нет в ревизии."""
        self.conn.commit()
        self.begin()
        try:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS new_paths(path TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM new_paths")
            self.conn.executemany("INSERT OR IGNORE INTO new_paths(path) VALUES(?)", ((f.path,) for f in files))
            self.conn.execute("DELETE FROM files WHERE path NOT IN (SELECT path FROM new_paths)")
            self.conn.execute("DELETE FROM secrets WHERE path NOT IN (SELECT path FROM new_paths)")
            for rec in files:
                self.put_file(rec)
            self.conn.execute("DROP TABLE new_paths")
            self.meta_set("repo_rev", rev)
            self.meta_set("index_rev", rev)
            self.meta_set("schema_version", model.SCHEMA_VERSION)
            self.meta_set("inventory_ver", model.INVENTORY_VER)
            self.meta_set_json("stats", stats.__dict__)
            self.meta_set_json("languages", stats.languages)
            self.meta_set_json("skipped", stats.skipped)
            self.meta_set("files_total", stats.files)
            self.meta_set("files_indexed", stats.indexed)
            self.meta_set("files_parsed", stats.parsed)
            self.meta_set("files_reused", stats.reused)
            self.meta_set("bytes", stats.bytes)
            self.meta_set("lines", stats.lines)
            secrets_total = self.conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0]
            self.meta_set("secrets_count", int(secrets_total))
            self.meta_set("duration_s", f"{stats.duration_s:.3f}")
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ------------------------------------------------------------ секреты

    def put_secret(self, rule: str, path: str, line: int, mask: str) -> None:
        self.conn.execute(
            "INSERT INTO secrets(rule, path, line, mask) VALUES(?,?,?,?) "
            "ON CONFLICT(rule, path, line) DO UPDATE SET mask=excluded.mask",
            (rule, path, int(line), mask),
        )

    def clear_secrets(self, path: str) -> None:
        self.conn.execute("DELETE FROM secrets WHERE path=?", (path,))

    def secrets(self) -> list:
        rows = self.conn.execute("SELECT rule, path, line, mask FROM secrets ORDER BY path, line, rule")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ поисковые таблицы

    def put_path_terms(self, path: str, words) -> None:
        """Путь и его слова в поиске. Содержимое файла сюда не идёт: только имена и каталоги."""
        text = " ".join(dict.fromkeys(words))
        if not text:
            return
        if self.fts_words == "fts5-unicode61":
            self.conn.execute("DELETE FROM fts_words WHERE path=?", (path,))
            self.conn.execute("INSERT INTO fts_words(path, text) VALUES(?, ?)", (path, text))
        else:
            self.conn.execute("DELETE FROM tokens WHERE sym_id=? AND term LIKE 'path:%'", (path,))
            self.conn.executemany("INSERT INTO tokens(term, sym_id, weight) VALUES(?,?,1.0)",
                                  ((f"path:{w}", path) for w in dict.fromkeys(words)))
        if self.fts_ident == "fts5-trigram":
            self.conn.execute("DELETE FROM fts_ident WHERE path=?", (path,))
            self.conn.execute("INSERT INTO fts_ident(path, text) VALUES(?, ?)", (path, text))

    def clear_path_terms(self, path: str) -> None:
        if self.fts_words == "fts5-unicode61":
            self.conn.execute("DELETE FROM fts_words WHERE path=?", (path,))
        if self.fts_ident == "fts5-trigram":
            self.conn.execute("DELETE FROM fts_ident WHERE path=?", (path,))
        self.conn.execute("DELETE FROM tokens WHERE sym_id=? AND term LIKE 'path:%'", (path,))

    # ------------------------------------------------------------ сводка

    def counts(self) -> dict:
        files = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        indexed = self.conn.execute("SELECT COUNT(*) FROM files WHERE skipped=''").fetchone()[0]
        secrets = self.conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0]
        return {"files": int(files), "indexed": int(indexed), "secrets": int(secrets)}

    def status(self) -> dict:
        return {
            "path": str(self.path),
            "repo_rev": self.meta_get("repo_rev", ""),
            "index_rev": self.meta_get("index_rev", ""),
            "schema_version": self.meta_get("schema_version", ""),
            "inventory_ver": self.meta_get("inventory_ver", ""),
            "indexed_at": self.meta_get("indexed_at", ""),
            "duration_s": self.meta_get("duration_s", ""),
            "files_parsed": self.meta_get("files_parsed", ""),
            "files_reused": self.meta_get("files_reused", ""),
            "fts_words": self.fts_words,
            "fts_ident": self.fts_ident,
            "languages": self.meta_json("languages", {}),
            "skipped": self.meta_json("skipped", {}),
            **self.counts(),
        }

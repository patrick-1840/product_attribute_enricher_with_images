"""
Persistente toestand op schijf (SQLite in de cachemap).

Alles wat duur is (downloads, classificaties) en alles wat de gebruiker doet
(reviews) wordt per afbeelding meteen weggeschreven. Een crash of afsluiten
verliest dus hoogstens de requests die op dat moment liepen; bij de volgende
start gaat alles verder waar het stopte.

Tabellen:
- ``images``   — één rij per UNIEKE URL: download- en classificatieresultaat.
                 Dezelfde URL bij meerdere producten = één download, één call.
- ``items``    — één rij per (product, beeldslot): welke URL waar staat.
                 Wordt bij elke "Excel inlezen" volledig vernieuwd.
- ``products`` — contextinfo per product (merk, naam, …) voor de review.
- ``reviews``  — handmatige beslissingen per (product, slot). Blijven bewaard
                 over meerdere inleesbeurten heen, maar gelden alleen zolang de
                 URL in dat slot dezelfde is.
- ``attr_results`` — attribuutwaarde per (product, attribuut) bepaald op het
                 packshot met URL ``url``; plus manuele aanpassing en exportvinkje.
                 Een ander packshot (andere URL) = opnieuw bepalen.
- ``scope``    — gekozen productcategorieën (``mainCategory``, hoofdletters).
                 Downloaden, classificeren, review en packshotkeuze werken
                 alleen op producten in deze categorieën. ``meta.scope_active``
                 ontbreekt = geen keuze gemaakt = alle categorieën (CLI-gedrag).

Elke thread opent zijn eigen ``Store`` (eigen connectie); WAL-modus laat de
UI lezen terwijl een worker schrijft.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    url              TEXT PRIMARY KEY,
    thumb            TEXT,
    fetch_status     TEXT NOT NULL DEFAULT 'pending',   -- pending | ok | failed
    fetch_error      TEXT,
    fetch_attempts   INTEGER NOT NULL DEFAULT 0,
    fetched_at       TEXT,
    label            TEXT,
    confidence       REAL,
    model            TEXT,
    first_label      TEXT,     -- resultaat vóór escalatie (indien geëscaleerd)
    first_confidence REAL,
    first_model      TEXT,
    raw_response     TEXT,
    classify_error   TEXT,
    classified_at    TEXT
);
CREATE TABLE IF NOT EXISTS items (
    product_id TEXT NOT NULL,
    slot       TEXT NOT NULL,     -- "1".."N" of de naam van een extra kolom
    position   INTEGER NOT NULL,  -- weergavevolgorde binnen het product
    source_col TEXT NOT NULL,
    url        TEXT NOT NULL,
    PRIMARY KEY (product_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_items_url ON items(url);
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    row_order  INTEGER NOT NULL,
    info       TEXT,
    category   TEXT
);
CREATE TABLE IF NOT EXISTS scope (
    category TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS attr_results (
    product_id    TEXT NOT NULL,
    attribute_key TEXT NOT NULL,
    url           TEXT NOT NULL,
    choice        TEXT,            -- modelkeuze (gevalideerd of originele tekst)
    valid         INTEGER NOT NULL DEFAULT 1,   -- 0 = niet in de optielijst
    motivation    TEXT,
    model         TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    edited_value  TEXT,            -- manuele aanpassing in de resultatentabel
    approved      INTEGER,         -- exportvinkje (NULL = standaard: aan als er een geldige waarde is)
    PRIMARY KEY (product_id, attribute_key)
);
CREATE TABLE IF NOT EXISTS reviews (
    product_id  TEXT NOT NULL,
    slot        TEXT NOT NULL,
    url         TEXT NOT NULL,
    decision    TEXT NOT NULL,    -- accept | deny | relabel
    final_label TEXT,
    use         INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (product_id, slot)
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        # migratie: oudere caches hebben nog geen categoriekolom
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(products)")}
        if "category" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN category TEXT")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ categoriekeuze

    # Producten binnen de gekozen categorieën (of alle, zolang er niets gekozen is).
    _IN_SCOPE = ("it.product_id IN (SELECT p0.product_id FROM products p0 WHERE "
                 "(SELECT value FROM meta WHERE key='scope_active') IS NULL "
                 "OR p0.category IN (SELECT category FROM scope))")

    def scope(self) -> list[str] | None:
        """Gekozen categorieën; ``None`` = nog geen keuze gemaakt (= alles)."""
        if self.conn.execute("SELECT 1 FROM meta WHERE key='scope_active'").fetchone() is None:
            return None
        return [r[0] for r in self.conn.execute("SELECT category FROM scope ORDER BY category")]

    def set_scope(self, categories: list[str] | None) -> None:
        """``None`` = alle categorieën; een (lege) lijst = alleen die categorieën."""
        c = self.conn
        c.execute("BEGIN")
        try:
            c.execute("DELETE FROM scope")
            if categories is None:
                c.execute("DELETE FROM meta WHERE key='scope_active'")
            else:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('scope_active', '1')")
                c.executemany("INSERT OR IGNORE INTO scope(category) VALUES (?)",
                              ((str(x).strip().upper(),) for x in categories))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def category_overview(self) -> pd.DataFrame:
        """Per categorie: producten, unieke beelden, packshots en hoeveel daarvan
        al gedownload/geclassificeerd zijn. Basis voor de categoriekeuze."""
        q = f"""
        WITH first AS (
            SELECT it.product_id, it.url FROM items it WHERE {self._FIRST_SLOT}
        )
        SELECT COALESCE(p.category, '') AS category,
               COUNT(DISTINCT p.product_id) AS products,
               COUNT(DISTINCT it.url) AS images,
               COUNT(DISTINCT CASE WHEN im.fetch_status='ok' THEN it.url END) AS fetched,
               COUNT(DISTINCT CASE WHEN im.label IS NOT NULL THEN it.url END) AS classified,
               COUNT(DISTINCT f.url) AS packshots,
               COUNT(DISTINCT CASE WHEN f.url IS NOT NULL AND im.fetch_status='ok'
                                   THEN f.url END) AS packshots_fetched
        FROM products p
        LEFT JOIN items it ON it.product_id = p.product_id
        LEFT JOIN images im ON im.url = it.url
        LEFT JOIN first f ON f.product_id = it.product_id AND f.url = it.url
        GROUP BY COALESCE(p.category, '')
        ORDER BY products DESC
        """
        return pd.read_sql_query(q, self.conn)

    # ------------------------------------------------------------ inlezen

    def replace_items(self, items: pd.DataFrame, products: pd.DataFrame) -> None:
        """Vervangt de werklijst door een nieuwe inleesbeurt. Downloads,
        classificaties en reviews blijven bewaard (gekoppeld via URL)."""
        c = self.conn
        c.execute("BEGIN")
        try:
            c.execute("DELETE FROM items")
            c.execute("DELETE FROM products")
            c.executemany(
                "INSERT INTO items(product_id, slot, position, source_col, url) VALUES (?,?,?,?,?)",
                items[["product_id", "slot", "position", "source_col", "url"]]
                .itertuples(index=False, name=None),
            )
            c.executemany(
                "INSERT INTO products(product_id, row_order, info, category) VALUES (?,?,?,?)",
                ((r.product_id, int(r.row_order), json.dumps(r.info, ensure_ascii=False),
                  str(getattr(r, "category", "") or "").strip().upper())
                 for r in products.itertuples(index=False)),
            )
            c.executemany(
                "INSERT OR IGNORE INTO images(url) VALUES (?)",
                ((u,) for u in items["url"].unique()),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ downloaden

    _FIRST_SLOT = ("it.position = (SELECT MIN(i2.position) FROM items i2 "
                   "WHERE i2.product_id = it.product_id)")

    def urls_to_fetch(self, retry_failed: bool = False, only_first: bool = False) -> list[str]:
        """Te downloaden URL's. ``only_first``: enkel het eerste beeld per
        product (het packshot in modus "first")."""
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        q = (f"SELECT DISTINCT i.url FROM images i JOIN items it ON it.url = i.url "
             f"WHERE i.fetch_status IN ({','.join('?' * len(statuses))}) AND {self._IN_SCOPE}")
        if only_first:
            q += " AND " + self._FIRST_SLOT
        return [r[0] for r in self.conn.execute(q, statuses)]

    def first_images(self, scoped: bool = True) -> pd.DataFrame:
        """Eerste beeld per product met downloadstatus."""
        q = ("SELECT it.product_id, it.slot, it.url, im.thumb, im.fetch_status "
             "FROM items it JOIN images im ON im.url = it.url WHERE " + self._FIRST_SLOT)
        if scoped:
            q += " AND " + self._IN_SCOPE
        return pd.read_sql_query(q, self.conn)

    def mark_fetched(self, url: str, thumb: str) -> None:
        self.conn.execute(
            "UPDATE images SET fetch_status='ok', thumb=?, fetch_error=NULL, "
            "fetch_attempts=fetch_attempts+1, fetched_at=? WHERE url=?",
            (thumb, _now(), url))

    def mark_fetch_failed(self, url: str, error: str) -> None:
        self.conn.execute(
            "UPDATE images SET fetch_status='failed', fetch_error=?, "
            "fetch_attempts=fetch_attempts+1, fetched_at=? WHERE url=?",
            (error[:500], _now(), url))

    def reset_missing_thumbs(self, thumbs_dir: Path) -> int:
        """Zet beelden waarvan de thumbnail verdwenen is terug op 'pending'
        (bv. na het wissen of verplaatsen van de cachemap)."""
        rows = self.conn.execute(
            "SELECT url, thumb FROM images WHERE fetch_status='ok'").fetchall()
        missing = [r["url"] for r in rows if not (Path(thumbs_dir) / (r["thumb"] or "")).is_file()]
        self.conn.executemany(
            "UPDATE images SET fetch_status='pending', thumb=NULL WHERE url=?",
            ((u,) for u in missing))
        return len(missing)

    # ------------------------------------------------------------ classificeren

    def urls_to_classify(self, reclassify_errors: bool = True) -> list[tuple[str, str]]:
        """(url, thumb) van gedownloade beelden zonder label (binnen de categoriekeuze)."""
        q = ("SELECT DISTINCT i.url, i.thumb FROM images i JOIN items it ON it.url = i.url "
             f"WHERE i.fetch_status='ok' AND i.label IS NULL AND {self._IN_SCOPE}")
        if not reclassify_errors:
            q += " AND i.classify_error IS NULL"
        return [(r[0], r[1]) for r in self.conn.execute(q)]

    def save_classification(self, url: str, label: str, confidence: float, model: str,
                            raw: str, first: tuple[str, float, str] | None = None) -> None:
        f_label, f_conf, f_model = first or (None, None, None)
        self.conn.execute(
            "UPDATE images SET label=?, confidence=?, model=?, raw_response=?, "
            "classify_error=NULL, classified_at=?, first_label=?, first_confidence=?, "
            "first_model=? WHERE url=?",
            (label, confidence, model, raw[:1000], _now(), f_label, f_conf, f_model, url))

    def mark_classify_failed(self, url: str, error: str) -> None:
        self.conn.execute(
            "UPDATE images SET classify_error=?, classified_at=? WHERE url=?",
            (error[:500], _now(), url))

    # ------------------------------------------------------------ review

    def set_review(self, product_id: str, slot: str, url: str, decision: str,
                   final_label: str | None, use: bool) -> None:
        self.conn.execute(
            "INSERT INTO reviews(product_id, slot, url, decision, final_label, use, reviewed_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(product_id, slot) DO UPDATE SET "
            "url=excluded.url, decision=excluded.decision, final_label=excluded.final_label, "
            "use=excluded.use, reviewed_at=excluded.reviewed_at",
            (product_id, slot, url, decision, final_label, int(use), _now()))

    def clear_review(self, product_id: str, slot: str) -> None:
        self.conn.execute("DELETE FROM reviews WHERE product_id=? AND slot=?", (product_id, slot))

    # ------------------------------------------------------------ attributen

    def save_attr_result(self, product_id: str, attr: str, url: str, choice: str | None,
                         valid: bool, motivation: str | None, model: str,
                         error: str | None = None) -> None:
        """Nieuw modelresultaat; een eerdere manuele aanpassing vervalt."""
        self.conn.execute(
            "INSERT INTO attr_results(product_id, attribute_key, url, choice, valid, motivation, "
            "model, error, created_at, edited_value, approved) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL) "
            "ON CONFLICT(product_id, attribute_key) DO UPDATE SET url=excluded.url, "
            "choice=excluded.choice, valid=excluded.valid, motivation=excluded.motivation, "
            "model=excluded.model, error=excluded.error, created_at=excluded.created_at, "
            "edited_value=NULL, approved=NULL",
            (product_id, attr, url, choice, int(valid), motivation, model, error, _now()))

    def set_attr_edit(self, product_id: str, attr: str, edited_value: str | None,
                      approved: bool | None) -> None:
        self.conn.execute(
            "UPDATE attr_results SET edited_value=?, approved=? WHERE product_id=? AND attribute_key=?",
            (edited_value, None if approved is None else int(approved), product_id, attr))

    def attr_results(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM attr_results", self.conn)

    def clear_simulated_attr_results(self, sim_model: str) -> int:
        return self.conn.execute("DELETE FROM attr_results WHERE model=?", (sim_model,)).rowcount

    # ------------------------------------------------------------ overzicht

    def item_table(self, scoped: bool = True) -> pd.DataFrame:
        """Platte tabel (één rij per product × slot) met download-,
        classificatie- en reviewstatus. Basis voor review en export.
        ``scoped``: alleen producten binnen de categoriekeuze."""
        where = f"WHERE {self._IN_SCOPE}" if scoped else ""
        q = f"""
        SELECT it.product_id, it.slot, it.position, it.source_col, it.url,
               p.row_order, p.info,
               im.thumb, im.fetch_status, im.fetch_error,
               im.label, im.confidence, im.model,
               im.first_label, im.first_confidence, im.classify_error,
               rv.decision, rv.final_label, rv.use AS review_use, rv.reviewed_at
        FROM items it
        JOIN products p ON p.product_id = it.product_id
        JOIN images im ON im.url = it.url
        LEFT JOIN reviews rv ON rv.product_id = it.product_id AND rv.slot = it.slot
                              AND rv.url = it.url
        {where}
        ORDER BY p.row_order, it.position
        """
        return pd.read_sql_query(q, self.conn)

    def stats(self) -> dict[str, int]:
        """Tellingen binnen de categoriekeuze (``products_total`` = hele bestand)."""
        one = lambda q: self.conn.execute(q).fetchone()[0]  # noqa: E731
        scoped_items = f"SELECT it.url FROM items it WHERE {self._IN_SCOPE}"
        live = f"FROM images i WHERE i.url IN ({scoped_items})"
        return {
            "products_total": one("SELECT COUNT(*) FROM products"),
            # alias "it" zodat _IN_SCOPE ook op de productentabel werkt
            "products": one(f"SELECT COUNT(*) FROM products it WHERE {self._IN_SCOPE}"),
            "items": one(f"SELECT COUNT(*) FROM items it WHERE {self._IN_SCOPE}"),
            "unique_urls": one(f"SELECT COUNT(*) {live}"),
            "fetched": one(f"SELECT COUNT(*) {live} AND i.fetch_status='ok'"),
            "fetch_failed": one(f"SELECT COUNT(*) {live} AND i.fetch_status='failed'"),
            "classified": one(f"SELECT COUNT(*) {live} AND i.label IS NOT NULL"),
            "classify_failed": one(
                f"SELECT COUNT(*) {live} AND i.label IS NULL AND i.classify_error IS NOT NULL"),
            "reviewed": one(
                "SELECT COUNT(*) FROM reviews r JOIN items it ON it.product_id=r.product_id "
                f"AND it.slot=r.slot AND it.url=r.url WHERE {self._IN_SCOPE}"),
        }

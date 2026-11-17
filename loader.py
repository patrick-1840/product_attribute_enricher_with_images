"""
Inlezen van de productlijst en opbouwen van de platte beeldtabel.

Invoer: hetzelfde brede Excel-bestand als de Product Attribute Enricher (één
rij per variant). Beeldkolommen worden automatisch herkend:

- ``product image 1`` … ``product image N``  (naamgeving uit de spec)
- ``imageLink1`` … ``imageLinkN``             (naamgeving in de huidige export)
- plus de extra kolommen uit ``ClassifierConfig.extra_image_columns``
  (standaard ``mainImageLink``).

Uitvoer: een tabel ``product_id, slot, position, source_col, url``. Lege
cellen en ongeldige URL's worden overgeslagen (en geteld in het rapport).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from config import ClassifierConfig

logger = logging.getLogger(__name__)

# "product image 3", "Product_Image3", "imageLink3", "image link 3", "image 3"
IMAGE_COL_RE = re.compile(r"^\s*(?:product[\s_]*image|image[\s_]*link|image)[\s_]*(\d+)\s*$",
                          re.IGNORECASE)


@dataclass
class ImageColumn:
    source_col: str
    slot: str          # "1".."N" of de kolomnaam voor extra kolommen
    position: int      # weergavevolgorde


@dataclass
class LoadReport:
    rows: int = 0
    products: int = 0
    images: int = 0
    unique_urls: int = 0
    empty_cells: int = 0
    invalid_urls: list[tuple[str, str, str]] = field(default_factory=list)  # (product, kolom, waarde)
    duplicate_ids: int = 0
    columns: list[ImageColumn] = field(default_factory=list)

    def summary(self) -> str:
        cols = ", ".join(c.source_col for c in self.columns)
        return (f"{self.rows} rijen, {self.products} producten, {self.images} beeldverwijzingen "
                f"({self.unique_urls} unieke URL's). Kolommen: {cols}. "
                f"{len(self.invalid_urls)} ongeldige URL's overgeslagen"
                + (f", {self.duplicate_ids} dubbele product-id's samengevoegd" if self.duplicate_ids else "")
                + ".")


def detect_image_columns(columns: list[str], extra: list[str] | None = None) -> list[ImageColumn]:
    """Zoekt de beeldkolommen, gesorteerd: extra kolommen (bv. hoofdbeeld)
    eerst, daarna de genummerde in numerieke volgorde."""
    numbered: list[tuple[int, str]] = []
    for col in columns:
        m = IMAGE_COL_RE.match(str(col))
        if m:
            numbered.append((int(m.group(1)), str(col)))
    numbered.sort()
    # Twee schema's tegelijk (bv. "product image 1" én "imageLink1")? Dan
    # krijgt het tweede een uniek slot zodat niets overschreven wordt.
    result: list[ImageColumn] = []
    used_slots: set[str] = set()
    pos = 0
    for col in extra or []:
        if col in columns:
            result.append(ImageColumn(col, col, pos))
            used_slots.add(col)
            pos += 1
    for n, col in numbered:
        slot = str(n) if str(n) not in used_slots else col
        result.append(ImageColumn(col, slot, pos))
        used_slots.add(slot)
        pos += 1
    return result


def is_valid_url(value: str) -> bool:
    try:
        p = urlparse(value)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc) and " " not in value


def read_product_file(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Productbestand niet gevonden: {path}")
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.read_excel(path, dtype=str, keep_default_na=False)


def build_image_table(df: pd.DataFrame, cfg: ClassifierConfig
                      ) -> tuple[pd.DataFrame, pd.DataFrame, LoadReport]:
    """Bouwt (items, products, rapport) uit het brede productbestand."""
    if cfg.id_column not in df.columns:
        raise ValueError(f"Kolom '{cfg.id_column}' (product-id) niet gevonden in het bestand. "
                         f"Pas de id-kolom aan in de instellingen.")
    cols = detect_image_columns(list(df.columns), cfg.extra_image_columns)
    if not cols:
        raise ValueError("Geen beeldkolommen gevonden (verwacht 'product image N' of 'imageLinkN').")

    report = LoadReport(rows=len(df), columns=cols)
    info_cols = [c for c in cfg.info_columns if c in df.columns]

    items: list[tuple[str, str, int, str, str]] = []
    products: dict[str, tuple[int, dict]] = {}
    seen: set[tuple[str, str]] = set()
    for row_order, row in enumerate(df.itertuples(index=False)):
        rec = dict(zip(df.columns, row))
        pid = str(rec[cfg.id_column]).strip()
        if not pid:
            continue
        if pid in products:
            report.duplicate_ids += 1
        else:
            products[pid] = (row_order, {c: str(rec[c]) for c in info_cols})
        for col in cols:
            raw = str(rec.get(col.source_col) or "").strip()
            if not raw or raw.lower() in ("nan", "none"):
                report.empty_cells += 1
                continue
            if not is_valid_url(raw):
                report.invalid_urls.append((pid, col.source_col, raw[:200]))
                continue
            if (pid, col.slot) in seen:
                continue
            seen.add((pid, col.slot))
            items.append((pid, col.slot, col.position, col.source_col, raw))

    items_df = pd.DataFrame(items, columns=["product_id", "slot", "position", "source_col", "url"])
    products_df = pd.DataFrame(
        [(pid, o, info) for pid, (o, info) in products.items()],
        columns=["product_id", "row_order", "info"])
    report.products = len(products_df)
    report.images = len(items_df)
    report.unique_urls = items_df["url"].nunique() if len(items_df) else 0
    if report.invalid_urls:
        logger.warning("%d ongeldige URL's overgeslagen, bv.: %s",
                       len(report.invalid_urls), report.invalid_urls[:3])
    logger.info("Ingelezen: %s", report.summary())
    return items_df, products_df, report

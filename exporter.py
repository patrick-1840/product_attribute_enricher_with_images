"""
Export: het originele Excel terugschrijven met per beeldslot twee extra kolommen,
plus een apart logbestand met de handmatige reviews (trainingsdata).

Kolomnamen: ``image N label`` / ``image N use`` voor genummerde slots, en
``<kolomnaam> label`` / ``<kolomnaam> use`` voor extra kolommen (bv.
``mainImageLink label``).

Effectief label en vinkje per beeld (ook gebruikt door de review-GUI):
- gereviewd  → het label en vinkje uit de review;
- niet gereviewd → het voorgestelde label; vinkje aan als het een packshot is.
- deny zonder nieuw label → label ``onbekend``, vinkje uit (tenzij manueel aangezet).

Het originele bestand wordt NOOIT overschreven; de gebruiker kiest via
"Exporteer als…" een nieuwe bestandsnaam.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from config import USE_LABEL, ClassifierConfig
from store import Store

logger = logging.getLogger(__name__)

UNKNOWN_LABEL = "onbekend"


def slot_prefix(slot: str) -> str:
    return f"image {slot}" if slot.isdigit() else slot


def effective_label(row) -> str:
    if isinstance(row.get("decision"), str):
        return row.get("final_label") or UNKNOWN_LABEL
    if row.get("fetch_status") == "failed":
        return "download mislukt"
    return row.get("label") or ""


def effective_use(row) -> bool | None:
    """None = nog geen voorstel en geen review (export laat de cel leeg)."""
    if isinstance(row.get("decision"), str):
        return bool(row.get("review_use"))
    if not isinstance(row.get("label"), str):
        return None
    return row.get("label") == USE_LABEL


def add_effective_columns(table: pd.DataFrame) -> pd.DataFrame:
    table = table.copy()
    recs = table.to_dict("records")
    table["eff_label"] = [effective_label(r) for r in recs]
    table["eff_use"] = [effective_use(r) for r in recs]
    return table


def _ordered_slots(table: pd.DataFrame) -> list[str]:
    order = table.groupby("slot")["position"].min().sort_values()
    return list(order.index)


def export_excel(cfg: ClassifierConfig, source: Path, target: Path) -> tuple[int, Path]:
    """Schrijft ``source`` + labelkolommen naar ``target``. Geeft (aantal
    gematchte rijen, pad van het correctielog) terug."""
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError("Kies een andere bestandsnaam: het originele bestand wordt niet overschreven.")
    store = Store(cfg.db_path)
    try:
        table = add_effective_columns(store.item_table())
    finally:
        store.close()
    if table.empty:
        raise ValueError("Geen beelden in de werklijst; lees eerst het Excel-bestand in.")

    slots = _ordered_slots(table)
    lookup = {(r.product_id, r.slot): (r.eff_label or None,
                                       None if r.eff_use is None else bool(r.eff_use))
              for r in table[["product_id", "slot", "eff_label", "eff_use"]].itertuples(index=False)}
    new_cols = [c for s in slots for c in (f"{slot_prefix(s)} label", f"{slot_prefix(s)} use")]

    if source.suffix.lower() == ".csv":
        df = pd.read_csv(source, dtype=str, keep_default_na=False)
        ids = df[cfg.id_column].astype(str).str.strip()
        for s in slots:
            p = slot_prefix(s)
            df[f"{p} label"] = [lookup.get((i, s), ("", None))[0] for i in ids]
            df[f"{p} use"] = [lookup.get((i, s), ("", None))[1] for i in ids]
        matched = int(ids.isin(table["product_id"]).sum())
        df.to_csv(target, index=False, sep=",", encoding="utf-8-sig")
    else:
        # openpyxl i.p.v. pandas: behoudt opmaak, celtypes en andere tabbladen.
        wb = load_workbook(source)
        ws = wb.worksheets[0]
        header = [c.value for c in ws[1]]
        if cfg.id_column not in header:
            raise ValueError(f"Kolom '{cfg.id_column}' niet gevonden in {source.name}.")
        id_idx = header.index(cfg.id_column) + 1
        # Bestaande labelkolommen (bv. van een vorige export) overschrijven i.p.v. dupliceren.
        col_of: dict[str, int] = {}
        next_col = ws.max_column + 1
        for name in new_cols:
            if name in header:
                col_of[name] = header.index(name) + 1
            else:
                col_of[name] = next_col
                ws.cell(row=1, column=next_col, value=name)
                next_col += 1
        matched = 0
        for r in range(2, ws.max_row + 1):
            pid = ws.cell(row=r, column=id_idx).value
            if pid is None:
                continue
            pid = str(pid).strip()
            hit = False
            for s in slots:
                p = slot_prefix(s)
                val = lookup.get((pid, s))
                if val:
                    hit = True
                ws.cell(row=r, column=col_of[f"{p} label"], value=val[0] if val else None)
                ws.cell(row=r, column=col_of[f"{p} use"], value=val[1] if val else None)
            matched += hit
        wb.save(target)

    log_path = target.with_name(f"{target.stem}_correcties.csv")
    export_corrections(table, log_path)
    logger.info("Export klaar: %s (%d rijen met beelden), correctielog: %s", target, matched, log_path)
    return matched, log_path


def export_corrections(table: pd.DataFrame, path: Path) -> int:
    """Alle handmatig gereviewde beelden, met voorstel én eindbeslissing.
    ``gecorrigeerd`` = de mens week af van het voorstel (label of vinkje)."""
    rv = table[table["decision"].notna()].copy()
    if rv.empty:
        pd.DataFrame(columns=["product_id"]).to_csv(path, index=False, encoding="utf-8-sig")
        return 0
    rv["voorgesteld_use"] = rv["label"] == USE_LABEL
    rv["gecorrigeerd"] = (rv["eff_label"] != rv["label"]) | (rv["eff_use"] != rv["voorgesteld_use"])
    out = rv.rename(columns={
        "label": "voorgesteld_label", "confidence": "voorgesteld_confidence",
        "model": "model", "decision": "beslissing", "eff_label": "finaal_label",
        "eff_use": "finaal_use", "reviewed_at": "gereviewd_op", "thumb": "thumbnail",
    })[["product_id", "slot", "source_col", "url", "thumbnail", "voorgesteld_label",
        "voorgesteld_confidence", "model", "beslissing", "finaal_label", "voorgesteld_use",
        "finaal_use", "gecorrigeerd", "gereviewd_op"]]
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return len(out)

"""
Referentiedata voor de attribuutverrijking: attribuuttypes, toegelaten waarden
(optielijsten), label→SAP-code en de categorie/attribuutmatrix. Plus het
opbouwen van de werklijst en de SAP/impex-vertaling.

OVERGENOMEN uit de Product Attribute Enricher (``data_loader.py`` en
``exporter.py``), zodat dit programma voorlopig los kan draaien. Bij integratie
van beide tools moet dit weer één module worden — wijzig de logica hier dus
alleen samen met het origineel (zelfde bronbestanden, zelfde impexformaat).
"""
from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass, field

import pandas as pd

from config import DEFAULT_CATEGORY_MAPPING, MULTI_VALUE_HINT_ATTRIBUTES, ClassifierConfig

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", message="Workbook contains no default style",
                        category=UserWarning, module="openpyxl")

CATEGORY_COLUMN = "mainCategory"
PRODUCT_KEY_COLUMNS = [
    "baseProduct", "variant_code", "ean", "brand", "productName", "productUrl",
    CATEGORY_COLUMN,
]


@dataclass
class ReferenceData:
    """Alle referentie-info over attributen; sleutels lowercase."""
    canonical_names: dict[str, str] = field(default_factory=dict)
    attribute_types: dict[str, str] = field(default_factory=dict)      # single|multi|boolean
    options: dict[str, list[str]] = field(default_factory=dict)        # Engelse labels
    label_to_sap: dict[str, dict[str, str]] = field(default_factory=dict)
    category_matrix: dict[str, set[str]] = field(default_factory=dict)

    def display_name(self, attr: str) -> str:
        return self.canonical_names.get(attr, attr)

    def attribute_type(self, attr: str) -> str:
        return self.attribute_types.get(attr, "multi")

    def options_for(self, attr: str) -> list[str]:
        return self.options.get(attr, [])

    def is_multi(self, attr: str) -> bool:
        """Meerdere waarden toegelaten? 'multi' in het typebestand of in de hintlijst."""
        return attr in MULTI_VALUE_HINT_ATTRIBUTES or self.attribute_type(attr) == "multi"

    def allowed_attributes_for_category(self, main_category: str) -> set[str]:
        """Attributen die volgens de matrix bij deze categorie horen én opties hebben."""
        cols = ["Alle producten"] + DEFAULT_CATEGORY_MAPPING.get(main_category.upper(), [])
        attrs: set[str] = set()
        for col in cols:
            attrs |= self.category_matrix.get(col, set())
        return {a for a in attrs if self.options.get(a)}


def load_reference_data(cfg: ClassifierConfig) -> ReferenceData:
    for p in (cfg.attribute_types_file, cfg.sap_mapping_file):
        if not p.exists():
            raise FileNotFoundError(f"Referentiebestand niet gevonden: {p}\n"
                                    "Kies het juiste bestand bij Instellingen.")
    ref = ReferenceData()

    types_df = pd.read_excel(cfg.attribute_types_file, sheet_name="Features")
    types_df.columns = types_df.columns.str.strip()
    for _, row in types_df.iterrows():
        ident = str(row.get("Identifier") or "").strip()
        multival = str(row.get("multival") or "").strip().lower()
        if ident:
            ref.attribute_types[ident.lower()] = multival or "multi"
            ref.canonical_names.setdefault(ident.lower(), ident)

    sap_df = pd.read_excel(cfg.sap_mapping_file, sheet_name="Alle SAP attribuut waarden")
    sap_df.columns = sap_df.columns.str.strip()
    for _, row in sap_df.iterrows():
        attr = str(row["sap_commerce_attribute_name"]).strip()
        code = str(row["sap_commerce_attribute_value"]).strip()
        label = str(row["label_en"]).strip()
        if not attr or not label or label.lower() == "nan":
            continue
        key = attr.lower()
        ref.canonical_names.setdefault(key, attr)
        ref.options.setdefault(key, [])
        if label not in ref.options[key]:
            ref.options[key].append(label)
        ref.label_to_sap.setdefault(key, {})[label.lower()] = code

    matrix_df = pd.read_excel(cfg.sap_mapping_file, sheet_name="category 1 attributes (2)")
    matrix_df.columns = matrix_df.columns.str.strip()
    for col in [c for c in matrix_df.columns if c not in ("Attribuut", "alle attributen")]:
        marked = matrix_df.loc[matrix_df[col].astype(str).str.strip().str.lower() == "x", "Attribuut"]
        ref.category_matrix[col] = {str(a).strip().lower() for a in marked}

    logger.info("Referentiedata geladen: %d attribuuttypes, %d attributen met opties, "
                "%d categoriekolommen", len(ref.attribute_types), len(ref.options),
                len(ref.category_matrix))
    return ref


# ------------------------------------------------------------------ werklijst

def _resolve_column(catalog: pd.DataFrame, attr: str) -> str | None:
    for col in catalog.columns:
        if str(col).strip().lower() == attr:
            return col
    return None


def _is_empty(series: pd.Series) -> pd.Series:
    return series.isna() | (series.astype(str).str.strip().isin(["", "nan", "None"]))


def fill_rates(catalog: pd.DataFrame, ref: ReferenceData) -> dict[tuple[str, str], float]:
    """Vulgraad (%) per (categorie, attribuut)."""
    out: dict[tuple[str, str], float] = {}
    cols = {str(c).strip().lower(): c for c in catalog.columns}
    for category, group in catalog.groupby(CATEGORY_COLUMN):
        total = len(group)
        for attr in ref.allowed_attributes_for_category(str(category)):
            col = cols.get(attr)
            filled = 0 if col is None else int((~_is_empty(group[col])).sum())
            out[(str(category), attr)] = round(100 * filled / total, 1) if total else 0.0
    return out


def build_work_items(catalog: pd.DataFrame, ref: ReferenceData,
                     selection: dict[str, list[str]], only_empty: bool,
                     brands: list[str] | None = None) -> pd.DataFrame:
    """Lange werklijst: één rij per (variant × attribuut). single/boolean-
    attributen worden per baseProduct ontdubbeld (zoals in de verrijker)."""
    frames = []
    key_cols = [c for c in PRODUCT_KEY_COLUMNS if c in catalog.columns]
    for category, attrs in selection.items():
        rows = catalog[catalog[CATEGORY_COLUMN].astype(str).str.upper() == category.upper()]
        if brands and "brand" in rows.columns:
            wanted = {b.strip().casefold() for b in brands}
            rows = rows[rows["brand"].astype(str).str.strip().str.casefold().isin(wanted)]
        if rows.empty:
            continue
        for attr in attrs:
            if not ref.options_for(attr):
                continue
            col = _resolve_column(rows, attr)
            current = rows[col] if col is not None else pd.Series("", index=rows.index)
            work = rows[key_cols].copy()
            work["Product attribute"] = ref.display_name(attr)
            work["attribute_key"] = attr
            work["attribute_type"] = ref.attribute_type(attr)
            work["current value"] = current.values
            if only_empty:
                work = work[_is_empty(work["current value"])]
            frames.append(work)
    if not frames:
        return pd.DataFrame()
    items = pd.concat(frames, ignore_index=True)
    items["variant_code"] = items["variant_code"].astype(str).str.strip()
    items = deduplicate_single_value_attributes(items)
    return items.reset_index(drop=True)


def deduplicate_single_value_attributes(items: pd.DataFrame) -> pd.DataFrame:
    if items.empty or "baseProduct" not in items.columns:
        return items
    mask = items["attribute_type"].isin(["single", "boolean"])
    singles, others = items[mask].copy(), items[~mask]
    if singles.empty:
        return items
    singles["_main"] = singles["baseProduct"].astype(str) == "BP_" + singles["variant_code"].astype(str)
    singles = singles.sort_values(["baseProduct", "attribute_key", "_main", "variant_code"],
                                  ascending=[True, True, False, True])
    singles = singles.drop_duplicates(subset=["baseProduct", "attribute_key"], keep="first")
    return pd.concat([singles.drop(columns="_main"), others], ignore_index=True)


# ------------------------------------------------------------------ waarden

def validate_choice(choice, options: list[str]) -> tuple[str | None, bool]:
    """Controleert of de gekozen waarde(n) letterlijk in de optielijst staan.
    Geeft (gevalideerde keuze of originele tekst, geldig?) terug."""
    if choice is None or (isinstance(choice, str) and not choice.strip()):
        return None, True
    if isinstance(choice, list):
        choice = ", ".join(str(c) for c in choice)
    normalized = {opt.strip().lower(): opt for opt in options}
    parts = [p.strip() for p in re.split(r",\s*", str(choice)) if p.strip()]
    if parts and all(p.lower() in normalized for p in parts):
        return ", ".join(dict.fromkeys(normalized[p.lower()] for p in parts)), True
    return str(choice), False


def sap_codes(ref: ReferenceData, attr: str, value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or not str(value).strip():
        return ""
    mapping = ref.label_to_sap.get(attr, {})
    codes = [mapping.get(p.strip().lower()) for p in str(value).split(",")]
    return ", ".join(c for c in codes if c)


def build_impex(rows: pd.DataFrame, catalog: str) -> str:
    """Impex-regels per attribuut, zelfde formaat als de attribuutverrijker:
    ``;{variant_code};$productCatalog:{catalog}; $lang:,{code1},{code2}``"""
    lines: list[str] = []
    rows = rows[rows["new_sap_values"].astype(str).str.strip() != ""]
    for attribute, group in rows.groupby("Product attribute"):
        lines.append(f"# --- {attribute} ({len(group)} products) ---")
        for _, r in group.iterrows():
            codes = str(r["new_sap_values"]).replace(", ", ",")
            lines.append(f";{r['variant_code']};$productCatalog:{catalog}; $lang:,{codes}")
        lines.append("")
    return "\n".join(lines)

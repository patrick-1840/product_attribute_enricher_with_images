"""
Tabs voor de visuele attribuutverrijking.

``AttributeTab``  — kies per categorie welke attributen gevuld worden (zelfde
                    boom als in de attribuutverrijker: alleen attributen die in
                    de SAP-matrix bij de categorie horen, met vulgraad), bekijk
                    per attribuut de toegelaten waarden, bouw de werklijst en
                    start de verrijking op het packshot.
``ResultsTab``    — controleer de gegenereerde waarden naast het packshot, pas
                    aan (dubbelklik een toegelaten waarde of typ), vink aan wat
                    mee moet, en exporteer naar Excel en impex.

Beide tabs krijgen een ``host`` (het hoofdvenster) mee dat de achtergrondtaken
beheert (één taak tegelijk, zie ``MainWindow._start``).
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import pandas as pd

from config import VISUAL_ATTRIBUTES, ClassifierConfig
from qt_common import StepBox
from reference import CATEGORY_COLUMN, ReferenceData, fill_rates

logger = logging.getLogger(__name__)


# ======================================================================= selectie

class AttributeTab(QWidget):
    work_ready = Signal()          # nieuwe werklijst/resultaten → resultatentab verversen

    def __init__(self, cfg: ClassifierConfig, host, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.host = host
        self.catalog: pd.DataFrame | None = None
        self.ref: ReferenceData | None = None
        self.work: pd.DataFrame | None = None      # werklijst + packshot
        self.rates: dict[tuple[str, str], float] = {}

        self.info = QLabel("Lees eerst het productbestand in (tab 1). De referentiebestanden "
                           "(attribuuttypes en SAP-mapping) kies je bij Instellingen.")
        self.info.setWordWrap(True)

        # --- boom + optielijst
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Categorie / attribuut", "Type", "Opties", "% gevuld"])
        self.tree.setColumnWidth(0, 360)
        self.tree.currentItemChanged.connect(self._show_options)
        self.options = QListWidget()
        self.options_title = QLabel("<b>Toegelaten waarden</b><br><span style='color:#888'>"
                                    "Klik een attribuut aan. Het model mag ALLEEN deze waarden "
                                    "teruggeven.</span>")
        self.options_title.setWordWrap(True)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.options_title)
        rl.addWidget(self.options, 1)
        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(right)
        split.setSizes([700, 320])

        sel = QGroupBox("Selectie — vink per categorie aan welke attributen gevuld worden "
                        "(👁 = visueel attribuut, standaard aangevinkt)")
        sl = QVBoxLayout(sel)
        sl.addWidget(split, 1)
        row = QHBoxLayout()
        for text, fn in (("Alleen visuele", self._check_visual), ("Alles aan", lambda: self._check_all(True)),
                         ("Alles uit", lambda: self._check_all(False))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        row.addWidget(QLabel("Merkfilter (kommagescheiden, leeg = alle):"))
        self.brand_edit = QLineEdit()
        self.brand_edit.setMinimumWidth(260)
        self.brand_edit.returnPressed.connect(self.refresh_tree)
        row.addWidget(self.brand_edit)
        b = QPushButton("Toepassen")
        b.clicked.connect(self.refresh_tree)
        row.addWidget(b)
        sl.addLayout(row)

        # --- werklijst
        wl = QGroupBox("Werklijst")
        wll = QHBoxLayout(wl)
        self.chk_empty = QCheckBox("alleen lege waarden aanvullen")
        self.chk_empty.setChecked(cfg.only_empty_values)
        self.btn_build = QPushButton("Werklijst opbouwen")
        self.btn_build.clicked.connect(self.build_work)
        self.work_label = QLabel("Nog geen werklijst.")
        self.work_label.setWordWrap(True)
        wll.addWidget(self.chk_empty)
        wll.addWidget(self.btn_build)
        wll.addWidget(self.work_label, 1)

        # --- run
        self.box = StepBox("Attributen bepalen op het packshot", "Start verrijking")
        self.chk_sim = QCheckBox("simulatiemodus (geen API-kosten)")
        self.chk_redo = QCheckBox("ook reeds verwerkte opnieuw")
        self.sp_limit = QSpinBox(minimum=0, maximum=1_000_000, specialValueText="geen limiet")
        self.sp_limit.setToolTip("Max. aantal producten (= API-calls) in deze run. 0 = alles.")
        self.lbl_model = QLabel()
        for w in (self.chk_sim, self.chk_redo, QLabel("limiet producten:"), self.sp_limit, self.lbl_model):
            self.box.options.addWidget(w)
        self.box.btn_start.clicked.connect(self.start)
        self.box.btn_pause.clicked.connect(lambda: host._toggle_pause(self.box))
        self.box.btn_stop.clicked.connect(host._stop)

        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addWidget(sel, 1)
        lay.addWidget(wl)
        lay.addWidget(self.box)
        self.update_model_label()

    # ------------------------------------------------------------ data

    def set_data(self, catalog: pd.DataFrame | None, ref: ReferenceData | None, ref_error: str = ""):
        self.catalog, self.ref = catalog, ref
        self.work = None
        if catalog is not None and ref is not None:
            self.info.setText(f"{len(catalog)} producten ingelezen · {len(ref.options)} attributen "
                              f"met toegelaten waarden. Packshot = "
                              + ("eerste beeld per product." if self.cfg.packshot_mode == "first"
                                 else "eerste beeld met vinkje 'gebruik' (review)."))
            self.refresh_tree()
        elif ref_error:
            self.info.setText(f"⚠️ Referentiebestanden niet geladen: {ref_error}")

    def update_model_label(self):
        self.lbl_model.setText(f"model: {self.cfg.attribute_model} (detail {self.cfg.attribute_detail})")

    def _brands(self) -> list[str] | None:
        return [b.strip() for b in self.brand_edit.text().split(",") if b.strip()] or None

    def _filtered_catalog(self) -> pd.DataFrame:
        brands = self._brands()
        if not brands or "brand" not in self.catalog.columns:
            return self.catalog
        wanted = {b.casefold() for b in brands}
        return self.catalog[self.catalog["brand"].astype(str).str.strip().str.casefold().isin(wanted)]

    def refresh_tree(self):
        if self.catalog is None or self.ref is None:
            return
        if CATEGORY_COLUMN not in self.catalog.columns:
            QMessageBox.warning(self, "Kolom ontbreekt", f"Kolom '{CATEGORY_COLUMN}' niet gevonden.")
            return
        # bestaande vinkjes onthouden
        previous = self._selection() if self.tree.topLevelItemCount() else None
        cat = self._filtered_catalog()
        self.rates = fill_rates(cat, self.ref)
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)
        counts = cat[CATEGORY_COLUMN].astype(str).value_counts()
        for category in sorted(c for c in counts.index if c.strip()):
            top = QTreeWidgetItem([f"{category}  ({counts[category]} producten)", "", "", ""])
            top.setFont(0, bold)
            top.setFlags(top.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable)
            top.setData(0, Qt.UserRole, category)
            self.tree.addTopLevelItem(top)
            allowed = self.ref.allowed_attributes_for_category(category)
            if not allowed:
                ph = QTreeWidgetItem(["(geen attributen gekoppeld in de SAP-mapping)", "", "", ""])
                ph.setFlags(Qt.ItemIsEnabled)
                ph.setForeground(0, QColor(150, 150, 150))
                top.addChild(ph)
                continue
            # visuele attributen eerst
            for attr in sorted(allowed, key=lambda a: (a not in VISUAL_ATTRIBUTES, a)):
                pct = self.rates.get((category, attr), 0.0)
                name = self.ref.display_name(attr)
                child = QTreeWidgetItem([("👁 " if attr in VISUAL_ATTRIBUTES else "    ") + name,
                                         self.ref.attribute_type(attr),
                                         str(len(self.ref.options_for(attr))), f"{pct:.0f}%"])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                if previous is not None:
                    on = attr in previous.get(category, [])
                else:
                    on = attr in VISUAL_ATTRIBUTES
                child.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
                child.setData(0, Qt.UserRole, attr)
                child.setForeground(3, QColor.fromHsv(int(pct * 1.2), 200, 180))
                top.addChild(child)
        self.tree.collapseAll()

    def _iter_children(self):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.flags() & Qt.ItemIsUserCheckable:
                    yield top, child

    def _check_visual(self):
        for _top, child in self._iter_children():
            child.setCheckState(0, Qt.Checked if child.data(0, Qt.UserRole) in VISUAL_ATTRIBUTES
                                else Qt.Unchecked)

    def _check_all(self, on: bool):
        for _top, child in self._iter_children():
            child.setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def _selection(self) -> dict[str, list[str]]:
        sel: dict[str, list[str]] = {}
        for top, child in self._iter_children():
            if child.checkState(0) == Qt.Checked:
                sel.setdefault(top.data(0, Qt.UserRole), []).append(child.data(0, Qt.UserRole))
        return sel

    def _show_options(self, item, _prev=None):
        self.options.clear()
        if item is None or self.ref is None or item.parent() is None:
            return
        attr = item.data(0, Qt.UserRole)
        if not attr:
            return
        opts = self.ref.options_for(attr)
        rule = "meerdere waarden toegelaten (max 3)" if self.ref.is_multi(attr) else "precies één waarde"
        self.options_title.setText(f"<b>Toegelaten waarden — {self.ref.display_name(attr)}</b><br>"
                                   f"<span style='color:#888'>{len(opts)} waarden · {rule}. "
                                   f"Het model mag ALLEEN deze waarden teruggeven.</span>")
        self.options.addItems(opts)

    # ------------------------------------------------------------ werklijst

    def build_work(self) -> bool:
        if self.catalog is None or self.ref is None:
            QMessageBox.information(self, "Geen data", "Lees eerst het productbestand in (tab 1).")
            return False
        sel = self._selection()
        if not sel:
            QMessageBox.warning(self, "Geen selectie", "Vink minstens één attribuut aan.")
            return False
        from attr_enricher import attach_packshots, select_packshots
        from reference import build_work_items
        from store import Store
        self.cfg.only_empty_values = self.chk_empty.isChecked()
        work = build_work_items(self.catalog, self.ref, sel, self.cfg.only_empty_values, self._brands())
        if work.empty:
            self.work = None
            self.work_label.setText("⚠️ Geen rijen voor deze selectie (alles al gevuld?).")
            return False
        store = Store(self.cfg.db_path)
        try:
            self.work = attach_packshots(work, select_packshots(self.cfg, store))
        finally:
            store.close()
        n_prod = self.work["variant_code"].nunique()
        ok = self.work.loc[self.work["fetch_status"] == "ok", "variant_code"].nunique()
        missing = n_prod - ok
        self.work_label.setText(
            f"<b>{len(self.work)}</b> rijen · <b>{n_prod}</b> producten · "
            f"<b>{self.work['attribute_key'].nunique()}</b> attributen · "
            f"packshot beschikbaar voor <b>{ok}</b> producten"
            + (f" — <span style='color:#c0392b'>{missing} zonder gedownload packshot "
               f"(download eerst in tab 1)</span>" if missing else ""))
        self.work_ready.emit()
        return True

    # ------------------------------------------------------------ run

    def start(self):
        if self.host._busy():
            return
        if self.work is None and not self.build_work():
            return
        self.cfg.use_openai = not self.chk_sim.isChecked()
        if self.cfg.use_openai and not self.cfg.openai_api_key:
            QMessageBox.warning(self, "Geen API-key", "Vul eerst je OpenAI API-key in bij Instellingen.")
            return
        cfg, ref, work = self.cfg, self.ref, self.work.copy()
        limit, redo = self.sp_limit.value() or None, self.chk_redo.isChecked()

        def task(control, progress):
            from attr_enricher import AttributeEnricher
            return AttributeEnricher(cfg, ref, control).run(work, limit=limit, redo=redo,
                                                            progress=progress)

        self.host._start(task, self.box, self._done)

    def _done(self, r: dict):
        stopped = self.host.worker is not None and self.host.worker.control.cancelled
        self.box.msg.setText(
            f"{'Gestopt' if stopped else 'Klaar'}: {r['ok']} producten verwerkt, {r['failed']} mislukt, "
            f"{r['values']} waarden ({r['invalid']} niet in de optielijst), "
            f"{r['no_packshot']} producten zonder packshot overgeslagen. Controleer in tab Resultaten.")
        if r.get("quota"):
            QMessageBox.critical(self, "Geen OpenAI-tegoed", "OpenAI meldt 'insufficient_quota'. "
                                 "Controleer de billing; verwerkte producten zijn bewaard.")
        self.work_ready.emit()


# ======================================================================= resultaten

class ResultsTab(QWidget):
    COLUMNS = [
        ("approved", "✅ export"), ("mainCategory", "Categorie"), ("brand", "Merk"),
        ("productName", "Product"), ("variant_code", "Variant"), ("Product attribute", "Attribuut"),
        ("current value", "Huidige waarde"), ("generated value", "Gegenereerde waarde"),
        ("new_sap_values", "SAP-codes"), ("motivation", "Motivatie"),
    ]
    VALUE_COL = 7

    def __init__(self, cfg: ClassifierConfig, attr_tab: AttributeTab, host, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.attr_tab = attr_tab
        self.host = host
        self.table_df: pd.DataFrame = pd.DataFrame()
        self.view_idx: list[int] = []
        self._loading = False

        self.f_attr = QComboBox()
        self.f_attr.addItem("alle attributen")
        self.f_status = QComboBox()
        self.f_status.addItems(["met resultaat", "alleen niet in optielijst", "zonder waarde",
                                "nog niet verwerkt", "alles"])
        btn = QPushButton("Vernieuwen")
        btn.clicked.connect(self.reload)
        self.f_attr.currentIndexChanged.connect(self._fill)
        self.f_status.currentIndexChanged.connect(self._fill)
        self.counter = QLabel()
        b_all = QPushButton("Alles aanvinken")
        b_all.clicked.connect(lambda: self._set_all(True))
        b_none = QPushButton("Alles uitvinken")
        b_none.clicked.connect(lambda: self._set_all(False))
        b_xls = QPushButton("Exporteer Excel…")
        b_xls.clicked.connect(self.export_excel)
        b_imp = QPushButton("Exporteer impex…")
        b_imp.clicked.connect(self.export_impex)
        bar = QHBoxLayout()
        for w in (QLabel("Filter:"), self.f_attr, self.f_status, btn, self.counter):
            bar.addWidget(w)
        bar.addStretch()
        for w in (b_all, b_none, b_xls, b_imp):
            bar.addWidget(w)

        self.grid = QTableWidget()
        self.grid.setColumnCount(len(self.COLUMNS))
        self.grid.setHorizontalHeaderLabels([c[1] for c in self.COLUMNS])
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.grid.horizontalHeader().setStretchLastSection(True)
        self.grid.setSelectionBehavior(QTableWidget.SelectRows)
        self.grid.itemChanged.connect(self._on_item_changed)
        self.grid.currentCellChanged.connect(lambda r, *_: self._show_row(r))

        self.preview = QLabel(alignment=Qt.AlignCenter)
        self.preview.setFixedSize(340, 340)
        self.preview.setStyleSheet("background:#fff; border:1px solid #ccc;")
        self.preview_info = QLabel()
        self.preview_info.setWordWrap(True)
        self.opt_list = QListWidget()
        self.opt_list.setToolTip("Dubbelklik om deze waarde in te vullen (Ctrl+dubbelklik = toevoegen)")
        self.opt_list.itemDoubleClicked.connect(self._pick_option)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.addWidget(self.preview)
        sl.addWidget(self.preview_info)
        sl.addWidget(QLabel("<b>Toegelaten waarden</b> (dubbelklik = invullen, "
                            "Ctrl+dubbelklik = toevoegen bij multi)"))
        sl.addWidget(self.opt_list, 1)

        split = QSplitter()
        split.addWidget(self.grid)
        split.addWidget(side)
        split.setSizes([1000, 360])
        hint = QLabel("Pas 'Gegenereerde waarde' aan waar nodig (typen of dubbelklik rechts) en vink aan "
                      "wat mee moet. Een waarde die niet letterlijk in de optielijst staat, is rood en "
                      "krijgt geen SAP-code. Aanpassingen worden meteen bewaard.")
        hint.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

    # ------------------------------------------------------------ data

    def reload(self):
        work, ref = self.attr_tab.work, self.attr_tab.ref
        if work is None or ref is None:
            self.table_df = pd.DataFrame()
            self.grid.setRowCount(0)
            self.counter.setText("Bouw eerst een werklijst in tab Attributen.")
            return
        from attr_enricher import results_table
        self.table_df = results_table(self.cfg, ref, work).reset_index(drop=True)
        current = self.f_attr.currentText()
        self.f_attr.blockSignals(True)
        self.f_attr.clear()
        self.f_attr.addItem("alle attributen")
        self.f_attr.addItems(sorted(self.table_df["Product attribute"].dropna().unique()))
        i = self.f_attr.findText(current)
        self.f_attr.setCurrentIndex(max(i, 0))
        self.f_attr.blockSignals(False)
        self._fill()

    def _fill(self):
        t = self.table_df
        if t.empty:
            self.grid.setRowCount(0)
            return
        mask = pd.Series(True, index=t.index)
        if self.f_attr.currentIndex() > 0:
            mask &= t["Product attribute"] == self.f_attr.currentText()
        processed = t["model"].notna()
        status = self.f_status.currentText()
        if status == "met resultaat":
            mask &= processed
        elif status == "alleen niet in optielijst":
            mask &= processed & t["generated value"].notna() & ~t["valid"]
        elif status == "zonder waarde":
            mask &= processed & t["generated value"].isna()
        elif status == "nog niet verwerkt":
            mask &= ~processed
        self.view_idx = list(t.index[mask])

        self._loading = True
        self.grid.setUpdatesEnabled(False)
        self.grid.setRowCount(len(self.view_idx))
        red = QColor(192, 57, 43)
        for r, idx in enumerate(self.view_idx):
            row = t.loc[idx]
            for c, (key, _) in enumerate(self.COLUMNS):
                if key == "approved":
                    item = QTableWidgetItem()
                    item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    item.setCheckState(Qt.Checked if row["approved"] else Qt.Unchecked)
                else:
                    v = row[key]
                    text = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
                    if key == "motivation" and isinstance(row["error"], str):
                        text = f"FOUT: {row['error']}"
                    item = QTableWidgetItem(text)
                    if key == "generated value":
                        item.setFlags(item.flags() | Qt.ItemIsEditable)
                        if text and not row["valid"]:
                            item.setForeground(red)
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setData(Qt.UserRole, idx)
                self.grid.setItem(r, c, item)
        self.grid.setUpdatesEnabled(True)
        self._loading = False
        if len(self.view_idx) < 3000:
            self.grid.resizeColumnsToContents()
        self.grid.setColumnWidth(3, min(self.grid.columnWidth(3), 260))
        self.grid.setColumnWidth(9, 320)
        n_ok = int((t["approved"] & t["generated value"].notna() & t["valid"]).sum())
        self.counter.setText(f"{len(self.view_idx)} rijen getoond · {n_ok} klaar voor export")

    # ------------------------------------------------------------ bewerken

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading:
            return
        idx = item.data(Qt.UserRole)
        col = item.column()
        t = self.table_df
        if col == 0:
            t.at[idx, "approved"] = item.checkState() == Qt.Checked
        elif col == self.VALUE_COL:
            self._set_value(idx, item.text().strip(), refresh_row=item.row())
            return
        else:
            return
        self._persist(idx)

    def _has_result(self, idx: int) -> bool:
        return isinstance(self.table_df.at[idx, "model"], str)

    def _set_value(self, idx: int, value: str, refresh_row: int | None = None):
        from reference import sap_codes, validate_choice
        ref = self.attr_tab.ref
        t = self.table_df
        attr = t.at[idx, "attribute_key"]
        url = t.at[idx, "packshot_url"]
        if not self._has_result(idx) and not isinstance(url, str):
            QMessageBox.information(self, "Geen packshot", "Dit product heeft geen packshot; "
                                    "een manuele waarde kan pas na het downloaden bewaard worden.")
            if refresh_row is not None:
                self._refresh_row(refresh_row, idx)
            return
        norm, valid = validate_choice(value or None, ref.options_for(attr))
        t.at[idx, "generated value"] = norm
        t.at[idx, "valid"] = valid
        t.at[idx, "new_sap_values"] = sap_codes(ref, attr, norm) if valid else ""
        t.at[idx, "approved"] = bool(norm) and valid
        if not self._has_result(idx):
            # nog geen modelresultaat: een manuele waarde maakt een rij aan
            from store import Store
            st = Store(self.cfg.db_path)
            st.save_attr_result(t.at[idx, "variant_code"], attr, url,
                                None, True, "manueel ingevuld", "manueel")
            st.close()
            t.at[idx, "model"] = "manueel"
        self._persist(idx, edited=norm or "")
        if refresh_row is not None:
            self._refresh_row(refresh_row, idx)

    def _persist(self, idx: int, edited: str | None = None):
        from store import Store
        t = self.table_df
        if not self._has_result(idx):
            return   # geen resultaatrij in de database (nog niet verwerkt)
        st = Store(self.cfg.db_path)
        try:
            if edited is None:
                # alleen vinkje: bestaande manuele waarde behouden
                cur = st.conn.execute("SELECT edited_value FROM attr_results WHERE product_id=? AND "
                                      "attribute_key=?", (t.at[idx, "variant_code"],
                                                          t.at[idx, "attribute_key"])).fetchone()
                edited = cur[0] if cur else None
            st.set_attr_edit(t.at[idx, "variant_code"], t.at[idx, "attribute_key"], edited,
                             bool(t.at[idx, "approved"]))
        finally:
            st.close()

    def _refresh_row(self, r: int, idx: int):
        self._loading = True
        t = self.table_df
        self.grid.item(r, 0).setCheckState(Qt.Checked if t.at[idx, "approved"] else Qt.Unchecked)
        v = t.at[idx, "generated value"]
        v = v if isinstance(v, str) else ""
        vi = self.grid.item(r, self.VALUE_COL)
        vi.setText(v)
        vi.setForeground(QColor(192, 57, 43) if v and not t.at[idx, "valid"] else QColor(0, 0, 0))
        self.grid.item(r, 8).setText(t.at[idx, "new_sap_values"] or "")
        self._loading = False

    def _pick_option(self, item):
        r = self.grid.currentRow()
        if r < 0 or r >= len(self.view_idx):
            return
        idx = self.view_idx[r]
        from PySide6.QtWidgets import QApplication
        value = item.text()
        cur = self.table_df.at[idx, "generated value"]
        if QApplication.keyboardModifiers() & Qt.ControlModifier and isinstance(cur, str) and cur:
            parts = [p.strip() for p in cur.split(",")]
            if value not in parts:
                value = ", ".join(parts + [value])
        self._set_value(idx, value, refresh_row=r)

    def _set_all(self, on: bool):
        for r, idx in enumerate(self.view_idx):
            self.grid.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _show_row(self, r: int):
        if r < 0 or r >= len(self.view_idx):
            return
        row = self.table_df.loc[self.view_idx[r]]
        thumb = row["thumb"]
        path = self.cfg.thumbs_dir / thumb if isinstance(thumb, str) else None
        if path and path.is_file():
            pm = QPixmap(str(path))
            self.preview.setPixmap(pm.scaled(self.preview.size(), Qt.KeepAspectRatio,
                                             Qt.SmoothTransformation))
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("geen packshot gedownload")
        self.preview_info.setText(f"<b>{row['brand'] or ''}</b> {row['productName'] or ''}<br>"
                                  f"variant {row['variant_code']} · {row['Product attribute']}<br>"
                                  f"<span style='color:#888'>{row['model'] or 'nog niet verwerkt'}</span>")
        self.opt_list.clear()
        ref = self.attr_tab.ref
        if ref is not None:
            self.opt_list.addItems(ref.options_for(row["attribute_key"]))

    # ------------------------------------------------------------ export

    def _export(self, title: str, suffix: str, filt: str, fn):
        if self.table_df.empty:
            QMessageBox.information(self, "Niets te exporteren", "Geen resultaten.")
            return
        start = str(Path(self.cfg.last_export_dir) / f"visuele_attributen{suffix}")
        path, _ = QFileDialog.getSaveFileName(self, title, start, filt)
        if not path:
            return
        n = fn(path)
        self.cfg.last_export_dir = Path(path).parent
        self.cfg.save_user_settings()
        invalid = int((self.table_df["approved"] & self.table_df["generated value"].notna()
                       & ~self.table_df["valid"]).sum())
        extra = (f"\n\n{invalid} aangevinkte rij(en) met een waarde buiten de optielijst zijn "
                 f"NIET meegenomen (filter 'alleen niet in optielijst').") if invalid else ""
        QMessageBox.information(self, "Export klaar", f"{n} rijen geschreven naar:\n{path}{extra}")

    def export_excel(self):
        from attr_enricher import export_attribute_excel
        self._export("Exporteer Excel als…", ".xlsx", "Excel (*.xlsx)",
                     lambda p: export_attribute_excel(self.table_df, p))

    def export_impex(self):
        from attr_enricher import export_attribute_impex
        self._export("Exporteer impex als…", ".impex", "Impex (*.impex);;Tekst (*.txt)",
                     lambda p: export_attribute_impex(self.table_df, p, self.cfg.impex_catalog))

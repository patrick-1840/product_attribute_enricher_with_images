"""
Review-scherm: per product de thumbnails naast (of onder) elkaar, met het
voorgestelde label, de confidence en een vinkje "gebruik voor attributie".

Werkwijze met het toetsenbord (alle toetsen instelbaar in Instellingen):
- pijltjes (links/rechts bij horizontale lay-out, omhoog/omlaag bij verticale)
  = vorige/volgende afbeelding; over productgrenzen heen;
- A = accept, D = deny  → bewaart meteen en springt naar de volgende afbeelding;
- 1–5 = label rechtstreeks zetten (correctie) → bewaart en springt door;
- spatie = vinkje "gebruik" omschakelen; Backspace = review wissen;
- PgUp/PgDown = vorig/volgend product.

Navigatie loopt over de GEFILTERDE lijst (label, lage confidence, niet
gereviewd). De filter wordt pas opnieuw toegepast bij "Toepassen", zodat een
net gereviewd beeld niet onder je handen verdwijnt.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QBoxLayout, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QVBoxLayout, QWidget,
)

import pandas as pd

from config import LABELS, USE_LABEL, ClassifierConfig
from exporter import UNKNOWN_LABEL, add_effective_columns, slot_prefix
from store import Store

logger = logging.getLogger(__name__)

NOT_CLASSIFIED = "(niet geclassificeerd)"
DECISION_TEXT = {
    "accept": "✔ geaccepteerd",
    "deny": "✖ afgekeurd",
    "relabel": "✎ gecorrigeerd",
}


def _conf_color(conf) -> str:
    if conf is None or pd.isna(conf):
        return "#888"
    if conf >= 0.85:
        return "#1b8a3a"
    if conf >= 0.6:
        return "#c77c00"
    return "#c0392b"


class ImageCard(QFrame):
    """Eén afbeelding met label, confidence, reviewstatus en vinkje."""

    clicked = Signal(int)                 # rij-index in de tabel
    use_toggled = Signal(int, bool)
    label_chosen = Signal(int, str)

    def __init__(self, row_idx: int, vertical: bool, parent=None):
        super().__init__(parent)
        self.row_idx = row_idx
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        self.setCursor(Qt.PointingHandCursor)
        self.thumb = QLabel(alignment=Qt.AlignCenter)
        size = 200 if vertical else 260
        self.thumb.setFixedSize(size, size)
        self.thumb.setStyleSheet("background:#fff;")
        self.title = QLabel()
        self.pred = QLabel()
        self.pred.setTextFormat(Qt.RichText)
        self.status = QLabel()
        self.use = QCheckBox("gebruik voor attributie")
        self.use.setFocusPolicy(Qt.NoFocus)
        self.combo = QComboBox()
        self.combo.setFocusPolicy(Qt.NoFocus)
        self.combo.addItems(["— label wijzigen —", *LABELS])
        self.combo.setToolTip("Label manueel corrigeren (of druk 1–5)")

        info = QVBoxLayout()
        for w in (self.title, self.pred, self.status, self.use, self.combo):
            info.addWidget(w)
        info.addStretch()
        lay = QHBoxLayout(self) if vertical else QVBoxLayout(self)
        lay.addWidget(self.thumb)
        lay.addLayout(info)

        self.use.clicked.connect(lambda checked: self.use_toggled.emit(self.row_idx, checked))
        self.combo.activated.connect(self._on_combo)

    def _on_combo(self, i: int):
        if i > 0:
            self.label_chosen.emit(self.row_idx, LABELS[i - 1])
        self.combo.setCurrentIndex(0)

    def mousePressEvent(self, event):
        self.clicked.emit(self.row_idx)
        super().mousePressEvent(event)

    def set_pixmap(self, path: Path | None, failed_text: str = ""):
        if path and path.is_file():
            pm = QPixmap(str(path))
            self.thumb.setPixmap(pm.scaled(self.thumb.size(), Qt.KeepAspectRatio,
                                           Qt.SmoothTransformation))
        else:
            self.thumb.setText(failed_text or "nog niet gedownload")

    def update_from(self, row: dict, current: bool, in_filter: bool):
        self.title.setText(f"<b>{slot_prefix(row['slot'])}</b>  <span style='color:#888'>"
                           f"({row['source_col']})</span>")
        label, conf = row.get("label"), row.get("confidence")
        if isinstance(label, str):
            extra = ""
            if isinstance(row.get("first_label"), str):
                extra = (f"<br><span style='color:#888'>eerst {row['first_label']} "
                         f"({row['first_confidence']:.2f}), geëscaleerd</span>")
            self.pred.setText(f"voorstel: <b>{label}</b> <span style='color:{_conf_color(conf)}'>"
                              f"({conf:.2f})</span>{extra}")
        elif isinstance(row.get("classify_error"), str):
            self.pred.setText(f"<span style='color:#c0392b'>classificatie mislukt</span>")
        else:
            self.pred.setText(f"<span style='color:#888'>{NOT_CLASSIFIED}</span>")
        decision = row.get("decision")
        if isinstance(decision, str):
            txt = DECISION_TEXT.get(decision, decision)
            if decision != "accept":
                txt += f" → {row.get('final_label') or UNKNOWN_LABEL}"
            self.status.setText(txt)
        else:
            self.status.setText("<span style='color:#888'>– nog niet gereviewd</span>")
        self.use.setChecked(bool(row["eff_use"]))
        border = "3px solid #2d7ff9" if current else "1px solid #ccc"
        bg = "#eef5ff" if current else ("#fff" if in_filter else "#f2f2f2")
        self.setStyleSheet(f"ImageCard {{ border:{border}; border-radius:6px; background:{bg}; }}")


class ReviewWidget(QWidget):
    status_message = Signal(str)
    export_requested = Signal()

    def __init__(self, cfg: ClassifierConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.store: Store | None = None
        self.table: pd.DataFrame = pd.DataFrame()
        self.by_product: dict[str, list[int]] = {}
        self.filtered: list[int] = []          # rij-indices in reviewvolgorde
        self.filtered_set: set[int] = set()
        self.pos = -1                           # index in self.filtered
        self.cards: dict[int, ImageCard] = {}
        self.shown_product: str | None = None
        self._shortcuts: list[QShortcut] = []

        # --- filterbalk
        self.f_label = QComboBox()
        self.f_label.addItems(["alle labels", *LABELS, NOT_CLASSIFIED])
        self.f_lowconf = QCheckBox("alleen confidence <")
        self.f_conf = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=1.0, singleStep=0.05)
        self.f_conf.setValue(cfg.confidence_threshold)
        self.f_unreviewed = QCheckBox("alleen niet gereviewd")
        self.f_unreviewed.setChecked(True)
        btn_apply = QPushButton("Toepassen / vernieuwen")
        btn_apply.clicked.connect(self.reload)
        self.search = QLineEdit(placeholderText="zoek product-id…")
        self.search.returnPressed.connect(self._goto_search)
        btn_export = QPushButton("Exporteer als…")
        btn_export.clicked.connect(self.export_requested.emit)
        self.counter = QLabel()

        bar = QHBoxLayout()
        for w in (QLabel("Filter:"), self.f_label, self.f_lowconf, self.f_conf,
                  self.f_unreviewed, btn_apply, self.search):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.counter)
        bar.addWidget(btn_export)

        # --- productlijst links
        self.product_list = QListWidget()
        self.product_list.setFocusPolicy(Qt.NoFocus)
        self.product_list.itemClicked.connect(self._on_product_clicked)

        # --- kaarten
        self.header = QLabel()
        self.header.setTextFormat(Qt.RichText)
        self.header.setWordWrap(True)
        self.cards_host = QWidget()
        self.cards_layout = QBoxLayout(QBoxLayout.LeftToRight, self.cards_host)
        self.cards_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.cards_host)
        self.scroll.setFocusPolicy(Qt.StrongFocus)
        self.help = QLabel()
        self.help.setStyleSheet("color:#666;")
        self.help.setWordWrap(True)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(self.header)
        rl.addWidget(self.scroll, 1)
        rl.addWidget(self.help)

        split = QSplitter()
        split.addWidget(self.product_list)
        split.addWidget(right)
        split.setSizes([260, 1000])

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        self.apply_settings()

    # ------------------------------------------------------------ instellingen

    def apply_settings(self):
        """(Her)bouwt sneltoetsen en lay-out na een wijziging in Instellingen."""
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts = []
        vertical = self.cfg.layout_orientation == "vertical"
        actions = {
            "accept": self.accept_current,
            "deny": self.deny_current,
            "toggle_use": self.toggle_use_current,
            "next_product": lambda: self.jump_product(+1),
            "prev_product": lambda: self.jump_product(-1),
            "undo": self.undo_current,
            ("next_v" if vertical else "next_h"): lambda: self.move(+1),
            ("prev_v" if vertical else "prev_h"): lambda: self.move(-1),
            **{f"label_{i + 1}": (lambda lab=lab: self.set_label_current(lab))
               for i, lab in enumerate(LABELS)},
        }
        for action, fn in actions.items():
            seq = self.cfg.shortcut(action)
            if not seq:
                continue
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
            self._shortcuts.append(sc)

        self.cards_layout.setDirection(QBoxLayout.TopToBottom if vertical else QBoxLayout.LeftToRight)
        k = self.cfg.shortcut
        nav = (f"{k('prev_v')}/{k('next_v')}" if vertical else f"{k('prev_h')}/{k('next_h')}")
        labels = ", ".join(f"{k(f'label_{i + 1}')}={lab}" for i, lab in enumerate(LABELS))
        self.help.setText(
            f"Sneltoetsen — {nav}: vorige/volgende · {k('accept')}: accept · {k('deny')}: deny · "
            f"{k('toggle_use')}: vinkje gebruik · {labels} · {k('undo')}: review wissen · "
            f"{k('prev_product')}/{k('next_product')}: vorig/volgend product")
        self.shown_product = None
        self._render()

    # ------------------------------------------------------------ data

    def reload(self, keep_position: bool = True):
        """Leest de tabel opnieuw uit de database en past de filter toe."""
        current_row = self.filtered[self.pos] if 0 <= self.pos < len(self.filtered) else None
        if self.store is None:
            self.store = Store(self.cfg.db_path)
        self.table = add_effective_columns(self.store.item_table())
        self.by_product = {}
        for idx, pid in enumerate(self.table["product_id"]):
            self.by_product.setdefault(pid, []).append(idx)
        self._apply_filter()
        self.pos = 0 if self.filtered else -1
        if keep_position and current_row is not None and current_row in self.filtered_set:
            self.pos = self.filtered.index(current_row)
        self.shown_product = None
        self._fill_product_list()
        self._render()
        self.scroll.setFocus()

    def reset_store(self):
        """Na het wijzigen van de cachemap: nieuwe databaseconnectie."""
        if self.store is not None:
            self.store.close()
        self.store = None

    def _apply_filter(self):
        t = self.table
        if t.empty:
            self.filtered, self.filtered_set = [], set()
            return
        mask = pd.Series(True, index=t.index)
        choice = self.f_label.currentText()
        if choice == NOT_CLASSIFIED:
            mask &= t["label"].isna()
        elif choice in LABELS:
            mask &= t["label"] == choice
        if self.f_lowconf.isChecked():
            mask &= t["confidence"].notna() & (t["confidence"] < self.f_conf.value())
        if self.f_unreviewed.isChecked():
            mask &= t["decision"].isna()
        self.filtered = list(t.index[mask])
        self.filtered_set = set(self.filtered)

    def _fill_product_list(self):
        self.product_list.clear()
        seen: list[str] = []
        last = None
        for idx in self.filtered:
            pid = self.table.at[idx, "product_id"]
            if pid != last:
                seen.append(pid)
                last = pid
        for pid in dict.fromkeys(seen):
            info = self._info(pid)
            text = f"{pid}  {info.get('brand', '')} {info.get('productName', '')}".strip()
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, pid)
            self.product_list.addItem(item)

    def _info(self, pid: str) -> dict:
        rows = self.by_product.get(pid)
        if not rows:
            return {}
        try:
            return json.loads(self.table.at[rows[0], "info"] or "{}")
        except (TypeError, ValueError):
            return {}

    # ------------------------------------------------------------ weergave

    def _render(self):
        if not self.filtered or self.pos < 0:
            self._clear_cards()
            self.header.setText("<i>Geen afbeeldingen voor deze filter. Lees eerst het Excel in, "
                                "download en classificeer, of pas de filter aan.</i>")
            self.counter.setText("0 / 0")
            self.shown_product = None
            return
        row_idx = self.filtered[self.pos]
        pid = self.table.at[row_idx, "product_id"]
        if pid != self.shown_product:
            self._build_cards(pid)
        for idx, card in self.cards.items():
            card.update_from(self.table.loc[idx].to_dict(), idx == row_idx, idx in self.filtered_set)
        cur = self.cards.get(row_idx)
        if cur:
            self.scroll.ensureWidgetVisible(cur)
        reviewed = int(self.table["decision"].notna().sum())
        self.counter.setText(f"beeld {self.pos + 1} / {len(self.filtered)} (filter) · "
                             f"{reviewed} / {len(self.table)} gereviewd")

    def _clear_cards(self):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            wdg = item.widget()
            if wdg:
                wdg.hide()            # meteen weg; deleteLater ruimt pas later op
                wdg.setParent(None)
                wdg.deleteLater()
        self.cards = {}

    def _build_cards(self, pid: str):
        self._clear_cards()
        vertical = self.cfg.layout_orientation == "vertical"
        thumbs = Path(self.cfg.thumbs_dir)
        for idx in self.by_product.get(pid, []):
            row = self.table.loc[idx]
            card = ImageCard(idx, vertical)
            thumb = row["thumb"]
            failed = (f"download mislukt\n{row['fetch_error'] or ''}"
                      if row["fetch_status"] == "failed" else "")
            card.set_pixmap(thumbs / thumb if isinstance(thumb, str) else None, failed)
            card.clicked.connect(self._on_card_clicked)
            card.use_toggled.connect(self._on_use_toggled)
            card.label_chosen.connect(self._on_label_chosen)
            self.cards_layout.addWidget(card)
            self.cards[idx] = card
        self.cards_layout.addStretch()
        info = self._info(pid)
        extra = " · ".join(f"{k}: <b>{v}</b>" for k, v in info.items() if v)
        self.header.setText(f"<span style='font-size:14pt'><b>{pid}</b></span>  {extra}")
        self.shown_product = pid
        # productlijst synchroon houden
        for i in range(self.product_list.count()):
            if self.product_list.item(i).data(Qt.UserRole) == pid:
                self.product_list.setCurrentRow(i)
                break

    # ------------------------------------------------------------ navigatie

    def move(self, step: int):
        if not self.filtered:
            return
        self.pos = max(0, min(len(self.filtered) - 1, self.pos + step))
        self._render()

    def jump_product(self, step: int):
        if not self.filtered:
            return
        pid = self.table.at[self.filtered[self.pos], "product_id"]
        i = self.pos
        while 0 <= i < len(self.filtered) and self.table.at[self.filtered[i], "product_id"] == pid:
            i += step
        if not 0 <= i < len(self.filtered):
            return
        if step < 0:   # naar het EERSTE beeld van het vorige product
            prev = self.table.at[self.filtered[i], "product_id"]
            while i > 0 and self.table.at[self.filtered[i - 1], "product_id"] == prev:
                i -= 1
        self.pos = i
        self._render()

    def _goto_row(self, row_idx: int):
        if row_idx in self.filtered_set:
            self.pos = self.filtered.index(row_idx)
            self._render()
        self.scroll.setFocus()

    def _on_card_clicked(self, row_idx: int):
        self._goto_row(row_idx)

    def _on_product_clicked(self, item: QListWidgetItem):
        pid = item.data(Qt.UserRole)
        for idx in self.by_product.get(pid, []):
            if idx in self.filtered_set:
                self._goto_row(idx)
                return

    def _goto_search(self):
        needle = self.search.text().strip()
        if not needle:
            return
        rows = self.by_product.get(needle)
        if not rows:
            self.status_message.emit(f"Product '{needle}' niet gevonden.")
            return
        hit = next((r for r in rows if r in self.filtered_set), None)
        if hit is None:
            self.status_message.emit(f"Product '{needle}' valt buiten de huidige filter.")
            return
        self._goto_row(hit)

    # ------------------------------------------------------------ beslissingen

    def _current(self) -> int | None:
        if not self.filtered or self.pos < 0:
            return None
        return self.filtered[self.pos]

    def _save(self, row_idx: int, decision: str | None, final_label: str | None, use: bool):
        if self.store is None:
            self.store = Store(self.cfg.db_path)
        r = self.table.loc[row_idx]
        if decision is None:
            self.store.clear_review(r["product_id"], r["slot"])
            self.table.at[row_idx, "decision"] = None
            self.table.at[row_idx, "final_label"] = None
            self.table.at[row_idx, "review_use"] = None
            self.table.at[row_idx, "eff_use"] = r["label"] == USE_LABEL
            self.table.at[row_idx, "eff_label"] = r["label"] or ""
        else:
            self.store.set_review(r["product_id"], r["slot"], r["url"], decision, final_label, use)
            self.table.at[row_idx, "decision"] = decision
            self.table.at[row_idx, "final_label"] = final_label
            self.table.at[row_idx, "review_use"] = int(use)
            self.table.at[row_idx, "eff_use"] = use
            self.table.at[row_idx, "eff_label"] = final_label or UNKNOWN_LABEL

    def accept_current(self):
        idx = self._current()
        if idx is None:
            return
        label = self.table.at[idx, "label"]
        if not isinstance(label, str):
            self.status_message.emit("Geen voorstel om te accepteren; kies een label met 1–5.")
            return
        self._save(idx, "accept", label, label == USE_LABEL)
        self.move(+1) if self.pos < len(self.filtered) - 1 else self._render()

    def deny_current(self):
        idx = self._current()
        if idx is None:
            return
        self._save(idx, "deny", None, False)
        self.move(+1) if self.pos < len(self.filtered) - 1 else self._render()

    def set_label_current(self, label: str):
        idx = self._current()
        if idx is not None:
            self._on_label_chosen(idx, label, advance=True)

    def _on_label_chosen(self, row_idx: int, label: str, advance: bool = False):
        predicted = self.table.at[row_idx, "label"]
        decision = "accept" if label == predicted else "relabel"
        self._save(row_idx, decision, label, label == USE_LABEL)
        if advance and self.pos < len(self.filtered) - 1:
            self.move(+1)
        else:
            self._render()

    def toggle_use_current(self):
        idx = self._current()
        if idx is not None:
            self._on_use_toggled(idx, not bool(self.table.at[idx, "eff_use"]))

    def _on_use_toggled(self, row_idx: int, use: bool):
        r = self.table.loc[row_idx]
        decision = r["decision"] if isinstance(r["decision"], str) else None
        if decision is None:
            if not isinstance(r["label"], str):
                self.status_message.emit("Kies eerst een label (1–5) voor dit beeld.")
                self._render()
                return
            decision, final = "accept", r["label"]
        else:
            final = r["final_label"] if isinstance(r["final_label"], str) else None
        self._save(row_idx, decision, final, use)
        self._render()

    def undo_current(self):
        idx = self._current()
        if idx is not None:
            self._save(idx, None, None, False)
            self._render()

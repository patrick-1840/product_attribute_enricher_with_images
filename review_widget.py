"""
Packshot-review als doorscrolbare lijst: één rij per product met alle
thumbnails naast elkaar. Het beeld dat als packshot gebruikt wordt, krijgt een
groene rand en een ★; beelden zonder vinkje "gebruik" zijn gedimd. Bedoeld om
snel door duizenden producten te scrollen en alleen in te grijpen waar nodig.

Bediening (alle toetsen instelbaar in Instellingen):
- muiswiel / PgUp / PgDown / Home / End = snel scrollen;
- ↑/↓ = vorig/volgend product, ←/→ = vorige/volgende afbeelding;
- klik = afbeelding selecteren, dubbelklik of P = als packshot kiezen,
  rechtsklik = menu (labels, accept/deny, vinkje, review wissen);
- Enter = product goedkeuren (alle voorstellen) en naar het volgende product;
- A/D = voorstel van de afbeelding goedkeuren/afkeuren, 1–7 = label zetten,
  spatie = vinkje "gebruik", Backspace = review wissen;
- knoppen "goedkeuren tot hier" / "alles in de lijst goedkeuren" voor bulk.

Toetsen worden in de lijst zelf afgehandeld (geen QShortcut), zodat een
filterveld of de tabbalk ze niet kan opslokken. De lijst is gevirtualiseerd
(QListView + delegate): alleen zichtbare rijen worden getekend, thumbnails
gaan via QPixmapCache.

De filter wordt pas opnieuw toegepast bij "Toepassen", zodat een net
gereviewd product niet onder je handen verdwijnt.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen, QPixmap, QPixmapCache
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit,
    QListView, QMenu, QMessageBox, QPushButton, QSpinBox, QStyledItemDelegate, QVBoxLayout,
    QWidget,
)

import pandas as pd

from config import LABELS, PACKSHOT_LABELS, ClassifierConfig, is_use_label
from exporter import UNKNOWN_LABEL, add_effective_columns
from store import Store

logger = logging.getLogger(__name__)

NOT_CLASSIFIED = "(niet geclassificeerd)"
ALL_PACKSHOTS = "alle packshots"
INFO_W = 270        # breedte van de productinfo links in elke rij
PAD = 6
GAP = 8
CAPTION_H = 18

SHORT_LABEL = {
    "packshot_product": "PS product",
    "packshot_verpakking": "PS verpakking",
    "packshot_beide": "PS beide",
    "packshot": "packshot",
}
DECISION_BADGE = {"accept": ("✔", "#1b8a3a"), "relabel": ("✎", "#c77c00"), "deny": ("✖", "#c0392b")}

QPixmapCache.setCacheLimit(256 * 1024)   # KB


def _conf_color(conf) -> str:
    if conf is None or pd.isna(conf):
        return "#888"
    if conf >= 0.85:
        return "#1b8a3a"
    if conf >= 0.6:
        return "#c77c00"
    return "#c0392b"


def _on(value) -> bool:
    """Effectief vinkje aan? (None/NaN/False/numpy-bools veilig afhandelen)"""
    return value is not None and not (isinstance(value, float) and pd.isna(value)) and bool(value)


def _key_text(seq: str) -> str:
    """Normaliseert een toetsomschrijving ("PgDown", "Return", "a") naar Qt-tekst."""
    return QKeySequence(seq).toString() if seq else ""


# ================================================================= model/view

class ProductModel(QAbstractListModel):
    """Eén rij per product (product-id's in reviewvolgorde)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pids: list[str] = []

    def set_products(self, pids: list[str]):
        self.beginResetModel()
        self.pids = pids
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.pids)

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and role == Qt.DisplayRole:
            return self.pids[index.row()]
        return None

    def refresh_row(self, row: int):
        if 0 <= row < len(self.pids):
            idx = self.index(row)
            self.dataChanged.emit(idx, idx)


class RowDelegate(QStyledItemDelegate):
    def __init__(self, review: "ReviewWidget"):
        super().__init__(review)
        self.review = review

    def sizeHint(self, option, index):
        return QSize(max(option.rect.width(), 400), self.review.row_height())

    def paint(self, painter: QPainter, option, index):
        self.review.paint_row(painter, option.rect, index.row())


class ProductView(QListView):
    """Lijst die zelf de sneltoetsen en klikken afhandelt."""

    def __init__(self, review: "ReviewWidget"):
        super().__init__(review)
        self.review = review
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(False)

    def keyPressEvent(self, event):
        if not self.review.handle_key(event):
            super().keyPressEvent(event)      # PgUp/PgDown/Home/End: native scroll

    def _hit(self, pos):
        idx = self.indexAt(pos)
        if not idx.isValid():
            return None, None
        return idx.row(), self.review.image_at(self.visualRect(idx), pos)

    def mousePressEvent(self, event):
        self.setFocus()
        row, img = self._hit(event.position().toPoint())
        if row is None:
            return
        self.review.set_current(row, img if img is not None else self.review.cur_img, scroll=False)
        if event.button() == Qt.RightButton and img is not None:
            self.review.context_menu(event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event):
        row, img = self._hit(event.position().toPoint())
        if row is not None and img is not None:
            self.review.set_current(row, img, scroll=False)
            self.review.set_packshot_current()


# ================================================================= widget

class ReviewWidget(QWidget):
    status_message = Signal(str)
    export_requested = Signal()

    def __init__(self, cfg: ClassifierConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.store: Store | None = None
        self.table: pd.DataFrame = pd.DataFrame()
        self.by_product: dict[str, list[int]] = {}
        self.cur_row = -1          # index in model.pids
        self.cur_img = 0           # index binnen de beelden van het product
        self._keymap: dict[str, callable] = {}
        self._info_cache: dict[str, dict] = {}

        # --- filterbalk
        self.f_label = QComboBox()
        self.f_label.addItems(["alle labels", ALL_PACKSHOTS, *LABELS, NOT_CLASSIFIED])
        self.f_doubt = QCheckBox("alleen twijfelgevallen")
        self.f_doubt.setToolTip("Producten zonder packshot, waar het packshot niet het eerste beeld "
                                "is, of met een beeld onder de confidence-drempel.")
        self.f_lowconf = QCheckBox("confidence <")
        self.f_conf = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=1.0, singleStep=0.05)
        self.f_conf.setValue(cfg.confidence_threshold)
        self.f_unreviewed = QCheckBox("alleen niet gereviewd")
        self.f_unreviewed.setChecked(True)
        btn_apply = QPushButton("Toepassen / vernieuwen")
        btn_apply.clicked.connect(lambda: self.reload(keep_position=False))
        self.search = QLineEdit(placeholderText="zoek product-id…")
        self.search.setMaximumWidth(160)
        self.search.returnPressed.connect(self._goto_search)
        self.sp_size = QSpinBox(minimum=80, maximum=300, singleStep=20, suffix=" px")
        self.sp_size.setValue(cfg.review_thumb_px)
        self.sp_size.setToolTip("Grootte van de thumbnails (kleiner = meer producten per scherm)")
        self.sp_size.valueChanged.connect(self._on_size_changed)
        btn_export = QPushButton("Exporteer als…")
        btn_export.clicked.connect(self.export_requested.emit)
        for w in (self.f_label, self.f_doubt, self.f_lowconf, self.f_conf, self.f_unreviewed,
                  btn_apply, self.search, self.sp_size, btn_export):
            w.setFocusPolicy(Qt.ClickFocus)

        bar = QHBoxLayout()
        for w in (QLabel("Filter:"), self.f_label, self.f_doubt, self.f_lowconf, self.f_conf,
                  self.f_unreviewed, btn_apply, self.search):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(QLabel("Beeldgrootte:"))
        bar.addWidget(self.sp_size)
        bar.addWidget(btn_export)

        # --- lijst
        self.model = ProductModel(self)
        self.view = ProductView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(RowDelegate(self))
        self.view.selectionModel().currentChanged.connect(self._on_view_current)

        # --- bulk + teller
        bulk = QHBoxLayout()
        self.counter = QLabel()
        b_here = QPushButton("Goedkeuren tot hier")
        b_here.setToolTip("Alle nog niet gereviewde voorstellen van het eerste product in de lijst "
                          "tot en met het geselecteerde product goedkeuren.")
        b_here.clicked.connect(lambda: self.accept_range(until_current=True))
        b_all = QPushButton("Alles in de lijst goedkeuren")
        b_all.clicked.connect(lambda: self.accept_range(until_current=False))
        for b in (b_here, b_all):
            b.setFocusPolicy(Qt.NoFocus)
        bulk.addWidget(self.counter, 1)
        bulk.addWidget(b_here)
        bulk.addWidget(b_all)

        self.help = QLabel()
        self.help.setStyleSheet("color:#666;")
        self.help.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        lay.addLayout(bulk)
        lay.addWidget(self.help)

        self.apply_settings()

    # ------------------------------------------------------------ instellingen

    def apply_settings(self):
        """(Her)bouwt de toetsenkaart na een wijziging in Instellingen."""
        actions = {
            "prod_next": lambda: self.move_product(+1),
            "prod_prev": lambda: self.move_product(-1),
            "img_next": lambda: self.move_image(+1),
            "img_prev": lambda: self.move_image(-1),
            "accept_product": self.accept_product_current,
            "set_packshot": self.set_packshot_current,
            "accept": self.accept_current,
            "deny": self.deny_current,
            "toggle_use": self.toggle_use_current,
            "undo": self.undo_current,
            **{f"label_{i + 1}": (lambda lab=lab: self.set_label_current(lab))
               for i, lab in enumerate(LABELS)},
        }
        self._keymap = {}
        for action, fn in actions.items():
            key = _key_text(self.cfg.shortcut(action))
            if key:
                self._keymap[key] = fn
        k = lambda a: self.cfg.shortcut(a)  # noqa: E731
        labels = ", ".join(f"{k(f'label_{i + 1}')}={SHORT_LABEL.get(lab, lab)}"
                           for i, lab in enumerate(LABELS))
        self.help.setText(
            f"Scrollen: muiswiel / PgUp / PgDown · {k('prod_prev')}/{k('prod_next')}: vorig/volgend "
            f"product · {k('img_prev')}/{k('img_next')}: afbeelding · <b>{k('accept_product')}</b>: "
            f"product goedkeuren en door · <b>{k('set_packshot')}</b> of dubbelklik: als packshot "
            f"kiezen · {k('accept')}/{k('deny')}: accept/deny afbeelding · {k('toggle_use')}: "
            f"vinkje gebruik · {labels} · {k('undo')}: review wissen · rechtsklik: menu. "
            "<span style='color:#888'>Groene rand + ★ = packshot voor de attributen; gedimd = "
            "niet gebruikt.</span>")
        self.help.setTextFormat(Qt.RichText)
        self.view.viewport().update()

    def handle_key(self, event) -> bool:
        if event.isAutoRepeat() and event.key() not in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Left,
                                                        Qt.Key_Right):
            return True                     # A/D/Enter niet laten "doorratelen"
        key = QKeySequence(event.keyCombination()).toString()
        fn = self._keymap.get(key)
        if fn is None:
            return False
        fn()
        return True

    def _on_size_changed(self, v: int):
        self.cfg.review_thumb_px = v
        self.cfg.save_user_settings()
        self.model.layoutChanged.emit()
        self._scroll_to_current()

    def row_height(self) -> int:
        return self.cfg.review_thumb_px + 2 * PAD + CAPTION_H

    # ------------------------------------------------------------ data

    def reload(self, keep_position: bool = True):
        """Leest de tabel opnieuw uit de database en past de filter toe."""
        cur_pid = self._current_pid()
        if self.store is None:
            self.store = Store(self.cfg.db_path)
        self.table = add_effective_columns(self.store.item_table()).reset_index(drop=True)
        self.by_product = {}
        for idx, pid in enumerate(self.table["product_id"]):
            self.by_product.setdefault(pid, []).append(idx)
        self._info_cache = {}
        pids = self._filtered_products()
        self.model.set_products(pids)
        row = 0 if pids else -1
        if keep_position and cur_pid in pids:
            row = pids.index(cur_pid)
        self.cur_row, self.cur_img = -1, 0
        if row >= 0:
            self.set_current(row, 0)
        self._update_counter()
        self.view.setFocus()

    def reset_store(self):
        """Na het wijzigen van de cachemap: nieuwe databaseconnectie."""
        if self.store is not None:
            self.store.close()
        self.store = None

    def _filtered_products(self) -> list[str]:
        t = self.table
        if t.empty:
            return []
        keep = pd.Series(True, index=t["product_id"].unique())

        def any_per_product(mask: pd.Series) -> pd.Series:
            return mask.groupby(t["product_id"]).any().reindex(keep.index, fill_value=False)

        choice = self.f_label.currentText()
        if choice == NOT_CLASSIFIED:
            keep &= any_per_product(t["label"].isna() & (t["fetch_status"] == "ok"))
        elif choice == ALL_PACKSHOTS:
            keep &= any_per_product(t["label"].isin(PACKSHOT_LABELS))
        elif choice in LABELS:
            keep &= any_per_product(t["label"] == choice)
        if self.f_lowconf.isChecked():
            keep &= any_per_product(t["confidence"].notna() & (t["confidence"] < self.f_conf.value()))
        if self.f_unreviewed.isChecked():
            keep &= any_per_product(t["decision"].isna() & (t["fetch_status"] == "ok"))
        if self.f_doubt.isChecked():
            low = any_per_product(t["confidence"].notna() & (t["confidence"] < self.f_conf.value()))
            classified = any_per_product(t["label"].notna())   # zonder voorstel: geen twijfel maar "nog te doen"
            doubt = {pid for pid in keep.index[keep]
                     if classified[pid] and (low[pid] or self._is_doubtful(pid))}
            keep &= keep.index.isin(doubt)
        order = t.drop_duplicates("product_id")["product_id"]      # tabel staat in row_order
        return [pid for pid in order if keep[pid]]

    def _is_doubtful(self, pid: str) -> bool:
        rows = self.by_product.get(pid, [])
        chosen = self.packshot_of(pid)
        return chosen is None or (rows and chosen != rows[0])

    def _info(self, pid: str) -> dict:
        if pid not in self._info_cache:
            rows = self.by_product.get(pid)
            try:
                self._info_cache[pid] = json.loads(self.table.at[rows[0], "info"] or "{}") if rows else {}
            except (TypeError, ValueError):
                self._info_cache[pid] = {}
        return self._info_cache[pid]

    def packshot_of(self, pid: str) -> int | None:
        """Rij-index van het beeld dat de attribuutverrijking als packshot gebruikt."""
        rows = self.by_product.get(pid, [])
        if not rows:
            return None
        if self.cfg.packshot_mode == "first":
            return rows[0]
        return next((r for r in rows if _on(self.table.at[r, "eff_use"])), None)

    # ------------------------------------------------------------ tekenen

    def image_at(self, row_rect: QRect, pos) -> int | None:
        """Welke afbeelding (index binnen het product) ligt onder ``pos``?"""
        t = self.cfg.review_thumb_px
        x = pos.x() - (row_rect.left() + INFO_W)
        if x < 0:
            return None
        i = x // (t + GAP)
        if x - i * (t + GAP) > t:
            return None
        row = self.view.indexAt(pos).row()
        n = len(self.by_product.get(self.model.pids[row], [])) if row >= 0 else 0
        return int(i) if i < n else None

    def _pixmap(self, thumb, size: int) -> QPixmap | None:
        if not isinstance(thumb, str) or not thumb:
            return None
        key = f"{thumb}|{size}"
        pm = QPixmapCache.find(key)
        if pm is None or pm.isNull():
            path = Path(self.cfg.thumbs_dir) / thumb
            if not path.is_file():
                return None
            pm = QPixmap(str(path))
            if pm.isNull():
                return None
            pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            QPixmapCache.insert(key, pm)
        return pm

    def paint_row(self, p: QPainter, rect: QRect, row: int):
        if not 0 <= row < len(self.model.pids):
            return
        pid = self.model.pids[row]
        rows = self.by_product.get(pid, [])
        current = row == self.cur_row
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(rect, QColor("#eaf2ff") if current else QColor("#ffffff" if row % 2 == 0 else "#f7f7f7"))
        p.setPen(QColor("#dddddd"))
        p.drawLine(rect.bottomLeft(), rect.bottomRight())

        # --- productinfo
        info = self._info(pid)
        fm = QFontMetrics(p.font())
        x, y, w = rect.left() + 8, rect.top() + PAD, INFO_W - 16
        bold = QFont(p.font())
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor("#2d7ff9") if current else QColor("#222"))
        p.drawText(x, y + fm.ascent(), f"{row + 1}.  {pid}")
        normal = QFont(bold)
        normal.setBold(False)
        p.setFont(normal)
        p.setPen(QColor("#333"))
        lines = [info.get("brand", ""), info.get("productName", ""), info.get("mainCategory", "")]
        ly = y + fm.height() + 4
        for text in lines:
            if text and ly + fm.height() < rect.bottom():
                p.drawText(x, ly + fm.ascent(), fm.elidedText(str(text), Qt.ElideRight, w))
                ly += fm.height() + 2
        n_rev = sum(isinstance(self.table.at[r, "decision"], str) for r in rows)
        if ly + fm.height() < rect.bottom():
            done = n_rev == len(rows) and rows
            p.setPen(QColor("#1b8a3a") if done else QColor("#888"))
            p.drawText(x, ly + fm.ascent(), "✔ gereviewd" if done else f"{n_rev}/{len(rows)} gereviewd")

        # --- thumbnails
        t = self.cfg.review_thumb_px
        chosen = self.packshot_of(pid)
        small = QFont(normal)
        small.setPointSizeF(max(7.0, normal.pointSizeF() - 1))
        sfm = QFontMetrics(small)
        for i, r in enumerate(rows):
            box = QRect(rect.left() + INFO_W + i * (t + GAP), rect.top() + PAD, t, t)
            if box.left() > rect.right():
                break
            rec = self.table.loc[r]
            use = _on(rec["eff_use"])
            p.fillRect(box, QColor("#ffffff"))
            pm = self._pixmap(rec["thumb"], t)
            p.setOpacity(1.0 if use or r == chosen else 0.4)
            if pm is not None:
                p.drawPixmap(box.left() + (t - pm.width()) // 2, box.top() + (t - pm.height()) // 2, pm)
            else:
                p.setPen(QColor("#999"))
                p.setFont(small)
                txt = "download mislukt" if rec["fetch_status"] == "failed" else "niet gedownload"
                p.drawText(box, Qt.AlignCenter | Qt.TextWordWrap, txt)
            p.setOpacity(1.0)
            # rand: packshot groen, huidige afbeelding blauw
            if r == chosen:
                p.setPen(QPen(QColor("#1b8a3a"), 4))
                p.drawRect(box.adjusted(2, 2, -2, -2))
            else:
                p.setPen(QPen(QColor("#cccccc"), 1))
                p.drawRect(box)
            if current and i == self.cur_img:
                p.setPen(QPen(QColor("#2d7ff9"), 3, Qt.DashLine))
                p.drawRect(box.adjusted(-3, -3, 3, 3))
            p.setFont(small)
            if r == chosen:
                badge = QRect(box.left() + 4, box.top() + 4, sfm.horizontalAdvance("★ packshot") + 8,
                              sfm.height() + 2)
                p.fillRect(badge, QColor("#1b8a3a"))
                p.setPen(QColor("white"))
                p.drawText(badge, Qt.AlignCenter, "★ packshot")
            dec = rec["decision"]
            if isinstance(dec, str) and dec in DECISION_BADGE:
                sym, col = DECISION_BADGE[dec]
                d = sfm.height() + 4
                circle = QRect(box.right() - d - 3, box.top() + 3, d, d)
                p.setBrush(QColor(col))
                p.setPen(Qt.NoPen)
                p.drawEllipse(circle)
                p.setBrush(Qt.NoBrush)
                p.setPen(QColor("white"))
                p.drawText(circle, Qt.AlignCenter, sym)
            # bijschrift: label + confidence
            label = rec["eff_label"] if isinstance(dec, str) else rec["label"]
            cap = QRect(box.left(), box.bottom() + 2, t, CAPTION_H - 2)
            if isinstance(label, str) and label:
                txt = SHORT_LABEL.get(label, label)
                conf = rec["confidence"]
                if not isinstance(dec, str) and conf is not None and not pd.isna(conf):
                    txt += f" {conf:.2f}"
                    p.setPen(QColor(_conf_color(conf)))
                else:
                    p.setPen(QColor("#333"))
                p.drawText(cap, Qt.AlignLeft | Qt.AlignVCenter, sfm.elidedText(txt, Qt.ElideRight, t))
            elif rec["fetch_status"] == "ok":
                p.setPen(QColor("#999"))
                p.drawText(cap, Qt.AlignLeft | Qt.AlignVCenter, NOT_CLASSIFIED)
        p.restore()

    # ------------------------------------------------------------ navigatie

    def _current_pid(self) -> str | None:
        if 0 <= self.cur_row < len(self.model.pids):
            return self.model.pids[self.cur_row]
        return None

    def _current(self) -> int | None:
        pid = self._current_pid()
        rows = self.by_product.get(pid, []) if pid else []
        return rows[self.cur_img] if 0 <= self.cur_img < len(rows) else None

    def set_current(self, row: int, img: int = 0, scroll: bool = True):
        if not self.model.pids:
            return
        row = max(0, min(len(self.model.pids) - 1, row))
        n = len(self.by_product.get(self.model.pids[row], []))
        img = max(0, min(n - 1, img))
        old = self.cur_row
        self.cur_row, self.cur_img = row, img
        idx = self.model.index(row)
        if self.view.currentIndex() != idx:
            self.view.selectionModel().blockSignals(True)
            self.view.setCurrentIndex(idx)
            self.view.selectionModel().blockSignals(False)
        self.model.refresh_row(old)
        self.model.refresh_row(row)
        if scroll:
            self._scroll_to_current()
        self._update_counter()

    def _scroll_to_current(self):
        if 0 <= self.cur_row < len(self.model.pids):
            self.view.scrollTo(self.model.index(self.cur_row), QAbstractItemView.EnsureVisible)

    def _on_view_current(self, cur: QModelIndex, _prev):
        # PgUp/PgDown/Home/End verplaatsen de huidige rij native
        if cur.isValid() and cur.row() != self.cur_row:
            self.set_current(cur.row(), 0, scroll=False)

    def move_product(self, step: int):
        self.set_current(self.cur_row + step, 0)

    def move_image(self, step: int):
        pid = self._current_pid()
        if pid is None:
            return
        n = len(self.by_product.get(pid, []))
        img = self.cur_img + step
        if img >= n and self.cur_row < len(self.model.pids) - 1:
            self.set_current(self.cur_row + 1, 0)
        elif img < 0 and self.cur_row > 0:
            prev_n = len(self.by_product.get(self.model.pids[self.cur_row - 1], []))
            self.set_current(self.cur_row - 1, prev_n - 1)
        else:
            self.set_current(self.cur_row, img)

    def _goto_search(self):
        needle = self.search.text().strip()
        if not needle:
            return
        if needle not in self.by_product:
            self.status_message.emit(f"Product '{needle}' niet gevonden (of buiten de categoriekeuze).")
        elif needle not in self.model.pids:
            self.status_message.emit(f"Product '{needle}' valt buiten de huidige filter.")
        else:
            self.set_current(self.model.pids.index(needle), 0)
            self.view.scrollTo(self.model.index(self.cur_row), QAbstractItemView.PositionAtTop)
        self.view.setFocus()

    def _update_counter(self):
        n = len(self.model.pids)
        reviewed = int(self.table["decision"].notna().sum()) if not self.table.empty else 0
        total = len(self.table)
        self.counter.setText(
            f"product <b>{self.cur_row + 1 if n else 0}</b> / {n} in de lijst · "
            f"{reviewed} / {total} beelden gereviewd" if n else
            "<i>Geen producten voor deze filter. Kies categorieën in tab 1, download (en "
            "classificeer), of pas de filter aan.</i>")

    # ------------------------------------------------------------ beslissingen

    def _save(self, row_idx: int, decision: str | None, final_label: str | None, use: bool):
        if self.store is None:
            self.store = Store(self.cfg.db_path)
        r = self.table.loc[row_idx]
        if decision is None:
            self.store.clear_review(r["product_id"], r["slot"])
            self.table.at[row_idx, "decision"] = None
            self.table.at[row_idx, "final_label"] = None
            self.table.at[row_idx, "review_use"] = None
            label = r["label"] if isinstance(r["label"], str) else None
            self.table.at[row_idx, "eff_use"] = is_use_label(label) if label else None
            self.table.at[row_idx, "eff_label"] = label or ""
        else:
            self.store.set_review(r["product_id"], r["slot"], r["url"], decision, final_label, use)
            self.table.at[row_idx, "decision"] = decision
            self.table.at[row_idx, "final_label"] = final_label
            self.table.at[row_idx, "review_use"] = int(use)
            self.table.at[row_idx, "eff_use"] = use
            self.table.at[row_idx, "eff_label"] = final_label or UNKNOWN_LABEL

    def _changed(self):
        self.model.refresh_row(self.cur_row)
        self._update_counter()

    def accept_current(self):
        idx = self._current()
        if idx is None:
            return
        label = self.table.at[idx, "label"]
        if not isinstance(label, str):
            self.status_message.emit("Geen voorstel om te accepteren; kies een label met 1–7.")
            return
        self._save(idx, "accept", label, is_use_label(label))
        self._changed()
        self.move_image(+1)

    def deny_current(self):
        idx = self._current()
        if idx is None:
            return
        self._save(idx, "deny", None, False)
        self._changed()
        self.move_image(+1)

    def set_label_current(self, label: str):
        idx = self._current()
        if idx is None:
            return
        self._set_label(idx, label)
        self._changed()
        self.move_image(+1)

    def _set_label(self, idx: int, label: str):
        predicted = self.table.at[idx, "label"]
        self._save(idx, "accept" if label == predicted else "relabel", label, is_use_label(label))

    def toggle_use_current(self):
        idx = self._current()
        if idx is None:
            return
        r = self.table.loc[idx]
        use = not _on(r["eff_use"])
        decision = r["decision"] if isinstance(r["decision"], str) else None
        if decision is None:
            if not isinstance(r["label"], str):
                self.status_message.emit("Kies eerst een label (1–7) of gebruik P (als packshot kiezen).")
                return
            decision, final = "accept", r["label"]
        else:
            final = r["final_label"] if isinstance(r["final_label"], str) else None
        self._save(idx, decision, final, use)
        self._changed()

    def set_packshot_current(self):
        """Huidige afbeelding = packshot: vinkje aan, alle andere beelden van het
        product vinkje uit (bewaard als review)."""
        idx = self._current()
        pid = self._current_pid()
        if idx is None or pid is None:
            return
        if self.table.at[idx, "fetch_status"] != "ok":
            self.status_message.emit("Deze afbeelding is niet gedownload.")
            return
        for r in self.by_product[pid]:
            rec = self.table.loc[r]
            label = rec["final_label"] if isinstance(rec["decision"], str) else rec["label"]
            label = label if isinstance(label, str) and label != UNKNOWN_LABEL else None
            if r == idx:
                if label not in PACKSHOT_LABELS and label != "packshot":
                    label = "packshot_product"
                decision = "accept" if label == rec["label"] else "relabel"
                self._save(r, decision, label, True)
            elif _on(rec["eff_use"]):
                decision = rec["decision"] if isinstance(rec["decision"], str) else (
                    "accept" if label else "deny")
                self._save(r, decision, label, False)
        self._changed()
        if self.cfg.packshot_mode == "first":
            self.status_message.emit("Bewaard. Let op: in packshot-modus 'eerste beeld' gebruikt de "
                                     "attribuutverrijking altijd het eerste beeld (zie Instellingen).")

    def accept_product_current(self):
        pid = self._current_pid()
        if pid is None:
            return
        self._accept_products([pid], transaction=False)
        self._changed()
        if self.cur_row < len(self.model.pids) - 1:
            self.move_product(+1)

    def _accept_products(self, pids: list[str], transaction: bool = True) -> int:
        """Keurt alle nog niet gereviewde voorstellen van deze producten goed."""
        if self.store is None:
            self.store = Store(self.cfg.db_path)
        n = 0
        if transaction:
            self.store.conn.execute("BEGIN")
        try:
            for pid in pids:
                for r in self.by_product.get(pid, []):
                    rec = self.table.loc[r]
                    if not isinstance(rec["decision"], str) and isinstance(rec["label"], str):
                        self._save(r, "accept", rec["label"], is_use_label(rec["label"]))
                        n += 1
            if transaction:
                self.store.conn.execute("COMMIT")
        except Exception:
            if transaction:
                self.store.conn.execute("ROLLBACK")
            raise
        return n

    def accept_range(self, until_current: bool):
        if not self.model.pids:
            return
        pids = self.model.pids[: self.cur_row + 1] if until_current else list(self.model.pids)
        todo = sum(1 for pid in pids for r in self.by_product.get(pid, [])
                   if not isinstance(self.table.at[r, "decision"], str)
                   and isinstance(self.table.at[r, "label"], str))
        if not todo:
            self.status_message.emit("Niets meer goed te keuren in dit bereik.")
            return
        scope = f"de eerste {len(pids)} producten" if until_current else f"alle {len(pids)} producten"
        if QMessageBox.question(self, "Goedkeuren",
                                f"{todo} nog niet gereviewde voorstellen van {scope} in de lijst "
                                "goedkeuren?\n\nBeelden die je al aanpaste, blijven zoals ze zijn.") \
                != QMessageBox.Yes:
            return
        n = self._accept_products(pids)
        self.view.viewport().update()
        self._update_counter()
        self.status_message.emit(f"{n} voorstellen goedgekeurd.")
        self.view.setFocus()

    def undo_current(self):
        idx = self._current()
        if idx is not None:
            self._save(idx, None, None, False)
            self._changed()

    # ------------------------------------------------------------ menu

    def context_menu(self, global_pos):
        idx = self._current()
        if idx is None:
            return
        k = self.cfg.shortcut
        m = QMenu(self)
        m.addAction(f"Als packshot kiezen ({k('set_packshot')})", self.set_packshot_current)
        m.addAction(f"Vinkje 'gebruik' aan/uit ({k('toggle_use')})", self.toggle_use_current)
        m.addSeparator()
        m.addAction(f"Voorstel goedkeuren ({k('accept')})", self.accept_current)
        m.addAction(f"Voorstel afkeuren ({k('deny')})", self.deny_current)
        sub = m.addMenu("Label zetten")
        for i, lab in enumerate(LABELS):
            sub.addAction(f"{lab}  ({k(f'label_{i + 1}')})",
                          lambda lab=lab: (self._set_label(idx, lab), self._changed()))
        m.addSeparator()
        m.addAction(f"Review wissen ({k('undo')})", self.undo_current)
        m.addAction(f"Product goedkeuren en door ({k('accept_product')})", self.accept_product_current)
        m.exec(global_pos)
        self.view.setFocus()

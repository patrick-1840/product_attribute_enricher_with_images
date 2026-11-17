"""
Gedeelde Qt-onderdelen: achtergrondtaak (QThread), de start/pauze/stop-groep,
sorteerbare items en een kolomkop met Excel-achtige filters.
"""
from __future__ import annotations

import logging
import re

from PySide6.QtCore import QPoint, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QTreeWidgetItem, QVBoxLayout,
    QWidget, QWidgetAction,
)

from runcontrol import RunControl

logger = logging.getLogger(__name__)


class TaskWorker(QThread):
    """Draait een blokkerende taak (inlezen, downloaden, classificeren) buiten
    de UI-thread. ``fn(control, progress)`` krijgt de RunControl en een
    voortgangscallback mee."""

    progress = Signal(int, int, str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.control = RunControl()

    def run(self):
        try:
            result = self.fn(self.control, lambda d, t, m: self.progress.emit(d, t, m))
            self.finished_ok.emit(result)
        except Exception as exc:
            logger.exception("Taak mislukt")
            self.failed.emit(str(exc))


class StepBox(QGroupBox):
    """Groep met start/pauze/stop-knoppen en een voortgangsbalk."""

    def __init__(self, title: str, start_text: str):
        super().__init__(title)
        self.btn_start = QPushButton(start_text)
        self.btn_pause = QPushButton("Pauzeer")
        self.btn_stop = QPushButton("Stop")
        self.bar = QProgressBar()
        self.bar.setFormat("%v / %m")
        self.msg = QLabel()
        self.options = QHBoxLayout()
        row = QHBoxLayout()
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            row.addWidget(b)
        row.addLayout(self.options)
        row.addStretch()
        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(self.bar)
        lay.addWidget(self.msg)
        self.set_running(False)

    def set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.btn_pause.setText("Pauzeer")


# ================================================================= sorteren

#: Rol met een expliciete sorteersleutel (getal of tekst). Zonder deze rol
#: sorteert een cel op haar tekst: numeriek als die met een getal begint,
#: anders alfabetisch zonder hoofdlettergevoeligheid.
SORT_ROLE = Qt.UserRole + 100
_NUM_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)")


def sort_key(value):
    """(0, getal) sorteert vóór (1, tekst); lege waarden altijd achteraan."""
    if value is None or value == "":
        return (2, "")
    if isinstance(value, (int, float)):
        return (0, float(value))
    m = _NUM_RE.match(str(value))
    if m:
        return (0, float(m.group(1).replace(",", ".")))
    return (1, str(value).casefold())


class SortableTableItem(QTableWidgetItem):
    def __lt__(self, other):
        a = self.data(SORT_ROLE)
        b = other.data(SORT_ROLE)
        return sort_key(self.text() if a is None else a) < sort_key(other.text() if b is None else b)


class SortableTreeItem(QTreeWidgetItem):
    def __lt__(self, other):
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        a = self.data(col, SORT_ROLE)
        b = other.data(col, SORT_ROLE)
        return (sort_key(self.text(col) if a is None else a)
                < sort_key(other.text(col) if b is None else b))


def enable_sorting(view) -> None:
    """Klik op een kolomkop = oplopend, nog eens = aflopend. Start ZONDER
    sortering (de oorspronkelijke volgorde blijft tot je op een kop klikt)."""
    header = view.horizontalHeader() if hasattr(view, "horizontalHeader") else view.header()
    header.setSortIndicator(-1, Qt.AscendingOrder)
    header.setSortIndicatorShown(True)
    header.setSectionsClickable(True)
    view.setSortingEnabled(True)


# ================================================================= Excel-filter

EMPTY_VALUE = "(leeg)"
CHECKED_VALUE = "✔ aangevinkt"
UNCHECKED_VALUE = "☐ niet aangevinkt"


def cell_value(item) -> str:
    """Filterwaarde van een cel: tekst, of de vinkjesstatus bij een aanvinkbare cel."""
    if item is None:
        return EMPTY_VALUE
    if item.data(Qt.CheckStateRole) is not None:     # cel met een echt vinkje
        return CHECKED_VALUE if item.checkState() == Qt.Checked else UNCHECKED_VALUE
    return item.text().strip() or EMPTY_VALUE


class _FilterPopup(QMenu):
    """Menu zoals in Excel: sorteren, filter wissen, zoeken en een aanvinklijst."""

    def __init__(self, header: "ExcelFilterHeader", col: int):
        super().__init__(header)
        self.header, self.col = header, col
        table = header.table
        title = table.horizontalHeaderItem(col).text() if table.horizontalHeaderItem(col) else ""

        a = self.addAction("Sorteer oplopend (A → Z)")
        a.triggered.connect(lambda: header.sort(col, Qt.AscendingOrder))
        a = self.addAction("Sorteer aflopend (Z → A)")
        a.triggered.connect(lambda: header.sort(col, Qt.DescendingOrder))
        self.addSeparator()
        a = self.addAction(f"Filter wissen uit '{title}'")
        a.setEnabled(col in header.filters)
        a.triggered.connect(lambda: header.clear_filter(col))
        self.addSeparator()

        # waarden: alleen rijen die door de ANDERE kolomfilters komen (zoals Excel)
        counts: dict[str, int] = {}
        for r in range(table.rowCount()):
            if header.row_passes(r, skip_col=col):
                v = cell_value(table.item(r, col))
                counts[v] = counts.get(v, 0) + 1
        current = header.filters.get(col)

        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 4, 8, 8)
        self.search = QLineEdit(placeholderText="Zoeken…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        lay.addWidget(self.search)
        self.list = QListWidget()
        self.list.setMinimumSize(300, 260)
        self.all_item = QListWidgetItem("(Alles selecteren)")
        self.all_item.setFlags(self.all_item.flags() | Qt.ItemIsUserCheckable)
        self.list.addItem(self.all_item)
        for v in sorted(counts, key=lambda x: (x == EMPTY_VALUE, sort_key(x))):
            it = QListWidgetItem(f"{v}   ({counts[v]})")
            it.setData(Qt.UserRole, v)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if current is None or v in current else Qt.Unchecked)
            self.list.addItem(it)
        self._sync_all()
        self.list.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        row.addStretch()
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self._apply)
        cancel = QPushButton("Annuleren")
        cancel.clicked.connect(self.close)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)
        wa = QWidgetAction(self)
        wa.setDefaultWidget(box)
        self.addAction(wa)
        self.search.setFocus()

    def _values(self):
        return [self.list.item(i) for i in range(1, self.list.count())]

    def _sync_all(self):
        vis = [it for it in self._values() if not it.isHidden()]
        self.list.blockSignals(True)
        self.all_item.setCheckState(Qt.Checked if vis and all(it.checkState() == Qt.Checked for it in vis)
                                    else Qt.Unchecked)
        self.list.blockSignals(False)

    def _on_item_changed(self, item):
        if item is self.all_item:
            state = item.checkState()
            self.list.blockSignals(True)
            for it in self._values():
                if not it.isHidden():
                    it.setCheckState(state)
            self.list.blockSignals(False)
        else:
            self._sync_all()

    def _on_search(self, text: str):
        needle = text.strip().casefold()
        for it in self._values():
            hit = not needle or needle in it.data(Qt.UserRole).casefold()
            it.setHidden(not hit)
            if needle:      # zoals Excel: zoekresultaat = selectie
                self.list.blockSignals(True)
                it.setCheckState(Qt.Checked if hit else Qt.Unchecked)
                self.list.blockSignals(False)
        self._sync_all()

    def _apply(self):
        items = self._values()
        chosen = {it.data(Qt.UserRole) for it in items
                  if it.checkState() == Qt.Checked and not it.isHidden()}
        if len(chosen) == len(items) and not self.search.text().strip():
            self.header.clear_filter(self.col)
        else:
            self.header.set_filter(self.col, chosen)
        self.close()


class ExcelFilterHeader(QHeaderView):
    """Kolomkop met een filtermenu per kolom, zoals in Excel. Een klik op de
    kop opent het menu (sorteren + filteren); de rand slepen past de breedte
    aan. Gefilterde kolommen tonen een trechter. Filters verbergen rijen
    (``setRowHidden``) en worden na elke sortering opnieuw toegepast."""

    filters_changed = Signal()
    _GRIP = 5   # px rond een sectierand = breedte aanpassen

    def __init__(self, table: QTableWidget):
        super().__init__(Qt.Horizontal, table)
        self.table = table
        self.filters: dict[int, set[str]] = {}
        self._pressed: int | None = None
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        table.setHorizontalHeader(self)
        self.setSortIndicatorShown(True)
        self.setSortIndicator(-1, Qt.AscendingOrder)
        table.setSortingEnabled(True)
        # sorteren verplaatst de inhoud tussen rijen; verborgen rijen blijven per
        # rijnummer staan -> filter opnieuw toepassen
        table.model().layoutChanged.connect(self.apply)

    # --- filterlogica
    def row_passes(self, r: int, skip_col: int | None = None) -> bool:
        for col, allowed in self.filters.items():
            if col != skip_col and cell_value(self.table.item(r, col)) not in allowed:
                return False
        return True

    def apply(self, *_):
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(r, not self.row_passes(r))
        self.viewport().update()
        self.filters_changed.emit()

    def set_filter(self, col: int, allowed: set[str]):
        self.filters[col] = set(allowed)
        self.apply()

    def clear_filter(self, col: int):
        self.filters.pop(col, None)
        self.apply()

    def clear_all(self):
        self.filters.clear()
        self.apply()

    def visible_rows(self) -> list[int]:
        return [r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]

    def sort(self, col: int, order):
        self.table.sortByColumn(col, order)
        self.apply()

    # --- muis
    def _on_grip(self, pos) -> bool:
        x = pos.x()
        for i in range(self.count()):
            edge = self.sectionViewportPosition(i) + self.sectionSize(i)
            if abs(x - edge) <= self._GRIP:
                return True
        return False

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if event.button() == Qt.LeftButton and not self._on_grip(pos):
            self._pressed = self.logicalIndexAt(pos)
            return
        self._pressed = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pressed is not None:
            col = self._pressed
            self._pressed = None
            if col >= 0 and col == self.logicalIndexAt(event.position().toPoint()):
                x = self.sectionViewportPosition(col)
                menu = _FilterPopup(self, col)
                menu.exec(self.mapToGlobal(QPoint(x, self.height())))
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._on_grip(event.position().toPoint()):
            super().mouseDoubleClickEvent(event)    # dubbelklik op de rand = passende breedte

    # --- tekenen: ▾ of trechter rechts in elke kop
    def paintSection(self, painter: QPainter, rect, index: int):
        painter.save()
        super().paintSection(painter, rect, index)
        painter.restore()
        active = index in self.filters
        painter.save()
        painter.setPen(QColor("#2d7ff9") if active else QColor("#888888"))
        font = painter.font()
        if active:
            font.setBold(True)
        painter.setFont(font)
        box = rect.adjusted(rect.width() - 16, 0, -2, 0)
        painter.drawText(box, Qt.AlignCenter, "⏷" if not active else "⧩")
        painter.restore()

    def sectionSizeFromContents(self, index: int):
        size = super().sectionSizeFromContents(index)
        size.setWidth(size.width() + 16)
        return size

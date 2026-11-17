"""
Gedeelde Qt-onderdelen: achtergrondtaak (QThread) en de start/pauze/stop-groep.
"""
from __future__ import annotations

import logging

import re

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTableWidgetItem, QTreeWidgetItem,
    QVBoxLayout,
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

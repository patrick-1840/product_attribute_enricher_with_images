"""
Gedeelde Qt-onderdelen: achtergrondtaak (QThread) en de start/pauze/stop-groep.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

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

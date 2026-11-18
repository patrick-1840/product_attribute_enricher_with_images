"""
Opstartscherm: logo, naam, versienummer en een laadbalk die de echte
opstartstappen volgt (modules laden, tabs opbouwen, cache openen).

Gebruik (zie ``desktop_app.main``)::

    splash = SplashScreen(logo_path)
    splash.show()
    window = MainWindow(progress=splash.step)
    window.show()
    splash.finish(window)
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from config import APP_TITLE, APP_VERSION

W, H = 460, 360          # logische pixels
ACCENT = QColor("#2d7ff9")


def _card(logo_path: Path | None) -> QPixmap:
    """Achtergrond van het opstartscherm (logo, naam, versie), scherp op HiDPI."""
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    pm = QPixmap(int(W * dpr), int(H * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)

    card = QPainterPath()
    card.addRoundedRect(QRectF(1, 1, W - 2, H - 2), 14, 14)
    p.fillPath(card, QColor("#ffffff"))
    p.setPen(QColor("#d0d7e2"))
    p.drawPath(card)

    logo_size = 150
    if logo_path is not None and logo_path.is_file():
        logo = QPixmap(str(logo_path))
        if not logo.isNull():
            logo.setDevicePixelRatio(1.0)
            scaled = logo.scaled(int(logo_size * dpr), int(logo_size * dpr),
                                 Qt.KeepAspectRatio, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            p.drawPixmap(int((W - logo_size) / 2), 34, scaled)

    title = QFont()
    title.setPointSize(17)
    title.setBold(True)
    p.setFont(title)
    p.setPen(QColor("#1f2933"))
    p.drawText(QRectF(0, 200, W, 34), Qt.AlignCenter, APP_TITLE)

    ver = QFont()
    ver.setPointSize(10)
    p.setFont(ver)
    p.setPen(QColor("#6b7785"))
    p.drawText(QRectF(0, 236, W, 22), Qt.AlignCenter, f"versie {APP_VERSION}")
    p.end()
    return pm


class SplashScreen(QSplashScreen):
    def __init__(self, logo_path: Path | None = None):
        super().__init__(_card(logo_path), Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)   # afgeronde hoeken
        self._pct = 0
        self._msg = "Opstarten…"

    def step(self, pct: int, msg: str) -> None:
        """Laadbalk bijwerken (0–100) en meteen tekenen."""
        self._pct = max(0, min(100, int(pct)))
        self._msg = msg
        self.repaint()
        QApplication.processEvents()

    def drawContents(self, p: QPainter) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        bar = QRectF(48, 286, W - 96, 8)
        track = QPainterPath()
        track.addRoundedRect(bar, 4, 4)
        p.fillPath(track, QColor("#e6ebf2"))
        if self._pct:
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * self._pct / 100,
                                       bar.height()), 4, 4)
            p.fillPath(fill, ACCENT)
        f = QFont()
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(QColor("#6b7785"))
        p.drawText(QRectF(0, 302, W, 22), Qt.AlignCenter, self._msg)

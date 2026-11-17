"""
Desktop-app (PySide6/Qt) voor de Product Image Classifier.

Start met:  python desktop_app.py

Structuur:
  Tab 1 — Data & verwerking : Excel inlezen → downloaden → classificeren
                              (elk met voortgang, pauzeren/hervatten, stoppen)
                              → exporteren
  Tab 2 — Review           : per product de thumbnails beoordelen met sneltoetsen
  Tab 3 — Instellingen     : API-key, model, concurrency, drempel, cachemap,
                              lay-out en sneltoetsen
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# PySide6 vóór pandas importeren: op sommige Windows-machines botsen de
# numpy/pandas-DLL's met die van Qt als pandas eerst geladen wordt (stille crash).
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from config import (
    DEFAULT_SHORTCUTS, LOG_FILE, USER_ENV_FILE, ClassifierConfig, save_api_key, setup_logging,
)
from runcontrol import RunControl

setup_logging()
logger = logging.getLogger(__name__)


def _resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


# ================================================================= worker thread

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


# ================================================================= main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Product Image Classifier")
        self.resize(1400, 860)
        icon_path = _resource_dir() / "favicon" / "favicon.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.cfg = ClassifierConfig()
        self.cfg.load_user_settings()
        self.worker: TaskWorker | None = None
        self.active_box: StepBox | None = None

        from review_widget import ReviewWidget   # pas na QApplication/PySide6 laden
        self.review = ReviewWidget(self.cfg)
        self.review.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.review.export_requested.connect(self.export)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_data_tab(), "1 · Data && verwerking")
        self.tabs.addTab(self.review, "2 · Review")
        self.tabs.addTab(self._build_settings_tab(), "3 · Instellingen")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.refresh_stats()
        self.statusBar().showMessage(f"Klaar. Logbestand: {LOG_FILE}")

    # ------------------------------------------------------------ tab 1

    def _build_data_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # Inlezen
        g = QGroupBox("1. Productbestand inlezen")
        gl = QGridLayout(g)
        self.ed_file = QLineEdit(str(self.cfg.product_file))
        btn_pick = QPushButton("Bladeren…")
        btn_pick.clicked.connect(self._pick_product_file)
        self.btn_load = QPushButton("Inlezen")
        self.btn_load.clicked.connect(self.load_file)
        self.lbl_load = QLabel("Detecteert automatisch de kolommen 'product image N' / "
                               "'imageLinkN' (+ extra kolommen uit Instellingen).")
        self.lbl_load.setWordWrap(True)
        gl.addWidget(QLabel("Excel/CSV:"), 0, 0)
        gl.addWidget(self.ed_file, 0, 1)
        gl.addWidget(btn_pick, 0, 2)
        gl.addWidget(self.btn_load, 0, 3)
        gl.addWidget(self.lbl_load, 1, 0, 1, 4)
        lay.addWidget(g)

        # Downloaden
        self.box_fetch = StepBox("2. Afbeeldingen downloaden (thumbnails in de cachemap)",
                                 "Start downloaden")
        self.chk_retry = QCheckBox("mislukte opnieuw proberen")
        self.box_fetch.options.addWidget(self.chk_retry)
        self.box_fetch.btn_start.clicked.connect(self.start_fetch)
        self._wire_box(self.box_fetch)
        lay.addWidget(self.box_fetch)

        # Classificeren
        self.box_cls = StepBox("3. Classificeren met OpenAI", "Start classificatie")
        self.chk_sim = QCheckBox("simulatiemodus (geen API-kosten)")
        self.sp_limit = QSpinBox(minimum=0, maximum=1_000_000, specialValueText="geen limiet")
        self.sp_limit.setToolTip("Maximum aantal beelden in deze run (0 = alles). Handig om te testen.")
        self.lbl_model = QLabel()
        for wdg in (self.chk_sim, QLabel("limiet:"), self.sp_limit, self.lbl_model):
            self.box_cls.options.addWidget(wdg)
        self.box_cls.btn_start.clicked.connect(self.start_classify)
        self._wire_box(self.box_cls)
        lay.addWidget(self.box_cls)

        # Overzicht + export
        g = QGroupBox("Overzicht")
        gl = QHBoxLayout(g)
        self.lbl_stats = QLabel()
        self.lbl_stats.setTextFormat(Qt.RichText)
        btn_refresh = QPushButton("Vernieuwen")
        btn_refresh.clicked.connect(self.refresh_stats)
        btn_export = QPushButton("Exporteer als…")
        btn_export.clicked.connect(self.export)
        gl.addWidget(self.lbl_stats, 1)
        gl.addWidget(btn_refresh)
        gl.addWidget(btn_export)
        lay.addWidget(g)
        lay.addStretch()
        self._update_model_label()
        return w

    def _wire_box(self, box: StepBox):
        box.btn_pause.clicked.connect(lambda: self._toggle_pause(box))
        box.btn_stop.clicked.connect(self._stop)

    def _pick_product_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Productbestand kiezen",
                                              str(Path(self.ed_file.text()).parent),
                                              "Excel/CSV (*.xlsx *.xlsm *.csv)")
        if path:
            self.ed_file.setText(path)

    def _busy(self) -> bool:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Bezig", "Er loopt al een taak. Stop of wacht tot ze klaar is.")
            return True
        return False

    def _start(self, fn, box: StepBox | None, done_cb):
        self.worker = TaskWorker(fn)
        self.active_box = box
        if box:
            box.set_running(True)
            box.bar.setRange(0, 0)
            self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(done_cb)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_thread_done)
        self.worker.start()

    def _on_progress(self, done: int, total: int, msg: str):
        box = self.active_box
        if box is None:
            return
        box.bar.setRange(0, max(total, 1))
        box.bar.setValue(done)
        state = " (gepauzeerd)" if self.worker and self.worker.control.paused else ""
        box.msg.setText(msg + state)

    def _toggle_pause(self, box: StepBox):
        if not self.worker:
            return
        c = self.worker.control
        if c.paused:
            c.resume()
            box.btn_pause.setText("Pauzeer")
            box.msg.setText(box.msg.text().replace(" (gepauzeerd)", ""))
        else:
            c.pause()
            box.btn_pause.setText("Hervat")
            box.msg.setText(box.msg.text() + " (gepauzeerd — lopende requests ronden af)")

    def _stop(self):
        if self.worker:
            self.worker.control.cancel()
            self.statusBar().showMessage("Stoppen… lopende requests ronden af en worden bewaard.")

    def _on_failed(self, message: str):
        QMessageBox.critical(self, "Fout", f"{message}\n\nDetails in het logbestand:\n{LOG_FILE}")

    def _on_thread_done(self):
        if self.active_box:
            self.active_box.set_running(False)
        self.btn_load.setEnabled(True)
        self.refresh_stats()

    # --- inlezen
    def load_file(self):
        if self._busy():
            return
        path = Path(self.ed_file.text().strip().strip('"'))
        if not path.exists():
            QMessageBox.warning(self, "Bestand niet gevonden", f"{path}\n\nKies het productbestand "
                                "met 'Bladeren…'. (Staat het op SharePoint? Controleer of de map "
                                "via OneDrive gesynchroniseerd is.)")
            return
        self.cfg.product_file = path
        self.cfg.save_user_settings()
        cfg = self.cfg

        def task(control, progress):
            from loader import build_image_table, read_product_file
            from store import Store
            progress(0, 0, "Inlezen…")
            items, products, report = build_image_table(read_product_file(path), cfg)
            store = Store(cfg.db_path)
            try:
                store.replace_items(items, products)
            finally:
                store.close()
            return report

        self.btn_load.setEnabled(False)
        self.lbl_load.setText("Bezig met inlezen… (een groot Excel duurt even)")
        self._start(task, None, self._on_loaded)

    def _on_loaded(self, report):
        txt = "✔ " + report.summary()
        if report.invalid_urls:
            txt += f"<br>Voorbeelden ongeldig: {report.invalid_urls[:3]}"
        self.lbl_load.setText(txt)
        self.review.reload(keep_position=False)

    # --- downloaden
    def start_fetch(self):
        if self._busy():
            return
        cfg, retry = self.cfg, self.chk_retry.isChecked()

        def task(control, progress):
            from fetcher import ImageFetcher
            return ImageFetcher(cfg, control).run(retry_failed=retry, progress=progress)

        self._start(task, self.box_fetch, lambda r: self.box_fetch.msg.setText(
            f"{'Gestopt' if self.worker.control.cancelled else 'Klaar'}: {r['ok']} ok, "
            f"{r['failed']} mislukt (zie mislukte_urls.csv in de cachemap)."))

    # --- classificeren
    def start_classify(self):
        if self._busy():
            return
        self.cfg.use_openai = not self.chk_sim.isChecked()
        if self.cfg.use_openai and not self.cfg.openai_api_key:
            QMessageBox.warning(self, "Geen API-key", "Vul eerst je OpenAI API-key in bij Instellingen.")
            self.tabs.setCurrentIndex(2)
            return
        cfg, limit = self.cfg, self.sp_limit.value() or None

        def task(control, progress):
            from classifier import ImageClassifier
            return ImageClassifier(cfg, control).run(limit=limit, progress=progress)

        self._start(task, self.box_cls, self._on_classified)

    def _on_classified(self, r):
        msg = (f"{'Gestopt' if self.worker.control.cancelled else 'Klaar'}: {r['ok']} ok, "
               f"{r['failed']} mislukt, {r.get('escalated', 0)} geëscaleerd.")
        self.box_cls.msg.setText(msg)
        if r.get("quota"):
            QMessageBox.critical(self, "Geen OpenAI-tegoed",
                                 "OpenAI meldt 'insufficient_quota'. Wachten lost dit niet op; "
                                 "controleer de billing op platform.openai.com. De classificatie "
                                 "is gestopt; reeds verwerkte beelden zijn bewaard.")

    def _update_model_label(self):
        esc = (f", escalatie naar {self.cfg.escalation_model} onder {self.cfg.confidence_threshold:.2f}"
               if self.cfg.escalate else "")
        self.lbl_model.setText(f"model: {self.cfg.openai_model}{esc}")

    # --- overzicht
    def refresh_stats(self):
        try:
            from store import Store
            store = Store(self.cfg.db_path)
            s = store.stats()
            store.close()
        except Exception as exc:
            self.lbl_stats.setText(f"Kon cache niet openen: {exc}")
            return
        u = max(s["unique_urls"], 1)
        self.lbl_stats.setText(
            f"<b>{s['products']}</b> producten · <b>{s['items']}</b> beeldverwijzingen · "
            f"<b>{s['unique_urls']}</b> unieke URL's<br>"
            f"Gedownload: <b>{s['fetched']}</b> ({s['fetched'] / u:.0%}) · mislukt: {s['fetch_failed']}<br>"
            f"Geclassificeerd: <b>{s['classified']}</b> ({s['classified'] / u:.0%}) · "
            f"mislukt: {s['classify_failed']}<br>"
            f"Gereviewd: <b>{s['reviewed']}</b> van {s['items']}<br>"
            f"<span style='color:#888'>Cachemap: {self.cfg.cache_dir}</span>")

    # --- export
    def export(self):
        if self._busy():
            return
        src = Path(self.cfg.product_file)
        suffix = ".csv" if src.suffix.lower() == ".csv" else ".xlsx"
        default = Path(self.cfg.last_export_dir) / f"{src.stem}_met_beeldlabels{suffix}"
        path, _ = QFileDialog.getSaveFileName(self, "Exporteer als…", str(default),
                                              f"Bestand (*{suffix})")
        if not path:
            return
        cfg = self.cfg

        def task(control, progress):
            from exporter import export_excel
            progress(0, 0, "Exporteren…")
            return export_excel(cfg, src, Path(path))

        self.cfg.last_export_dir = Path(path).parent
        self.cfg.save_user_settings()
        self.statusBar().showMessage("Bezig met exporteren…")
        self._start(task, None, lambda r: QMessageBox.information(
            self, "Export klaar", f"{r[0]} rijen met beelden geschreven naar:\n{path}\n\n"
                                  f"Correctielog (trainingsdata):\n{r[1]}"))

    def _on_tab_changed(self, i: int):
        if self.tabs.widget(i) is self.review:
            self.review.reload()
        elif i == 0:
            self.refresh_stats()

    # ------------------------------------------------------------ tab 3

    def _build_settings_tab(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        w = QWidget()
        outer.setWidget(w)
        lay = QVBoxLayout(w)

        g = QGroupBox("OpenAI")
        f = QFormLayout(g)
        self.ed_key = QLineEdit(echoMode=QLineEdit.Password)
        self.ed_key.setPlaceholderText("sk-… (laat leeg om de huidige key te behouden)")
        key_state = "ingesteld" if self.cfg.openai_api_key else "NIET ingesteld"
        f.addRow("API-key:", self.ed_key)
        f.addRow("", QLabel(f"Huidige key: {key_state}. Wordt bewaard in {USER_ENV_FILE} "
                            "(nooit in settings.json)."))
        self.cb_model = QComboBox(editable=True)
        self.cb_model.addItems(["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "gpt-5-mini"])
        self.cb_model.setCurrentText(self.cfg.openai_model)
        f.addRow("Model:", self.cb_model)
        self.sp_conc = QSpinBox(minimum=1, maximum=50, value=self.cfg.concurrent_requests)
        f.addRow("Gelijktijdige API-calls:", self.sp_conc)
        self.chk_esc = QCheckBox("herclassificeer onder de confidence-drempel met:")
        self.chk_esc.setChecked(self.cfg.escalate)
        self.cb_esc_model = QComboBox(editable=True)
        self.cb_esc_model.addItems(["gpt-4o", "gpt-4.1", "gpt-5"])
        self.cb_esc_model.setCurrentText(self.cfg.escalation_model)
        row = QHBoxLayout()
        row.addWidget(self.chk_esc)
        row.addWidget(self.cb_esc_model)
        row.addStretch()
        f.addRow("Escalatie:", row)
        self.sp_thr = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=1.0, singleStep=0.05,
                                     value=self.cfg.confidence_threshold)
        f.addRow("Confidence-drempel:", self.sp_thr)
        lay.addWidget(g)

        g = QGroupBox("Bestanden en downloaden")
        f = QFormLayout(g)
        self.ed_cache = QLineEdit(str(self.cfg.cache_dir))
        btn = QPushButton("Bladeren…")
        btn.clicked.connect(self._pick_cache_dir)
        row = QHBoxLayout()
        row.addWidget(self.ed_cache)
        row.addWidget(btn)
        f.addRow("Cachemap:", row)
        f.addRow("", QLabel("Bevat de thumbnails en state.sqlite (downloads, labels, reviews). "
                            "Liefst NIET in OneDrive (tienduizenden bestanden)."))
        self.sp_dl = QSpinBox(minimum=1, maximum=50, value=self.cfg.concurrent_downloads)
        f.addRow("Gelijktijdige downloads:", self.sp_dl)
        self.ed_id = QLineEdit(self.cfg.id_column)
        f.addRow("Product-id-kolom:", self.ed_id)
        self.ed_extra = QLineEdit(", ".join(self.cfg.extra_image_columns))
        f.addRow("Extra beeldkolommen:", self.ed_extra)
        self.ed_info = QLineEdit(", ".join(self.cfg.info_columns))
        f.addRow("Infokolommen in review:", self.ed_info)
        lay.addWidget(g)

        g = QGroupBox("Review: lay-out en sneltoetsen")
        f = QFormLayout(g)
        self.cb_orient = QComboBox()
        self.cb_orient.addItem("horizontaal (naast elkaar, ←/→)", "horizontal")
        self.cb_orient.addItem("verticaal (onder elkaar, ↑/↓)", "vertical")
        self.cb_orient.setCurrentIndex(1 if self.cfg.layout_orientation == "vertical" else 0)
        f.addRow("Oriëntatie:", self.cb_orient)
        self.key_edits: dict[str, QKeySequenceEdit] = {}
        for action, (desc, _default) in DEFAULT_SHORTCUTS.items():
            ed = QKeySequenceEdit(QKeySequence(self.cfg.shortcut(action)))
            ed.setMaximumSequenceLength(1)
            self.key_edits[action] = ed
            f.addRow(desc + ":", ed)
        btn_reset = QPushButton("Standaardtoetsen herstellen")
        btn_reset.clicked.connect(self._reset_keys)
        f.addRow("", btn_reset)
        lay.addWidget(g)

        btn_save = QPushButton("Instellingen opslaan")
        btn_save.clicked.connect(self._save_settings)
        lay.addWidget(btn_save)
        lay.addWidget(QLabel(f"Instellingen worden bewaard in %APPDATA%/ProductImageClassifier. "
                             f"Logbestand: {LOG_FILE}"))
        lay.addStretch()
        return outer

    def _pick_cache_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Cachemap kiezen", self.ed_cache.text())
        if d:
            self.ed_cache.setText(d)

    def _reset_keys(self):
        for action, (_d, default) in DEFAULT_SHORTCUTS.items():
            self.key_edits[action].setKeySequence(QKeySequence(default))

    def _save_settings(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Bezig", "Wacht tot de lopende taak klaar is.")
            return
        keys = {a: ed.keySequence().toString() for a, ed in self.key_edits.items()}
        # dubbele toewijzingen binnen dezelfde lay-out weigeren
        vertical = self.cb_orient.currentData() == "vertical"
        skip = {"next_h", "prev_h"} if vertical else {"next_v", "prev_v"}
        active = [(a, k) for a, k in keys.items() if k and a not in skip]
        seen: dict[str, str] = {}
        for a, k in active:
            if k in seen:
                QMessageBox.warning(self, "Dubbele sneltoets",
                                    f"'{k}' is zowel toegewezen aan '{DEFAULT_SHORTCUTS[seen[k]][0]}' "
                                    f"als aan '{DEFAULT_SHORTCUTS[a][0]}'.")
                return
            seen[k] = a

        if self.ed_key.text().strip():
            save_api_key(self.ed_key.text())
            self.ed_key.clear()
        old_cache = Path(self.cfg.cache_dir)
        self.cfg.openai_model = self.cb_model.currentText().strip()
        self.cfg.concurrent_requests = self.sp_conc.value()
        self.cfg.escalate = self.chk_esc.isChecked()
        self.cfg.escalation_model = self.cb_esc_model.currentText().strip()
        self.cfg.confidence_threshold = self.sp_thr.value()
        self.cfg.cache_dir = Path(self.ed_cache.text().strip())
        self.cfg.concurrent_downloads = self.sp_dl.value()
        self.cfg.id_column = self.ed_id.text().strip() or "variant_code"
        self.cfg.extra_image_columns = [c.strip() for c in self.ed_extra.text().split(",") if c.strip()]
        self.cfg.info_columns = [c.strip() for c in self.ed_info.text().split(",") if c.strip()]
        self.cfg.layout_orientation = self.cb_orient.currentData()
        self.cfg.shortcuts = keys
        self.cfg.save_user_settings()

        if Path(self.cfg.cache_dir) != old_cache:
            self.review.reset_store()
        self.review.f_conf.setValue(self.cfg.confidence_threshold)
        self.review.apply_settings()
        self._update_model_label()
        self.refresh_stats()
        self.statusBar().showMessage("Instellingen opgeslagen.", 5000)


def main():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "ASWatson.ProductImageClassifier")
        except OSError:
            pass
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icon_path = _resource_dir() / "favicon" / "favicon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

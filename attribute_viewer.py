"""
Attribuutoverzicht — apart programma om duizenden producten snel visueel te
valideren: kies een categorie en een productattribuut en scroll door alle
producten met foto, merk, naam, de attribuutwaarde en de productomschrijving.

Start met:  python attribute_viewer.py

- Leest hetzelfde productbestand en dezelfde referentiebestanden als de
  Product Image Classifier (instellingen in %APPDATA%/ProductImageClassifier).
- Foto's komen uit dezelfde thumbnailcache; wat ontbreekt, wordt pas
  gedownload als de rij op het scherm komt (max. 8 tegelijk).
- Het ingelezen Excel wordt gecachet (pickle in de cachemap) zolang het
  bestand niet wijzigt: de tweede start is dus snel.
- Alleen-lezen: dit programma wijzigt niets aan de data.

Bediening: ↑/↓/PgUp/PgDown/muiswiel = scrollen · dubbelklik = productpagina
openen · Ctrl+C = geselecteerde rijen kopiëren (plakken in Excel/mail).
"""
from __future__ import annotations

import html
import logging
import re
import sys
import urllib.request
from pathlib import Path

# PySide6 vóór pandas importeren (DLL-conflict op Windows, zie desktop_app.py)
from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QRect, QRunnable, QSize, Qt, QThreadPool, QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPixmap,
    QPixmapCache, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListView, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSpinBox, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from config import APP_VERSION, LOG_FILE, VISUAL_ATTRIBUTES, ClassifierConfig, setup_logging
from qt_common import TaskWorker

setup_logging()
logger = logging.getLogger("attribute_viewer")

TITLE = "Attribuutoverzicht"
CATEGORY_COLUMN = "mainCategory"
IMAGE_COLUMNS = ["mainImageLink", "imageLink1", "imageLink2", "imageLink3", "imageLink4", "imageLink5"]
DESCRIPTION_COLUMNS = ["description", "prod.info.descript"]
ALL_VALUES, EMPTY, FILLED = "alle waarden", "(leeg)", "(gevuld)"
ACCENT = QColor("#2d7ff9")
QPixmapCache.setCacheLimit(200 * 1024)
_TAG_RE = re.compile(r"<[^>]+>")


def _resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _clean(text) -> str:
    """HTML-tags en -entiteiten weg, witruimte samenvoegen (omschrijvingen)."""
    if not isinstance(text, str):
        return ""
    t = html.unescape(html.unescape(text))          # bronbestand bevat soms &amp;apos;
    t = _TAG_RE.sub(" ", t)
    return " ".join(t.split())


def _is_empty(v) -> bool:
    return not isinstance(v, str) or v.strip().lower() in ("", "nan", "none")


def _split(v) -> list[str]:
    return [p.strip() for p in str(v).split(",") if p.strip()] if not _is_empty(v) else []


# ================================================================= data laden

def load_catalog(cfg: ClassifierConfig, path: Path, progress) -> "pd.DataFrame":
    """Productbestand inlezen, met een pickle-cache zolang het bestand niet wijzigt."""
    import pandas as pd
    from loader import read_product_file
    cache = Path(cfg.cache_dir) / "viewer_catalog.pkl"
    stat = path.stat()
    key = f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    if cache.is_file():
        try:
            cached = pd.read_pickle(cache)
            if cached.attrs.get("source_key") == key:
                progress(0, 0, "Uit cache geladen")
                return cached
        except Exception as exc:  # noqa: BLE001 - cache is optioneel
            logger.info("Viewer-cache onbruikbaar (%s); opnieuw inlezen.", exc)
    progress(0, 0, "Productbestand inlezen… (eerste keer duurt even)")
    df = read_product_file(path)
    df.attrs["source_key"] = key
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_pickle(cache)
    except OSError as exc:
        logger.warning("Kon viewer-cache niet bewaren: %s", exc)
    return df


# ================================================================= thumbnails

class _Signals(QObject):
    ready = Signal(str)          # url
    failed = Signal(str)


class _Download(QRunnable):
    def __init__(self, url: str, target: Path, cfg: ClassifierConfig, signals: _Signals):
        super().__init__()
        self.url, self.target, self.cfg, self.signals = url, target, cfg, signals

    def run(self):
        from fetcher import USER_AGENT, make_thumbnail
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            make_thumbnail(data, self.target, self.cfg.thumb_max_px, self.cfg.thumb_quality)
            self.signals.ready.emit(self.url)
        except Exception as exc:  # noqa: BLE001
            logger.info("Thumbnail mislukt voor %s: %s", self.url, exc)
            self.signals.failed.emit(self.url)


class ThumbLoader(QObject):
    """Haalt ontbrekende thumbnails op de achtergrond op (alleen voor zichtbare rijen)."""
    ready = Signal(str)

    def __init__(self, cfg: ClassifierConfig, parent=None):
        super().__init__(parent)
        from fetcher import thumb_name
        self._thumb_name = thumb_name
        self.cfg = cfg
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(8)
        self.pending: set[str] = set()
        self.failed: set[str] = set()
        self.signals = _Signals()
        self.signals.ready.connect(self._done)
        self.signals.failed.connect(self._fail)

    def path(self, url: str) -> Path:
        return Path(self.cfg.thumbs_dir) / self._thumb_name(url)

    def pixmap(self, url: str, size: int) -> QPixmap | None:
        """Thumbnail in de gevraagde grootte, of None (en dan wordt hij gedownload)."""
        if not url:
            return None
        key = f"{url}|{size}"
        pm = QPixmapCache.find(key)
        if pm is not None and not pm.isNull():
            return pm
        p = self.path(url)
        if p.is_file():
            pm = QPixmap(str(p))
            if not pm.isNull():
                pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                QPixmapCache.insert(key, pm)
                return pm
        if url not in self.pending and url not in self.failed:
            self.pending.add(url)
            self.pool.start(_Download(url, p, self.cfg, self.signals))
        return None

    def cancel_pending(self):
        """Wachtrij leegmaken (bv. na een andere filter: andere rijen zichtbaar)."""
        self.pool.clear()
        self.pending.clear()

    def _done(self, url: str):
        self.pending.discard(url)
        self.ready.emit(url)

    def _fail(self, url: str):
        self.pending.discard(url)
        self.failed.add(url)
        self.ready.emit(url)


# ================================================================= lijst

class RowModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []
        self._by_url: dict[str, list[int]] = {}

    def set_rows(self, rows: list[dict]):
        self.beginResetModel()
        self.rows = rows
        self._by_url = {}
        for i, r in enumerate(rows):
            self._by_url.setdefault(r["image"], []).append(i)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        if role == Qt.ToolTipRole:
            return f"<b>{html.escape(r['brand'])}</b> {html.escape(r['name'])}<br>" \
                   f"<i>{html.escape(r['value'] or EMPTY)}</i><br><br>{html.escape(r['desc'][:1500])}"
        if role == Qt.DisplayRole:
            return r["variant"]
        return None

    def url_changed(self, url: str):
        for i in self._by_url.get(url, []):
            idx = self.index(i)
            self.dataChanged.emit(idx, idx)


class RowDelegate(QStyledItemDelegate):
    def __init__(self, viewer: "ViewerWindow"):
        super().__init__(viewer)
        self.v = viewer

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.v.row_height())

    def paint(self, p: QPainter, option, index):
        r = self.v.model.rows[index.row()]
        rect = option.rect
        t = self.v.sp_size.value()
        p.save()
        selected = option.state & QStyle.State_Selected
        p.fillRect(rect, QColor("#dbe8ff") if selected else
                   QColor("#ffffff" if index.row() % 2 == 0 else "#f6f7f9"))

        # foto
        box = QRect(rect.left() + 4, rect.top() + 4, t, t)
        p.fillRect(box, QColor("#ffffff"))
        pm = self.v.thumbs.pixmap(r["image"], t)
        if pm is not None:
            p.drawPixmap(box.left() + (t - pm.width()) // 2, box.top() + (t - pm.height()) // 2, pm)
        else:
            p.setPen(QColor("#bbbbbb"))
            p.drawRect(box.adjusted(0, 0, -1, -1))
            if r["image"] in self.v.thumbs.failed or not r["image"]:
                p.drawText(box, Qt.AlignCenter, "geen foto")

        # tekst
        base = QFont(option.font)
        fm = QFontMetrics(base)
        lh = fm.height()
        x = box.right() + 10
        w = rect.right() - x - 8
        y = rect.top() + 4

        bold = QFont(base)
        bold.setBold(True)
        p.setFont(base)
        p.setPen(QColor("#8a94a1"))
        vtxt = r["variant"]
        vw = fm.horizontalAdvance(vtxt) + 4
        p.drawText(QRect(rect.right() - vw - 6, y, vw, lh), Qt.AlignRight | Qt.AlignVCenter, vtxt)
        bfm = QFontMetrics(bold)
        brand = r["brand"]
        p.setFont(bold)
        p.setPen(QColor("#1f2933"))
        brand_w = min(bfm.horizontalAdvance(brand), w // 3)
        p.drawText(QRect(x, y, brand_w, lh), Qt.AlignLeft | Qt.AlignVCenter,
                   bfm.elidedText(brand, Qt.ElideRight, brand_w))
        p.setFont(base)
        p.drawText(QRect(x + brand_w + 8, y, w - brand_w - vw - 16, lh), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(r["name"], Qt.ElideRight, max(0, w - brand_w - vw - 16)))
        y += lh + 1

        # attribuutwaarde
        p.setFont(bold)
        if r["value"]:
            p.setPen(ACCENT)
            txt = r["value"]
        else:
            p.setPen(QColor("#c0392b"))
            txt = EMPTY
        p.drawText(QRect(x, y, w, lh), Qt.AlignLeft | Qt.AlignVCenter,
                   bfm.elidedText(f"{self.v.attr_label}: {txt}", Qt.ElideRight, w))
        y += lh + 1

        # omschrijving: zoveel regels als er plaats is
        p.setFont(base)
        p.setPen(QColor("#4a5562"))
        lines = max(1, (rect.bottom() - 4 - y) // lh)
        if r["desc"]:
            text_rect = QRect(x, y, w, lines * lh)
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                       self._fit(fm, r["desc"], w, lines))
        p.setPen(QColor("#e3e7ec"))
        p.drawLine(rect.bottomLeft(), rect.bottomRight())
        p.restore()

    @staticmethod
    def _fit(fm: QFontMetrics, text: str, width: int, lines: int) -> str:
        """Tekst inkorten tot ``lines`` regels (met … op het einde)."""
        words, out, cur = text.split(), [], ""
        for wd in words:
            test = f"{cur} {wd}".strip()
            if fm.horizontalAdvance(test) <= width:
                cur = test
                continue
            out.append(cur)
            cur = wd
            if len(out) == lines:
                break
        if len(out) < lines and cur:
            out.append(cur)
            cur = ""
        result = " ".join(out[:lines])
        if len(out) >= lines and (cur or len(" ".join(out)) < len(text)):
            last = fm.elidedText(out[lines - 1] + " …", Qt.ElideRight, width)
            result = " ".join(out[:lines - 1] + [last])
        return result


class ListView(QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)


# ================================================================= venster

class ViewerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{TITLE} {APP_VERSION}")
        self.resize(1200, 900)
        icon = _resource_dir() / "favicon" / "favicon.ico"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.cfg = ClassifierConfig()
        self.cfg.load_user_settings()
        self.catalog = None
        self.ref = None
        self.worker: TaskWorker | None = None
        self.attr_label = ""
        self.thumbs = ThumbLoader(self.cfg, self)

        # --- één compacte balk
        self.btn_file = QPushButton("Bestand…")
        self.btn_file.setToolTip(str(self.cfg.product_file))
        self.btn_file.clicked.connect(self._pick_file)
        self.cb_cat = QComboBox()
        self.cb_cat.setMinimumContentsLength(14)
        self.cb_attr = QComboBox()
        self.cb_attr.setMinimumContentsLength(22)
        self.cb_val = QComboBox()
        self.cb_val.setMinimumContentsLength(18)
        self.cb_val.setMaxVisibleItems(30)
        self.ed_search = QLineEdit(placeholderText="zoek merk / naam / variant…")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.setMinimumWidth(180)
        self.cb_sort = QComboBox()
        self.cb_sort.addItems(["sorteer: waarde", "sorteer: merk", "sorteer: naam", "sorteer: bestand"])
        self.sp_size = QSpinBox(minimum=40, maximum=240, singleStep=16, suffix=" px")
        self.sp_size.setValue(72)
        self.sp_size.setToolTip("Fotogrootte (kleiner = meer producten per scherm)")
        self.lbl_count = QLabel()
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        bar.setSpacing(6)
        for wdg in (self.btn_file, self.cb_cat, self.cb_attr, self.cb_val, self.ed_search,
                    self.cb_sort, self.sp_size):
            bar.addWidget(wdg)
        bar.setStretch(4, 1)
        bar.addWidget(self.lbl_count)

        self.model = RowModel(self)
        self.view = ListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(RowDelegate(self))
        self.view.doubleClicked.connect(self._open_url)
        self.thumbs.ready.connect(self.model.url_changed)
        QShortcut(QKeySequence.Copy, self.view, activated=self._copy)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        self.setCentralWidget(central)
        self.busy = QProgressBar(maximumWidth=160)
        self.busy.setRange(0, 0)
        self.busy.hide()
        self.statusBar().addPermanentWidget(self.busy)
        self.statusBar().showMessage("Dubbelklik = productpagina · Ctrl+C = geselecteerde rijen kopiëren")

        self.cb_cat.currentIndexChanged.connect(self._fill_attributes)
        self.cb_attr.currentIndexChanged.connect(self._fill_values)
        self.cb_val.currentIndexChanged.connect(self._apply)
        self.cb_sort.currentIndexChanged.connect(self._apply)
        self.ed_search.textChanged.connect(self._apply)
        self.sp_size.valueChanged.connect(lambda: self.model.layoutChanged.emit())
        for w in (self.cb_cat, self.cb_attr, self.cb_val):
            w.setEnabled(False)
        self._load(Path(self.cfg.product_file))

    def row_height(self) -> int:
        lh = QFontMetrics(self.view.font()).height()
        return max(self.sp_size.value(), 3 * lh + 4) + 8

    # ------------------------------------------------------------ laden
    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Productbestand kiezen",
                                              str(Path(self.cfg.product_file).parent),
                                              "Excel/CSV (*.xlsx *.xlsm *.csv)")
        if path:
            self._load(Path(path))

    def _load(self, path: Path):
        if not path.exists():
            self.lbl_count.setText("Productbestand niet gevonden — kies het met 'Bestand…'.")
            return
        if self.worker is not None and self.worker.isRunning():
            return
        cfg = self.cfg

        def task(control, progress):
            catalog = load_catalog(cfg, path, progress)
            ref, err = None, ""
            try:
                from reference import load_reference_data
                ref = load_reference_data(cfg)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            return catalog, ref, err

        self.busy.show()
        self.lbl_count.setText("Bezig met inlezen…")
        self.worker = TaskWorker(task)
        self.worker.progress.connect(lambda d, t, m: self.lbl_count.setText(m))
        self.worker.finished_ok.connect(lambda res: self._loaded(path, *res))
        self.worker.failed.connect(lambda m: (self.busy.hide(), QMessageBox.critical(
            self, "Fout", f"{m}\n\nDetails: {LOG_FILE}")))
        self.worker.start()

    def _loaded(self, path: Path, catalog, ref, err: str):
        self.busy.hide()
        self.catalog, self.ref = catalog, ref
        self.btn_file.setToolTip(str(path))
        if err:
            self.statusBar().showMessage(f"Referentiebestanden niet geladen ({err}); alle kolommen "
                                         "worden als attribuut aangeboden.", 15000)
        # beeld-URL en opgeschoonde omschrijving één keer voorbereiden
        img = None
        for col in IMAGE_COLUMNS:
            if col in catalog.columns:
                vals = catalog[col].where(catalog[col].astype(str).str.startswith("http"), None)
                img = vals if img is None else img.fillna(vals)
        catalog["_image"] = img.fillna("") if img is not None else ""
        desc_col = next((c for c in DESCRIPTION_COLUMNS if c in catalog.columns), None)
        catalog["_desc"] = catalog[desc_col].map(_clean) if desc_col else ""
        self.cb_cat.blockSignals(True)
        self.cb_cat.clear()
        counts = catalog[CATEGORY_COLUMN].astype(str).str.strip().value_counts()
        for cat, n in counts.items():
            if cat:
                self.cb_cat.addItem(f"{cat} ({n})", cat)
        self.cb_cat.blockSignals(False)
        for w in (self.cb_cat, self.cb_attr, self.cb_val):
            w.setEnabled(True)
        i = self.cb_cat.findData("PERFUME")
        self.cb_cat.setCurrentIndex(max(i, 0))
        self._fill_attributes()

    # ------------------------------------------------------------ keuzes
    def _rows_of_category(self):
        cat = self.cb_cat.currentData()
        c = self.catalog
        return c[c[CATEGORY_COLUMN].astype(str).str.strip() == cat] if cat else c.iloc[0:0]

    def _fill_attributes(self, *_):
        if self.catalog is None:
            return
        rows = self._rows_of_category()
        cols = {str(c).strip().lower(): c for c in self.catalog.columns}
        if self.ref is not None:
            keys = [k for k in self.ref.allowed_attributes_for_category(self.cb_cat.currentData() or "")
                    if k in cols]
            name = self.ref.display_name
        else:
            skip = {c.lower() for c in IMAGE_COLUMNS + DESCRIPTION_COLUMNS}
            keys = [k for k in cols if not k.startswith("_") and k not in skip]
            name = lambda k: cols[k]  # noqa: E731
        current = self.cb_attr.currentData()
        self.cb_attr.blockSignals(True)
        self.cb_attr.clear()
        for k in sorted(keys, key=lambda k: (k not in VISUAL_ATTRIBUTES, name(k).lower())):
            col = cols[k]
            filled = int((~rows[col].map(_is_empty)).sum()) if len(rows) else 0
            pct = round(100 * filled / len(rows)) if len(rows) else 0
            eye = "👁 " if k in VISUAL_ATTRIBUTES else ""
            self.cb_attr.addItem(f"{eye}{name(k)}  ({pct}% gevuld)", col)
        i = self.cb_attr.findData(current)
        self.cb_attr.setCurrentIndex(max(i, 0))
        self.cb_attr.blockSignals(False)
        self._fill_values()

    def _fill_values(self, *_):
        col = self.cb_attr.currentData()
        self.attr_label = (self.cb_attr.currentText().split("  (")[0].replace("👁 ", "") if col else "")
        rows = self._rows_of_category()
        self.cb_val.blockSignals(True)
        self.cb_val.clear()
        self.cb_val.addItem(ALL_VALUES)
        if col is not None and len(rows):
            counts: dict[str, int] = {}
            n_empty = 0
            for v in rows[col]:
                parts = _split(v)
                if not parts:
                    n_empty += 1
                for part in parts:
                    counts[part] = counts.get(part, 0) + 1
            self.cb_val.addItem(f"{EMPTY}  ({n_empty})", EMPTY)
            self.cb_val.addItem(f"{FILLED}  ({len(rows) - n_empty})", FILLED)
            for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower())):
                self.cb_val.addItem(f"{v}  ({n})", v)
        self.cb_val.blockSignals(False)
        self._apply()

    def _apply(self, *_):
        if self.catalog is None:
            return
        col = self.cb_attr.currentData()
        rows = self._rows_of_category()
        if col is None:
            self.model.set_rows([])
            self.lbl_count.setText("0 producten")
            return
        want = self.cb_val.currentData()
        needle = self.ed_search.text().strip().casefold()
        out = []
        for rec in rows[["variant_code", "brand", "productName", col, "_image", "_desc", "productUrl"]
                        ].itertuples(index=False, name=None):
            variant, brand, name, value, image, desc, url = rec
            value = "" if _is_empty(value) else str(value).strip()
            if want == EMPTY and value:
                continue
            if want == FILLED and not value:
                continue
            if want not in (None, EMPTY, FILLED) and want not in _split(value):
                continue
            if needle and needle not in f"{brand} {name} {variant}".casefold():
                continue
            out.append({"variant": str(variant), "brand": str(brand or ""), "name": str(name or ""),
                        "value": value, "image": image or "", "desc": desc or "", "url": url or ""})
        sort = self.cb_sort.currentIndex()
        if sort == 0:
            out.sort(key=lambda r: (r["value"] == "", r["value"].lower(), r["brand"].lower(), r["name"].lower()))
        elif sort == 1:
            out.sort(key=lambda r: (r["brand"].lower(), r["name"].lower()))
        elif sort == 2:
            out.sort(key=lambda r: r["name"].lower())
        self.thumbs.cancel_pending()
        self.model.set_rows(out)
        self.view.scrollToTop()
        self.lbl_count.setText(f"<b>{len(out)}</b> / {len(rows)} producten")

    # ------------------------------------------------------------ acties
    def _open_url(self, index):
        url = self.model.rows[index.row()]["url"]
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _copy(self):
        rows = sorted({i.row() for i in self.view.selectedIndexes()})
        if not rows:
            return
        lines = ["variant_code\tbrand\tproductName\t" + self.attr_label]
        for i in rows:
            r = self.model.rows[i]
            lines.append(f"{r['variant']}\t{r['brand']}\t{r['name']}\t{r['value']}")
        QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"{len(rows)} rij(en) gekopieerd.", 4000)


def main():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ASWatson.AttributeViewer")
        except OSError:
            pass
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icon = _resource_dir() / "favicon" / "favicon.ico"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    logger.info("%s %s gestart", TITLE, APP_VERSION)
    w = ViewerWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

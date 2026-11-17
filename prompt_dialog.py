"""
Venster om de instructies aan het model zelf aan te passen:

- Uitleg per attribuut  — wat het model precies moet bekijken (bv. bij
  colorPackaging: dominante kleuren, geen tekst/logo's). Standaard uit
  ``config.VISUAL_HINTS``; een eigen versie gaat naar ``cfg.attribute_hints``.
- Attribuutprompt       — de volledige instructie voor de attribuutverrijking
  (plaatshouders {brand}, {product_name}, {attribute_block}).
- Classificatieprompt   — de instructie voor de beeldclassificatie.
- Voorbeeld             — de prompt zoals hij naar het model gaat.

Alleen wat AFWIJKT van de standaard wordt bewaard (settings.json), zodat een
verbeterde standaard in een nieuwe versie vanzelf geldt voor wie niets aanpaste.
"""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from config import (
    DEFAULT_ATTRIBUTE_PROMPT, DEFAULT_PROMPT, VISUAL_ATTRIBUTES, VISUAL_HINTS, ClassifierConfig,
)

NO_HINT = "-"     # eigen "lege" uitleg = standaarduitleg uitschakelen


def _mono(edit: QPlainTextEdit) -> QPlainTextEdit:
    edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
    return edit


class PromptDialog(QDialog):
    def __init__(self, cfg: ClassifierConfig, ref, selection: dict[str, list[str]] | None = None,
                 initial_attr: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Prompt en uitleg aanpassen")
        self.resize(980, 720)
        self.cfg = cfg
        self.ref = ref
        self.selection = selection or {}
        self.hints = dict(cfg.attribute_hints)       # werkkopie
        self._attr: str | None = None

        tabs = QTabWidget()
        self.tabs = tabs

        # --- uitleg per attribuut
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Deze uitleg komt in de prompt bij het attribuut, vóór de lijst met toegelaten waarden. "
            "Gebruik ze om te sturen <b>waar</b> het model naar kijkt. Schrijf in het Engels (zoals "
            "de rest van de prompt). Maak het veld leeg om geen uitleg mee te geven."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Attribuut:"))
        self.cb_attr = QComboBox()
        self.cb_attr.setMinimumWidth(320)
        for key in self._attribute_keys():
            name = ref.display_name(key) if ref is not None else key
            mark = "👁 " if key in VISUAL_ATTRIBUTES else ""
            own = " ✎" if self.hints.get(key, "").strip() else ""
            self.cb_attr.addItem(f"{mark}{name}{own}", key)
        self.cb_attr.currentIndexChanged.connect(self._on_attr_changed)
        row.addWidget(self.cb_attr)
        row.addStretch()
        lay.addLayout(row)
        self.ed_hint = _mono(QPlainTextEdit())
        lay.addWidget(self.ed_hint, 1)
        self.lbl_default = QLabel()
        self.lbl_default.setWordWrap(True)
        self.lbl_default.setStyleSheet("color:#666;")
        lay.addWidget(self.lbl_default)
        b = QPushButton("Standaarduitleg herstellen")
        b.clicked.connect(self._reset_hint)
        lay.addWidget(b)
        self.lbl_options = QLabel()
        self.lbl_options.setWordWrap(True)
        self.lbl_options.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_options)
        tabs.addTab(w, "Uitleg per attribuut")

        # --- attribuutprompt
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Plaatshouders: <b>{brand}</b>, <b>{product_name}</b> en <b>{attribute_block}</b> "
            "(verplicht: daar komen de attributen, hun uitleg en toegelaten waarden). Letterlijke "
            "accolades schrijf je dubbel: <code>{{</code> en <code>}}</code>. Het antwoord moet JSON "
            "blijven in het formaat onderaan de standaardprompt."))
        self.ed_attr_prompt = _mono(QPlainTextEdit(cfg.active_attribute_prompt()))
        lay.addWidget(self.ed_attr_prompt, 1)
        b = QPushButton("Standaardprompt herstellen")
        b.clicked.connect(lambda: self.ed_attr_prompt.setPlainText(DEFAULT_ATTRIBUTE_PROMPT))
        lay.addWidget(b)
        tabs.addTab(w, "Attribuutprompt")

        # --- classificatieprompt
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Instructie voor het classificeren van beelden (tab 1, stap 4). Het antwoord moet JSON "
            "blijven: <code>{\"label\": ..., \"confidence\": ...}</code> met een van de labels uit "
            "de prompt."))
        self.ed_cls_prompt = _mono(QPlainTextEdit(cfg.active_prompt()))
        lay.addWidget(self.ed_cls_prompt, 1)
        b = QPushButton("Standaardprompt herstellen")
        b.clicked.connect(lambda: self.ed_cls_prompt.setPlainText(DEFAULT_PROMPT))
        lay.addWidget(b)
        tabs.addTab(w, "Classificatieprompt")

        # --- voorbeeld
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_preview = QLabel()
        self.lbl_preview.setWordWrap(True)
        lay.addWidget(self.lbl_preview)
        self.ed_preview = _mono(QPlainTextEdit())
        self.ed_preview.setReadOnly(True)
        lay.addWidget(self.ed_preview, 1)
        tabs.addTab(w, "Voorbeeld")
        tabs.currentChanged.connect(self._on_tab)

        note = QLabel("Let op: reeds verwerkte producten veranderen niet. Vink in tab Attributen "
                      "<i>ook reeds verwerkte opnieuw</i> aan om ze met de nieuwe prompt te herdoen "
                      "(test eerst met een limiet).")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Opslaan")
        buttons.button(QDialogButtonBox.Cancel).setText("Annuleren")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(tabs, 1)
        root.addWidget(note)
        root.addWidget(buttons)

        start = self.cb_attr.findData(initial_attr) if initial_attr else -1
        self.cb_attr.setCurrentIndex(max(start, 0))
        self._on_attr_changed()

    # ------------------------------------------------------------ uitleg

    def _attribute_keys(self) -> list[str]:
        keys = set(VISUAL_HINTS) | set(self.hints)
        if self.ref is not None:
            keys |= {k for k, v in self.ref.options.items() if v}
        name = (lambda k: self.ref.display_name(k).lower()) if self.ref is not None else str
        return sorted(keys, key=lambda k: (k not in VISUAL_ATTRIBUTES, name(k)))

    def _store_current_hint(self):
        """Tekst in het veld → werkkopie (alleen bewaren wat afwijkt)."""
        if self._attr is None:
            return
        text = self.ed_hint.toPlainText().strip()
        default = VISUAL_HINTS.get(self._attr, "")
        if text == default:
            self.hints.pop(self._attr, None)
        elif not text:
            if default:
                self.hints[self._attr] = NO_HINT
            else:
                self.hints.pop(self._attr, None)
        else:
            self.hints[self._attr] = text
        i = self.cb_attr.findData(self._attr)
        if i >= 0:
            label = self.cb_attr.itemText(i).removesuffix(" ✎")
            self.cb_attr.setItemText(i, label + (" ✎" if self._attr in self.hints else ""))

    def _on_attr_changed(self, *_):
        self._store_current_hint()
        self._attr = self.cb_attr.currentData()
        if self._attr is None:
            return
        own = self.hints.get(self._attr)
        default = VISUAL_HINTS.get(self._attr, "")
        self.ed_hint.setPlainText("" if own == NO_HINT else (own or default))
        self.lbl_default.setText(f"Standaard: {default}" if default else
                                 "Standaard: geen uitleg (alleen de naam en de toegelaten waarden).")
        if self.ref is not None:
            opts = self.ref.options_for(self._attr)
            rule = "meerdere waarden (max 3)" if self.ref.is_multi(self._attr) else "precies één waarde"
            shown = "; ".join(opts[:40]) + (" …" if len(opts) > 40 else "")
            self.lbl_options.setText(f"{len(opts)} toegelaten waarden, {rule}: {shown}")

    def _reset_hint(self):
        if self._attr is not None:
            self.ed_hint.setPlainText(VISUAL_HINTS.get(self._attr, ""))

    # ------------------------------------------------------------ voorbeeld

    def _pending_cfg(self) -> ClassifierConfig:
        self._store_current_hint()
        attr_prompt = self.ed_attr_prompt.toPlainText()
        cls_prompt = self.ed_cls_prompt.toPlainText()
        return replace(self.cfg,
                       attribute_hints=dict(self.hints),
                       custom_attribute_prompt="" if attr_prompt.strip() == DEFAULT_ATTRIBUTE_PROMPT.strip()
                       else attr_prompt,
                       custom_prompt="" if cls_prompt.strip() == DEFAULT_PROMPT.strip() else cls_prompt)

    def _on_tab(self, i: int):
        if self.tabs.tabText(i) != "Voorbeeld":
            return
        from attr_enricher import build_prompt, check_attribute_prompt
        cfg = self._pending_cfg()
        err = check_attribute_prompt(cfg.active_attribute_prompt())
        if err:
            self.lbl_preview.setText(f"<span style='color:#c0392b'>{err}</span>")
            self.ed_preview.setPlainText("")
            return
        if self.ref is None:
            self.lbl_preview.setText("Referentiebestanden niet geladen; lees eerst het productbestand in.")
            return
        # de aangevinkte attributen van de eerste gekozen categorie, anders het huidige attribuut
        category, attrs = next(iter(self.selection.items()), (None, []))
        if not attrs:
            attrs = [self._attr] if self._attr else []
        self.lbl_preview.setText(
            "Zo gaat de prompt naar het model (samen met het packshot) voor "
            + (f"de aangevinkte attributen van <b>{category}</b>." if category else "dit attribuut.")
            + " Merk en productnaam zijn voorbeelden.")
        self.ed_preview.setPlainText(build_prompt(cfg, self.ref, attrs, "<merk>", "<productnaam>"))

    # ------------------------------------------------------------ opslaan

    def _save(self):
        from attr_enricher import check_attribute_prompt
        cfg = self._pending_cfg()
        err = check_attribute_prompt(cfg.active_attribute_prompt())
        if err:
            self.tabs.setCurrentIndex(1)
            QMessageBox.warning(self, "Attribuutprompt", err)
            return
        if not cfg.active_prompt().strip():
            QMessageBox.warning(self, "Classificatieprompt", "De classificatieprompt is leeg.")
            return
        self.cfg.attribute_hints = cfg.attribute_hints
        self.cfg.custom_attribute_prompt = cfg.custom_attribute_prompt
        self.cfg.custom_prompt = cfg.custom_prompt
        self.cfg.save_user_settings()
        self.accept()

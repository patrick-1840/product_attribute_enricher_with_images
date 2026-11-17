"""
Centrale configuratie voor de Product Image Classifier.

Alle instelbare parameters staan hier in één dataclass, zodat UI en CLI
dezelfde configuratie delen. Per-gebruiker instellingen (paden, model,
sneltoetsen, …) worden bewaard in %APPDATA%/ProductImageClassifier/settings.json.

De OpenAI-key komt UITSLUITEND uit een omgevingsvariabele of een .env-bestand
(naast het programma of in de gebruikersmap) — nooit uit de code en nooit uit
settings.json.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv, set_key

logger = logging.getLogger(__name__)

APP_NAME = "ProductImageClassifier"


def _app_dir() -> Path:
    """Map waar het programma staat. Bij een PyInstaller-.exe wijst ``__file__``
    naar de tijdelijke uitpakmap; daarom ``sys.executable`` in gecompileerde
    modus. **Niet terugdraaien** (zie CLAUDE.md)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.absolute()
    return Path(__file__).parent.absolute()


BASE_DIR = _app_dir()


def _user_config_dir() -> Path:
    """Per-gebruiker map: %APPDATA%/ProductImageClassifier op Windows."""
    if os.name == "nt":
        base = os.getenv("APPDATA") or str(Path.home())
    else:
        base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    directory = Path(base) / APP_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _default_cache_dir() -> Path:
    """Standaard cachemap (thumbnails + resultatendatabase). Bewust in
    %LOCALAPPDATA% en niet in OneDrive: ~75.000 kleine bestanden synchroniseren
    is traag en onnodig."""
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home())
    else:
        base = os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_NAME / "cache"


USER_DIR = _user_config_dir()
USER_SETTINGS_FILE = USER_DIR / "settings.json"
USER_ENV_FILE = USER_DIR / ".env"
LOG_FILE = USER_DIR / "logs" / "app.log"

# Volgorde: echte omgevingsvariabele > .env naast het programma > .env in %APPDATA%.
# load_dotenv overschrijft standaard niets dat al gezet is.
load_dotenv(BASE_DIR / ".env")
load_dotenv(USER_ENV_FILE)


def setup_logging(level: int = logging.INFO) -> None:
    """Logt naar console en een roterend logbestand. Eén keer aanroepen per
    entrypoint (desktop_app.py, cli.py)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3,
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # httpx/openai loggen elke request op INFO; dat is bij 75k calls ruis.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("Logging gestart; logbestand: %s", LOG_FILE)


def save_api_key(key: str) -> None:
    """Bewaart de OpenAI-key in %APPDATA%/ProductImageClassifier/.env (nooit in
    settings.json of in de repo) en zet hem meteen actief voor deze sessie."""
    key = key.strip()
    USER_ENV_FILE.touch(exist_ok=True)
    set_key(str(USER_ENV_FILE), "OPENAI_API_KEY", key, quote_mode="never")
    os.environ["OPENAI_API_KEY"] = key
    logger.info("API-key bewaard in %s", USER_ENV_FILE)


# ------------------------------------------------------------------ labels

LABELS = ["packshot", "sfeerbeeld", "swatch", "tekst", "overig"]
USE_LABEL = "packshot"   # label dat standaard "gebruik voor attributie" aanvinkt

DEFAULT_PROMPT = """Classify this e-commerce product image from a beauty/perfume webshop.

Labels:
- packshot: the product (bottle, jar, tube, box, palette...) clearly shown on a plain/neutral background, front, side or back view. Product + its box together also counts.
- sfeerbeeld: lifestyle/mood image: model, hands, decor, scenery, props, or product in a styled setting.
- swatch: colour/texture sample (smear, drop, swatch on skin, texture close-up) or colour chip.
- tekst: image dominated by text, icons, infographic, ingredient list, claims or instructions.
- overig: anything else (logo, empty/broken image, multiple different products, gift set overview...).

Answer ONLY with JSON: {"label": "<one of: packshot|sfeerbeeld|swatch|tekst|overig>", "confidence": <0.0-1.0>}"""

# ------------------------------------------------------------------ sneltoetsen

#: actie -> (omschrijving, standaardtoets). Volgorde = volgorde in het settings-scherm.
DEFAULT_SHORTCUTS: dict[str, tuple[str, str]] = {
    "accept": ("Voorstel goedkeuren (accept)", "A"),
    "deny": ("Voorstel afkeuren (deny)", "D"),
    "toggle_use": ("Vinkje 'gebruik' omschakelen", "Space"),
    "next_h": ("Volgende afbeelding (horizontale lay-out)", "Right"),
    "prev_h": ("Vorige afbeelding (horizontale lay-out)", "Left"),
    "next_v": ("Volgende afbeelding (verticale lay-out)", "Down"),
    "prev_v": ("Vorige afbeelding (verticale lay-out)", "Up"),
    "next_product": ("Volgend product", "PgDown"),
    "prev_product": ("Vorig product", "PgUp"),
    "undo": ("Review van huidige afbeelding wissen", "Backspace"),
    **{f"label_{i + 1}": (f"Label zetten op '{lab}'", str(i + 1)) for i, lab in enumerate(LABELS)},
}


@dataclass
class ClassifierConfig:
    """Alle instellingen op één plaats. De UI toont en bewerkt dit object."""

    # --- Bestanden ---
    product_file: Path = (
        Path.home() / "AS Watson Europe" / "Ecommerce - Documenten" / "Byteblast"
        / "Data Governance Hygiene MDC" / "Product inventory"
        / "wekelijks productoverzicht met alle attributen.xlsx"
    )
    cache_dir: Path = field(default_factory=_default_cache_dir)
    last_export_dir: Path = Path.home()

    # --- Inlezen ---
    id_column: str = "variant_code"
    # Kolommen "product image N" én "imageLinkN" worden automatisch herkend.
    # Extra (ongenummerde) beeldkolommen die ook meegenomen worden:
    extra_image_columns: list[str] = field(default_factory=lambda: ["mainImageLink"])
    # Kolommen die in de review als context bij het product getoond worden.
    info_columns: list[str] = field(default_factory=lambda: ["brand", "productName", "mainCategory"])

    # --- Downloaden ---
    concurrent_downloads: int = 10
    download_timeout: int = 30
    retry_limit: int = 3
    retry_backoff: float = 1.7
    thumb_max_px: int = 512
    thumb_quality: int = 85

    # --- OpenAI ---
    use_openai: bool = True                 # False = simulatiemodus (geen kosten)
    openai_model: str = "gpt-4o-mini"
    concurrent_requests: int = 10
    openai_timeout: int = 60
    openai_max_tokens: int = 60
    escalate: bool = False                  # herclassificeren onder de drempel
    escalation_model: str = "gpt-4o"
    confidence_threshold: float = 0.7       # ook de standaard voor de review-filter
    prompt: str = DEFAULT_PROMPT

    # --- Review ---
    layout_orientation: str = "horizontal"  # "horizontal" | "vertical"
    shortcuts: dict[str, str] = field(
        default_factory=lambda: {k: v[1] for k, v in DEFAULT_SHORTCUTS.items()})

    @property
    def openai_api_key(self) -> str | None:
        return os.getenv("OPENAI_API_KEY")

    @property
    def thumbs_dir(self) -> Path:
        return Path(self.cache_dir) / "thumbs"

    @property
    def db_path(self) -> Path:
        return Path(self.cache_dir) / "state.sqlite"

    @property
    def failed_log(self) -> Path:
        return Path(self.cache_dir) / "mislukte_urls.csv"

    def shortcut(self, action: str) -> str:
        return self.shortcuts.get(action) or DEFAULT_SHORTCUTS[action][1]

    # -------------------------------------------------------- user settings

    _PATH_FIELDS = ("product_file", "cache_dir", "last_export_dir")
    # Simulatiemodus nooit onthouden; de prompt leeft in de code zodat een
    # verbeterde standaardprompt niet door een oude settings.json overschaduwd wordt.
    _SKIP_FIELDS = ("use_openai", "prompt")

    def load_user_settings(self, path: Path = USER_SETTINGS_FILE) -> None:
        """Overschrijft velden met wat in settings.json staat (indien aanwezig)."""
        if not Path(path).exists():
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Kon %s niet lezen (%s); standaardinstellingen gebruikt.", path, exc)
            return
        known = {f.name for f in fields(self)}
        for key, value in data.items():
            if key not in known or key in self._SKIP_FIELDS:
                continue
            if key in self._PATH_FIELDS:
                value = Path(value).expanduser()
            if key == "shortcuts" and isinstance(value, dict):
                merged = {k: v[1] for k, v in DEFAULT_SHORTCUTS.items()}
                merged.update({k: v for k, v in value.items() if k in merged})
                value = merged
            setattr(self, key, value)

    def save_user_settings(self, path: Path = USER_SETTINGS_FILE) -> None:
        data = {}
        for f in fields(self):
            if f.name in self._SKIP_FIELDS:
                continue
            value = getattr(self, f.name)
            data[f.name] = str(value) if isinstance(value, Path) else value
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Instellingen bewaard in %s", path)

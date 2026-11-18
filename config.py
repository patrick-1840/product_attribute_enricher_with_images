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
APP_TITLE = "Product Image Classifier"
# Versienummer (splash, venstertitel, log, zipnaam). Verhoog bij elke release
# naar main en tag dezelfde versie in git (vX.Y.Z).
APP_VERSION = "0.3.0"


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

# Drie soorten packshots: alleen het product, alleen de verpakking (doos), of
# beide samen. Dat onderscheid bepaalt mee welk beeld geschikt is voor welk
# attribuut (bv. flesvorm vs. kleur van de verpakking).
PACKSHOT_LABELS = ["packshot_product", "packshot_verpakking", "packshot_beide"]
LABELS = [*PACKSHOT_LABELS, "sfeerbeeld", "swatch", "tekst", "overig"]
# Labels die standaard "gebruik voor attributie" aanvinken. "packshot" staat er
# voor oude resultaten (vóór de opsplitsing) nog bij.
USE_LABELS = {*PACKSHOT_LABELS, "packshot"}


def is_use_label(label) -> bool:
    return isinstance(label, str) and label in USE_LABELS

DEFAULT_PROMPT = """Classify this e-commerce product image from a beauty/perfume webshop.

Labels:
- packshot_product: ONLY the product itself (bottle, jar, tube, compact, palette...) on a plain/neutral background, front, side or back view. No outer box.
- packshot_verpakking: ONLY the outer packaging/box (or gift box) on a plain background, without the product itself visible next to it.
- packshot_beide: the product AND its outer packaging/box together on a plain background.
- sfeerbeeld: lifestyle/mood image: model, hands, decor, scenery, props, or product in a styled setting.
- swatch: colour/texture sample (smear, drop, swatch on skin, texture close-up) or colour chip.
- tekst: image dominated by text, icons, infographic, ingredient list, claims or instructions.
- overig: anything else (logo, empty/broken image, multiple different products, gift set overview...).

Answer ONLY with JSON: {"label": "<one of: packshot_product|packshot_verpakking|packshot_beide|sfeerbeeld|swatch|tekst|overig>", "confidence": <0.0-1.0>}"""

# ------------------------------------------------------------------ attributen

# mainCategory in de productlijst -> kolommen in de categorie/attribuutmatrix van
# de SAP-mapping. Gekopieerd uit de Product Attribute Enricher (houd in sync).
# Kolom met de productcategorie (categoriekeuze en attribuutmatrix).
CATEGORY_COLUMN = "mainCategory"

DEFAULT_CATEGORY_MAPPING: dict[str, list[str]] = {
    "PERFUME": ["Parfum"],
    "HAIR CARE": ["Haar"],
    "SKINCARE": ["Lichaam", "Gezicht"],
    "MAKE-UP": ["Make-up"],
    "SUNSCREEN": ["Lichaam", "Gezicht"],
    "BATH & BODY": ["Body"],
    "HOME ACCESSORIES": ["Home"],
    "HOME": ["Home"],
}

# Attributen die je op een packshot kan ZIEN. Deze staan in de attribuutboom
# standaard aangevinkt; andere attributen kunnen wel, maar staan uit.
VISUAL_ATTRIBUTES = {
    "colorpackaging", "shapeofthebottle", "typedispenser", "productformat",
}

# Extra uitleg per attribuut voor het visiemodel (lowercase sleutel).
VISUAL_HINTS: dict[str, str] = {
    "colorpackaging": "the 1-2 DOMINANT colours of the packaging surfaces themselves (bottle, "
                      "jar, tube, cap, box): the colours that cover most of the visible packaging. "
                      "Ignore printed text, logos, brand names, labels and small decorative details, "
                      "even when they are bold or large. For a transparent bottle, judge the "
                      "glass/plastic and cap, and use 'clear' when the container is transparent.",
    "shapeofthebottle": "the overall shape of the bottle/container.",
    "typedispenser": "how the product is dispensed: spray nozzle, pump/flacon, jar, stick or tube.",
    "productformat": "the physical form of the product, only if visible (e.g. compact, stick, "
                     "liquid in a dropper bottle).",
}

# Attributen waarvoor meerdere waarden mogen (aangevuld met 'multi' uit het
# attribuuttypes-bestand), zoals in de attribuutverrijker.
MULTI_VALUE_HINT_ATTRIBUTES = {"colorpackaging"}

DEFAULT_ATTRIBUTE_PROMPT = """You are a precise product data assistant for a beauty and perfume webshop.
Look at the product image (a packshot) and determine the attributes below.

Product: {brand} - {product_name}

Attributes:
{attribute_block}

Rules:
- Base your answer ONLY on what is visible in the image.
- Only choose from the listed options, spelled exactly as given.
- For attributes marked "multiple allowed", return up to 3 options as a comma-separated string.
- If an attribute cannot be determined from the image, set "choice" to null.
- Give a confidence between 0 and 1 per attribute: how sure you are that the choice is
  correct based on the image (1.0 = clearly visible, 0.5 = guess between options).
- Give a brief motivation (max 1 sentence) per attribute.

Answer ONLY with JSON, one key per attribute name exactly as listed:
{{"<attribute>": {{"choice": "<option>" or null, "confidence": 0.0-1.0, "motivation": "..."}}, ...}}"""


def _enricher_setting(key: str) -> Path | None:
    """Neemt een bronbestandpad over uit de instellingen van de Product Attribute
    Enricher (%APPDATA%/AttributeEnricher/settings.json), als die bestaat."""
    path = USER_DIR.parent / "AttributeEnricher" / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(key)
    except (OSError, json.JSONDecodeError):
        return None
    return Path(value) if value else None


def _default_ref_file(key: str, name: str) -> Path:
    return _enricher_setting(key) or BASE_DIR / "sources" / name


# ------------------------------------------------------------------ sneltoetsen

#: actie -> (omschrijving, standaardtoets). Volgorde = volgorde in het settings-scherm.
# (Actienamen gewijzigd t.o.v. v0.1 zodat oude toetsen uit settings.json niet
# blijven hangen; PgUp/PgDown/Home/End blijven vrij om snel te scrollen.)
DEFAULT_SHORTCUTS: dict[str, tuple[str, str]] = {
    "prod_next": ("Volgend product", "Down"),
    "prod_prev": ("Vorig product", "Up"),
    "img_next": ("Volgende afbeelding", "Right"),
    "img_prev": ("Vorige afbeelding", "Left"),
    "accept_product": ("Product goedkeuren (alle voorstellen) en door", "Return"),
    "set_packshot": ("Huidige afbeelding als packshot kiezen", "P"),
    "accept": ("Voorstel van de afbeelding goedkeuren (accept)", "A"),
    "deny": ("Voorstel van de afbeelding afkeuren (deny)", "D"),
    "toggle_use": ("Vinkje 'gebruik' omschakelen", "Space"),
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
    # Eigen prompts/uitleg (leeg = standaard uit de code). Bewaard in
    # settings.json; een verbeterde standaard geldt zo vanzelf voor wie niets aanpaste.
    custom_prompt: str = ""                 # classificatieprompt

    # --- Packshot-keuze voor de attribuutverrijking ---
    # "first"  = het eerste beeld per product is het packshot (geen classificatie nodig)
    # "review" = het eerste beeld met vinkje "gebruik" (voorstel of review)
    packshot_mode: str = "first"
    only_fetch_packshots: bool = True       # bij "first": alleen dat ene beeld downloaden

    # --- Attribuutverrijking ---
    attribute_types_file: Path = field(default_factory=lambda: _default_ref_file(
        "attribute_types_file", "attributen - single or double.xlsx"))
    sap_mapping_file: Path = field(default_factory=lambda: _default_ref_file(
        "sap_mapping_file", "sap_commerce_product_attributes.xlsx"))
    attribute_model: str = "gpt-4o"
    attribute_detail: str = "low"           # "low" (goedkoop) of "high" (fijnere details)
    attribute_max_tokens: int = 800
    only_empty_values: bool = True
    impex_catalog: str = "Staged"
    custom_attribute_prompt: str = ""
    attribute_hints: dict[str, str] = field(default_factory=dict)   # attribuut (lowercase) -> uitleg

    # --- Review ---
    review_thumb_px: int = 140              # thumbnailgrootte in de reviewlijst
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

    def active_prompt(self) -> str:
        """Classificatieprompt: eigen versie of de standaard."""
        return self.custom_prompt.strip() or DEFAULT_PROMPT

    def active_attribute_prompt(self) -> str:
        return self.custom_attribute_prompt.strip() or DEFAULT_ATTRIBUTE_PROMPT

    def hint_for(self, attr: str) -> str:
        """Uitleg voor het model bij dit attribuut: eigen versie of de standaard.
        Een eigen LEGE uitleg ("-") schakelt de standaarduitleg uit."""
        own = self.attribute_hints.get(attr.lower())
        if own is not None and own.strip():
            return "" if own.strip() == "-" else own.strip()
        return VISUAL_HINTS.get(attr.lower(), "")

    def shortcut(self, action: str) -> str:
        return self.shortcuts.get(action) or DEFAULT_SHORTCUTS[action][1]

    # -------------------------------------------------------- user settings

    _PATH_FIELDS = ("product_file", "cache_dir", "last_export_dir",
                    "attribute_types_file", "sap_mapping_file")
    # Simulatiemodus nooit onthouden. (De standaardprompts leven in de code;
    # alleen een EIGEN aanpassing gaat naar settings.json, zie custom_*.)
    _SKIP_FIELDS = ("use_openai",)

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

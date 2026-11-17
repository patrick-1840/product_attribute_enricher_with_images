# Product Image Classifier

Selecteert per product de bruikbare productafbeeldingen (packshots) uit de
wekelijkse productlijst van ICI PARIS XL, als voorbereiding op visuele
attribuutverrijking. Bouwt verder op de **Product Attribute Enricher**
(`../product_attribute_enricher`), maar is (voorlopig) een apart programma met
dezelfde conventies. Desktop-app (PySide6) + CLI. Nederlandstalige codebase en UI.

Draait lokaal op Windows. Collega's krijgen alleen de **gecompileerde build**
(PyInstaller-map als .zip). Houd het robuust voor niet-technische gebruikers:
geen absolute paden, niet schrijven naast de .exe.

- Per-gebruiker instellingen: `%APPDATA%/ProductImageClassifier/settings.json`
  (beheerd door `config.py`; ook de sneltoetsen).
- OpenAI-key: `%APPDATA%/ProductImageClassifier/.env` (via het settings-scherm)
  of een `.env` naast het programma. **Nooit** in settings.json of in code.
- Cache (thumbnails + `state.sqlite`): standaard
  `%LOCALAPPDATA%/ProductImageClassifier/cache` — bewust niet in OneDrive.
- Logbestand: `%APPDATA%/ProductImageClassifier/logs/app.log`.

## Commando's

```bash
pip install -r requirements.txt
python desktop_app.py                                   # desktop-UI
python cli.py load --file "pad/naar/productlijst.xlsx"  # werklijst opbouwen
python cli.py fetch --limit 50                          # downloaden + thumbnails
python cli.py classify --limit 50 --simulate            # testrun zonder API-kosten
python cli.py stats
python cli.py export --out "pad/naar/met_labels.xlsx"
```
Alle CLI-commando's accepteren `--cache-dir <map>` om in een testcache te werken.

## Architectuur

Afhankelijkheidsrichting: `config` ← `store` ← `loader`/`fetcher`/`classifier`/`exporter` ← UI/CLI.

- `config.py` — `ClassifierConfig` dataclass, labels, standaardprompt,
  standaardsneltoetsen (`DEFAULT_SHORTCUTS`). **Nieuwe knoppen komen hier.**
- `store.py` — SQLite-toestand: `images` (per unieke URL: download + label),
  `items` (product × slot → URL), `products` (contextinfo), `reviews`.
- `loader.py` — leest Excel/CSV, detecteert beeldkolommen, bouwt de platte tabel.
- `fetcher.py` — async download (aiohttp), thumbnails max 512 px (Pillow).
- `classifier.py` — `AsyncOpenAI`, `detail: low`, JSON-antwoord, escalatie.
- `runcontrol.py` — pauzeren/hervatten/stoppen, gedeeld door fetcher en classifier.
- `exporter.py` — Excel terugschrijven (openpyxl, behoudt opmaak) + correctielog.
- `review_widget.py` — reviewscherm met kaarten en sneltoetsen.
- `desktop_app.py` — hoofdvenster, 3 tabs; taken draaien in een `QThread`.
- `cli.py` — scriptbare variant van dezelfde flow.

## Datamodel (belangrijk)

- Beeldkolommen: `product image N` **en** `imageLinkN` worden herkend (regex
  `IMAGE_COL_RE` in `loader.py`), plus `extra_image_columns` (standaard
  `mainImageLink`). `imageUrl` in de huidige export is een kapotte URL
  (`/medias/?context=…`) en wordt bewust NIET meegenomen.
- Product-id: `variant_code` (instelbaar). Slot = `"1".."N"` of de kolomnaam.
- Exportkolommen: `image N label` / `image N use`, en `mainImageLink label` / `… use`.
- Labels: `packshot | sfeerbeeld | swatch | tekst | overig` (`config.LABELS`).
  Bij review kan daar `onbekend` bijkomen (deny zonder nieuw label).
- Effectief label/vinkje: review wint; anders voorstel (vinkje aan ⇔ packshot);
  geen voorstel en geen review → lege cellen. Logica in `exporter.effective_*`
  en gespiegeld in `ReviewWidget._save` — houd die twee in sync.
- Reviews gelden alleen zolang de URL in dat slot dezelfde is (join op URL).

## Conventies

- **Eén download en één call per unieke URL.** Dubbele URL's tussen producten
  delen hun resultaat. Niets opnieuw doen wat al in `state.sqlite` staat.
- Elk resultaat (download, classificatie, review) gaat METEEN naar de database.
  Nooit batchen in geheugen: crash-bestendigheid is een harde eis (~75k beelden).
- Een label dat niet in `LABELS` staat (na synoniemen) is een fout, wordt
  gelogd en later opnieuw geprobeerd; nooit stilzwijgend wegschrijven.
- Reasoning-modellen (`gpt-5*`, `o*`): `max_completion_tokens`, GEEN `temperature`.
- Gebruik `openai.RateLimitError`; `insufficient_quota` stopt de hele run.
- Retry-teller hoogt ALTIJD op, ook bij timeouts (anders oneindige lus).
- Concurrency 10 voor downloads en API-calls (instelbaar). De downloader jittert
  0,05–0,3 s per request; die randomisatie niet weghalen.
- Simulatiemodus schrijft labels met model `simulatie`; de eerstvolgende echte
  run wist die automatisch. Test altijd eerst met `--simulate` + `--limit`.
- PySide6 importeren vóór pandas in `desktop_app.py` (DLL-conflict op Windows).
- Het originele Excel wordt nooit overschreven; export via "Exporteer als…".
- Nederlands in commentaar, docstrings, UI-teksten en logs.

## Git-workflow (VERPLICHT)

Werk voor **elke** taak op een feature branch. Begin geen codewijziging op `main` of `dev`.

1. Start: `git switch dev && git switch -c feat/<korte-naam>` (`fix/`, `chore/` idem).
2. Kleine, gefocuste commits; imperatieve Nederlandstalige boodschap. Claude mag zelf committen.
3. Na verificatie: `git switch dev && git merge --no-ff feat/<naam>`, branch verwijderen.
4. `dev` → `main` uitsluitend voor releases (via merge).
5. **Commit nooit rechtstreeks op `main` of `dev`** (de hook in `.claude/` blokkeert dit).

Remote: https://github.com/patrick-1840/product_attribute_enricher_with_images —
pushen doet de gebruiker zelf.

## Distributie

PyInstaller-**map** (geen losse .exe), als .zip: `build.ps1` of
`pyinstaller build.spec --noconfirm`. `BASE_DIR` in `config.py` gebruikt
`sys.executable` zodra `sys.frozen` waar is — **niet terugdraaien**.
`build/` en `dist/` worden nooit gecommit.

## Handmatig verifiëren

Geen automatische testsuite. Na wijzigingen minstens, in een testcache:
```bash
python cli.py --cache-dir C:/temp/pic load --file <productlijst>
python cli.py --cache-dir C:/temp/pic fetch --limit 30
python cli.py --cache-dir C:/temp/pic classify --limit 30 --simulate
python cli.py --cache-dir C:/temp/pic export --file <productlijst> --out C:/temp/pic/out.xlsx
```
en open de desktop-app om het reviewscherm met de sneltoetsen te testen.

@README.md bevat de gebruikersworkflow.

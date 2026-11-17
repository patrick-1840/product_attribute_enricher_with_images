# Product Image Classifier

Bepaalt productattributen (bv. `colorPackaging`, `shapeOfTheBottle`,
`typeDispenser`) op basis van het **packshot** van elk product in de wekelijkse
productlijst van ICI PARIS XL, met dezelfde optielijsten, SAP-codes en
impex-export als de attribuutverrijker. Kan ook alle productbeelden
classificeren (packshot product / verpakking / beide, sfeerbeeld, ...). Bouwt verder op de **Product Attribute Enricher**
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
python cli.py categories --set PERFUME                  # alleen deze categorie(ën) verwerken
python cli.py fetch --limit 50                          # downloaden + thumbnails
python cli.py classify --limit 50 --simulate            # testrun zonder API-kosten
python cli.py stats
python cli.py export --out "pad/naar/met_labels.xlsx"
python cli.py enrich --category PERFUME --attributes colorPackaging,shapeOfTheBottle --limit 20 --simulate
python cli.py export-attrs --category PERFUME --attributes colorPackaging --out res.xlsx --impex res.impex
```
Alle CLI-commando's accepteren `--cache-dir <map>` om in een testcache te werken.

## Architectuur

Afhankelijkheidsrichting: `config` ← `store` ← `loader`/`fetcher`/`classifier`/`exporter`
← `reference` ← `attr_enricher` ← UI/CLI.

- `config.py` — `ClassifierConfig` dataclass, labels, standaardprompt,
  standaardsneltoetsen (`DEFAULT_SHORTCUTS`). **Nieuwe knoppen komen hier.**
  Een actie hernoemen = oude waarden in settings.json worden genegeerd (bewust
  gebruikt bij de overgang naar de lijstreview).
- `store.py` — SQLite-toestand: `images` (per unieke URL: download + label),
  `items` (product × slot → URL), `products` (contextinfo), `reviews`.
- `loader.py` — leest Excel/CSV, detecteert beeldkolommen, bouwt de platte tabel.
- `fetcher.py` — async download (aiohttp), thumbnails max 512 px (Pillow).
- `classifier.py` — `AsyncOpenAI`, `detail: low`, JSON-antwoord, escalatie.
- `runcontrol.py` — pauzeren/hervatten/stoppen, gedeeld door fetcher en classifier.
- `exporter.py` — Excel terugschrijven (openpyxl, behoudt opmaak) + correctielog.
- `reference.py` — **kopie** van de referentielogica van de attribuutverrijker:
  attribuuttypes, optielijsten, label→SAP-code, categoriematrix, werklijst
  (incl. ontdubbeling single/boolean per baseProduct), `validate_choice`, impex.
  Wijzig alleen samen met het origineel; bij integratie weer samenvoegen.
- `attr_enricher.py` — packshotkeuze, één visie-call per product met alle
  gekozen attributen, validatie, `results_table`, Excel/impex-export.
- `review_widget.py` — packshot-review als gevirtualiseerde lijst (QListView +
  delegate die zelf tekent, thumbnails via QPixmapCache): één rij per product.
  Toetsen worden in `ProductView.keyPressEvent` afgehandeld (geen QShortcut,
  anders slokken filtervelden/tabbalk ze op); PgUp/PgDown/Home/End blijven native.
- `attribute_tab.py` — tabs "Attributen" (boom + toegelaten waarden + run) en
  "Resultaten" (tabel + packshotvoorbeeld + export).
- `qt_common.py` — `TaskWorker` (QThread) en `StepBox` (start/pauze/stop).
- `desktop_app.py` — hoofdvenster, 5 tabs; één achtergrondtaak tegelijk.
- `cli.py` — scriptbare variant van dezelfde flow.

## Datamodel (belangrijk)

- Beeldkolommen: `product image N` **en** `imageLinkN` worden herkend (regex
  `IMAGE_COL_RE` in `loader.py`), plus `extra_image_columns` (standaard
  `mainImageLink`). `imageUrl` in de huidige export is een kapotte URL
  (`/medias/?context=…`) en wordt bewust NIET meegenomen.
- Product-id: `variant_code` (instelbaar). Slot = `"1".."N"` of de kolomnaam.
- Exportkolommen: `image N label` / `image N use`, en `mainImageLink label` / `… use`.
- Labels: `packshot_product | packshot_verpakking | packshot_beide | sfeerbeeld |
  swatch | tekst | overig` (`config.LABELS`). Alle drie packshot-labels vinken
  standaard "gebruik" aan (`config.is_use_label`; oud label `packshot` telt mee).
  Bij review kan `onbekend` bijkomen (deny zonder nieuw label).
- Packshot voor attributen (`packshot_mode`): `first` = eerste beeld per product
  (slotvolgorde: `mainImageLink`, dan `imageLink1`...; dat is de front-zoom), of
  `review` = eerste beeld met effectief vinkje "gebruik". In modus `first`
  downloadt tab 1 standaard alleen die beelden.
- Referentiebestanden: dezelfde als de attribuutverrijker (`attribute_types_file`,
  `sap_mapping_file`); standaardpaden worden overgenomen uit
  `%APPDATA%/AttributeEnricher/settings.json`. In de boom zijn alleen attributen
  selecteerbaar die volgens de SAP-matrix bij de categorie horen en opties hebben;
  `VISUAL_ATTRIBUTES` staan standaard aan.
- `attr_results` is per (variant, attribuut) en geldt alleen voor de packshot-URL
  waarop het bepaald werd. Manuele aanpassing (`edited_value`) en exportvinkje
  (`approved`) worden meteen bewaard; een nieuwe modelrun wist ze.
- Effectief label/vinkje: review wint; anders voorstel (vinkje aan ⇔ packshot);
  geen voorstel en geen review → lege cellen. Logica in `exporter.effective_*`
  en gespiegeld in `ReviewWidget._save` — houd die twee in sync. Het packshot
  in de review (`ReviewWidget.packshot_of`) volgt dezelfde regel als
  `attr_enricher.select_packshots`.
- Reviews gelden alleen zolang de URL in dat slot dezelfde is (join op URL).
- Categoriekeuze (`scope`-tabel, `products.category` = `mainCategory` in
  hoofdletters): `urls_to_fetch`, `urls_to_classify`, `item_table()`,
  `first_images()` en `stats()` filteren via `Store._IN_SCOPE`. Geen
  `meta.scope_active` = alles (CLI-standaard); de desktop-app zet bij de eerste
  keer inlezen een lege keuze, zodat de gebruiker eerst categorieën kiest.
  De Excel-export en de packshotkeuze voor attributen gebruiken `scoped=False`
  (de werklijst beperkt zelf al tot categorieën).

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
- Eén call per product voor alle attributen samen (het beeld is de dure input).
  Een keuze die niet letterlijk in de optielijst staat, wordt bewaard met
  `valid=0`, rood getoond en krijgt GEEN SAP-code/impexregel.
- Impex-regelformaat identiek aan de verrijker:
  `;{variant_code};$productCatalog:{catalog}; $lang:,{codes}`.
- Simulatiemodus schrijft labels/attributen met model `simulatie`; de eerstvolgende
  echte run wist die automatisch. Test altijd eerst met `--simulate` + `--limit`.
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
en open de desktop-app om de reviewlijst met de sneltoetsen te testen
(ook nadat je in een filterveld geklikt hebt).

@README.md bevat de gebruikersworkflow.

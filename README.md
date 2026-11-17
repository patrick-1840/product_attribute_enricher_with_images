# Product Image Classifier

Selecteert per product de bruikbare productafbeeldingen (**packshots**) uit de
productlijst van ICI PARIS XL, als voorbereiding op visuele attribuutverrijking.
Elke afbeelding krijgt automatisch een label (packshot, sfeerbeeld, swatch,
tekst, overig) met een confidence. Daarna beoordeel je ze snel met het
toetsenbord. Het resultaat is per afbeelding een label en een vinkje
"gebruik voor attributie".

Dit programma bouwt verder op de Product Attribute Enricher en gebruikt
hetzelfde Excel-bestand als invoer.

## Workflow

**laden → downloaden → classificeren → reviewen → exporteren**

1. **Laden** (tab 1): kies het productbestand en klik *Inlezen*. Het programma
   zoekt zelf de beeldkolommen (`product image N` of `imageLinkN`, plus
   `mainImageLink`) en bouwt een werklijst: één regel per product × afbeelding.
   Lege cellen en ongeldige URL's worden overgeslagen.
2. **Downloaden**: *Start downloaden* haalt elke unieke afbeelding één keer
   op en bewaart een kleine versie (max. 512 px) in de cachemap. Je kunt
   pauzeren, hervatten en stoppen. Wat al binnen is, wordt de volgende keer
   overgeslagen. Mislukte downloads staan in `mislukte_urls.csv` in de cachemap.
   Vink *mislukte opnieuw proberen* aan om ze opnieuw te proberen.
3. **Classificeren**: *Start classificatie* stuurt elke thumbnail naar OpenAI
   (standaard `gpt-4o-mini`, `detail: low`). Test eerst met de
   **simulatiemodus** en een **limiet** (bv. 50). Elk resultaat wordt meteen
   bewaard, dus stoppen of een crash verliest niets. Optioneel kun je in de
   instellingen *escalatie* aanzetten: beelden onder de confidence-drempel
   gaan dan nog eens naar een sterker model.
4. **Reviewen** (tab 2): per product staan de afbeeldingen naast elkaar, met
   het voorgestelde label en de confidence (groen/oranje/rood). Voorgestelde
   packshots zijn standaard aangevinkt. Filter op label, op lage confidence
   of op "alleen niet gereviewd", en klik *Toepassen*.
5. **Exporteren**: *Exporteer als…* schrijft een kopie van het originele Excel
   met per afbeelding de kolommen `image N label` en `image N use`
   (true/false). Ernaast komt `<naam>_correcties.csv`: alle handmatig
   beoordeelde beelden met voorstel én eindbeslissing, bruikbaar als
   trainingsdata.

## Sneltoetsen in de review

| Toets | Actie |
|---|---|
| ← / → (horizontaal) of ↑ / ↓ (verticaal) | vorige / volgende afbeelding |
| **A** | accept: voorgesteld label goedkeuren, door naar de volgende afbeelding |
| **D** | deny: voorgesteld label afkeuren (label "onbekend", vinkje uit), door naar de volgende afbeelding |
| **1–5** | label zetten op packshot / sfeerbeeld / swatch / tekst / overig, door naar de volgende afbeelding |
| Spatie | vinkje "gebruik voor attributie" omschakelen |
| Backspace | review van de huidige afbeelding wissen |
| PgUp / PgDown | vorig / volgend product |

Alle toetsen en de oriëntatie (horizontaal/verticaal) stel je in bij
**Instellingen**. Ze worden onthouden.

## Instellingen (tab 3)

API-key, model, aantal gelijktijdige calls, escalatiemodel, confidence-drempel,
cachemap, aantal gelijktijdige downloads, product-id-kolom, extra beeldkolommen,
lay-out en sneltoetsen.

- Instellingen: `%APPDATA%\ProductImageClassifier\settings.json`
- API-key: `%APPDATA%\ProductImageClassifier\.env` (of een `.env` naast het programma)
- Cache: standaard `%LOCALAPPDATA%\ProductImageClassifier\cache` (thumbnails +
  `state.sqlite` met downloads, labels en reviews). Zet deze map liefst niet in
  OneDrive, want het gaat om tienduizenden kleine bestanden.
- Log: `%APPDATA%\ProductImageClassifier\logs\app.log`

## Kosten en schaal

Ongeveer 15.500 producten met ±50.500 beeldverwijzingen (±50.400 unieke URL's).
Dezelfde URL bij meerdere producten wordt één keer gedownload en één keer
geclassificeerd. Met `detail: low` kost elk beeld een vast, laag aantal tokens.
Controleer de actuele prijzen. Bij `detail: low` rekent `gpt-4o-mini` per beeld
veel meer beeldtokens aan dan `gpt-4o`, waardoor het per beeld niet
noodzakelijk goedkoper is. Vergelijk dus eerst op een kleine run.

## Installatie (ontwikkelaars)

```bash
pip install -r requirements.txt
python desktop_app.py
```
CLI: zie `CLAUDE.md` (`python cli.py --help`).

## Compileren

`build.ps1` (installeert de afhankelijkheden, draait PyInstaller en zipt
`dist/ProductImageClassifier/`). Of handmatig: `pyinstaller build.spec --noconfirm`.
Deel de **hele map** (als .zip), niet alleen de .exe.

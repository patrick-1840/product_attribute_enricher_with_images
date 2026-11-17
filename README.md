# Product Image Classifier

Vult productattributen aan die je op de **productfoto (packshot)** kan zien,
zoals de kleur van de verpakking (`colorPackaging`), de vorm van de fles
(`shapeOfTheBottle`) of het type dispenser (`typeDispenser`). Het werkt zoals
de Product Attribute Enricher: je kiest per categorie welke attributen gevuld
worden, het model kiest alleen uit de toegelaten waarden van SAP Commerce, je
controleert het resultaat, en je exporteert naar Excel en impex (met SAP-codes).
Het verschil is dat het model naar het productbeeld kijkt in plaats van naar de
tekst op de productpagina.

Daarnaast kan het programma alle productbeelden classificeren als packshot
product / packshot verpakking / packshot beide / sfeerbeeld / swatch / tekst /
overig, met een snelle review via het toetsenbord.

## Workflow

**inlezen → categorieën kiezen → packshots downloaden → attributen kiezen → verrijken → controleren → exporteren**

1. **Inlezen** (tab 1): kies het productbestand (de wekelijkse productlijst) en
   klik *Inlezen*. Het programma herkent zelf de beeldkolommen
   (`mainImageLink`, `imageLink1`...`5`, of `product image N`) en leest meteen de
   referentiebestanden (attribuuttypes en SAP-mapping, dezelfde als die van de
   attribuutverrijker; je kiest ze bij Instellingen).
2. **Categorieën kiezen** (tab 1, stap 2): vink aan met welke productcategorieën
   (`mainCategory`) je werkt, bv. alleen PERFUME. Per categorie zie je hoeveel
   producten er zijn en hoeveel beelden al gedownload en geclassificeerd zijn.
   Downloaden, classificeren, de review en de attribuutboom werken daarna
   **alleen op die categorieën**. Na de eerste keer inlezen staat er nog niets
   aangevinkt. De keuze wordt bewaard in de cache. Wat je eerder voor andere
   categorieën deed, blijft bewaard.
3. **Packshots downloaden**: *Start downloaden* met *alleen packshots* aangevinkt
   haalt per product het eerste beeld op (de front-foto). Je kunt pauzeren en
   stoppen. Wat al binnen is, wordt overgeslagen.
4. **Attributen kiezen** (tab 3): de boom toont per categorie alleen de
   attributen die volgens de SAP-matrix bij die categorie horen, met de
   vulgraad. Visuele attributen (oog-icoon) staan standaard aan. Klik een
   attribuut aan om rechts de **toegelaten waarden** te zien: alleen die mag het
   model teruggeven. Optioneel kun je een merkfilter zetten en "alleen lege
   waarden" gebruiken. Klik daarna *Werklijst opbouwen*.
5. **Verrijken**: *Start verrijking*. Per product gaat er één vraag naar het model,
   met het packshot en alle gekozen attributen samen. Test eerst met de
   **simulatiemodus** en een **limiet** (bv. 20 producten). Elk resultaat wordt
   meteen bewaard.
6. **Controleren** (tab 4): per rij zie je de gegenereerde waarde, de SAP-code
   en de motivatie, met rechts het packshot en de toegelaten waarden. Pas aan
   door te typen of door een waarde te dubbelklikken (Ctrl+dubbelklik voegt een
   waarde toe bij multi-attributen). Een waarde die niet in de lijst staat, is
   rood en gaat niet mee.
7. **Exporteren**: *Exporteer Excel...* (zelfde kolommen als de attribuutverrijker,
   plus de packshot-URL) en *Exporteer impex...* (zelfde impexformaat).

### Optioneel: beelden classificeren en reviewen

Wil je niet blindelings het eerste beeld als packshot gebruiken? Download dan
alle beelden van de gekozen categorieën, laat ze classificeren (tab 1, stap 4), review ze in tab 2 en zet
bij Instellingen *Packshot =* op "eerste beeld met vinkje 'gebruik'". Elk van de
drie packshot-labels (product, verpakking, beide) krijgt standaard het vinkje.
Via *Exporteer als...* in tab 1 schrijf je de beeldlabels terug naar het Excel
(`image N label` / `image N use`), samen met een correctielog als trainingsdata.

## Packshot-review (tab 2)

Een doorscrolbare lijst met één rij per product en alle beelden naast elkaar.
Het beeld dat de attribuutverrijking als packshot gebruikt, heeft een **groene
rand en een ★**. Beelden zonder vinkje "gebruik" zijn gedimd. Je scrollt snel
door de lijst (muiswiel, PgUp/PgDown) en grijpt alleen in waar het packshot
fout is. Daarna keur je de rest in één keer goed met **Goedkeuren tot hier**.

Handige filters: *alleen twijfelgevallen* (geen packshot, packshot is niet het
eerste beeld, of een beeld onder de confidence-drempel) en *alleen niet
gereviewd*. Met *Beeldgrootte* zet je meer producten op één scherm.

| Toets / muis | Actie |
|---|---|
| muiswiel, PgUp / PgDown, Home / End | snel scrollen |
| ↑ / ↓ | vorig / volgend product |
| ← / → | vorige / volgende afbeelding |
| klik | afbeelding selecteren |
| **dubbelklik** of **P** | deze afbeelding als packshot kiezen (de andere beelden van het product krijgen geen vinkje meer) |
| **Enter** | product goedkeuren (alle voorstellen) en naar het volgende product |
| rechtsklik | menu: packshot, vinkje, accept/deny, label, review wissen |
| A / D | voorstel van de afbeelding goedkeuren / afkeuren |
| 1–7 | label zetten op packshot product / verpakking / beide / sfeerbeeld / swatch / tekst / overig |
| Spatie | vinkje "gebruik voor attributie" omschakelen |
| Backspace | review van de afbeelding wissen |

Alle toetsen stel je in bij **Instellingen**. Ze worden onthouden. Let op:
de keuze van het packshot telt alleen als *Packshot =* bij Instellingen op
"eerste beeld met vinkje 'gebruik'" staat. In de modus "eerste beeld" wordt
altijd het eerste beeld gebruikt.

## Instellingen (tab 3)

API-key, model, aantal gelijktijdige calls, escalatiemodel, confidence-drempel,
cachemap, aantal gelijktijdige downloads, product-id-kolom, extra beeldkolommen
en sneltoetsen.

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

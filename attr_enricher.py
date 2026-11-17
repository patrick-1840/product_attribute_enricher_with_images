"""
Visuele attribuutverrijking: attributen bepalen op basis van het packshot.

Flow:
1. ``select_packshots`` kiest per product één packshot:
   - modus "first":  het eerste beeld (mainImageLink, anders imageLink1, …);
   - modus "review": het eerste beeld met vinkje "gebruik" (voorstel of review).
2. De werklijst (product × attribuut, zelfde logica als de attribuutverrijker)
   wordt aan dat packshot gekoppeld.
3. Per product gaat er ÉÉN call naar het visiemodel met het packshot en alle
   gekozen attributen + hun toegelaten waarden. Het beeld is het duurste deel
   van de call, dus bundelen per product scheelt veel.
4. Elke keuze wordt gevalideerd tegen de optielijst. Een waarde die er niet
   letterlijk in staat, wordt bewaard maar gemarkeerd (valid=0) en krijgt geen
   SAP-code — nooit stilzwijgend wegschrijven.
5. Resultaten gaan per product meteen naar ``attr_results`` (hervatbaar). Een
   product waarvan alle attributen al een resultaat hebben op hetzelfde
   packshot, wordt overgeslagen.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
from typing import Callable

import openai
import pandas as pd
from openai import AsyncOpenAI

from config import DEFAULT_ATTRIBUTE_PROMPT, VISUAL_HINTS, ClassifierConfig
from exporter import add_effective_columns
from reference import ReferenceData, sap_codes, validate_choice
from runcontrol import RunControl
from store import Store

logger = logging.getLogger(__name__)

SIM_MODEL = "simulatie"
ProgressFn = Callable[[int, int, str], None]


class QuotaExceeded(RuntimeError):
    pass


# ------------------------------------------------------------------ packshots

def select_packshots(cfg: ClassifierConfig, store: Store) -> pd.DataFrame:
    """Eén packshot per product: kolommen product_id, slot, url, thumb, fetch_status."""
    cols = ["product_id", "slot", "url", "thumb", "fetch_status"]
    if cfg.packshot_mode == "review":
        t = add_effective_columns(store.item_table(scoped=False))
        t = t[t["eff_use"] == True]  # noqa: E712  (None = onbekend telt niet)
        t = t.sort_values(["product_id", "position"]).drop_duplicates("product_id")
        return t[cols].reset_index(drop=True)
    return store.first_images(scoped=False)[cols]


def attach_packshots(work: pd.DataFrame, packshots: pd.DataFrame) -> pd.DataFrame:
    """Koppelt het packshot (url/thumb/status) aan elke werklijstrij."""
    ps = packshots.rename(columns={"product_id": "variant_code", "url": "packshot_url",
                                   "slot": "packshot_slot"})
    out = work.merge(ps, on="variant_code", how="left")
    return out


# ------------------------------------------------------------------ prompt

def build_prompt(cfg: ClassifierConfig, ref: ReferenceData, attrs: list[str],
                 brand: str, product_name: str) -> str:
    blocks = []
    for attr in attrs:
        name = ref.display_name(attr)
        rule = "multiple allowed (max 3)" if ref.is_multi(attr) else "exactly one"
        hint = VISUAL_HINTS.get(attr)
        opts = "; ".join(ref.options_for(attr))
        blocks.append(f'- "{name}" ({rule}){": " + hint if hint else ""}\n  options: {opts}')
    fields = dict(brand=brand or "?", product_name=product_name or "?",
                  attribute_block="\n".join(blocks))
    try:
        return cfg.attribute_prompt.format(**fields)
    except (KeyError, IndexError) as exc:
        logger.error("Ongeldige attribuutprompt (%s); standaardprompt gebruikt.", exc)
        return DEFAULT_ATTRIBUTE_PROMPT.format(**fields)


def parse_attribute_response(text: str, ref: ReferenceData, attrs: list[str]
                             ) -> dict[str, tuple[object, str | None]]:
    """{attribute_key: (ruwe keuze, motivatie)}; sleutels hoofdletterongevoelig."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not m:
            raise ValueError(f"geen JSON in antwoord: {cleaned[:150]}")
        data = json.loads(m.group(0))
    by_lower = {str(k).strip().lower(): v for k, v in data.items()}
    out = {}
    for attr in attrs:
        entry = by_lower.get(attr) or by_lower.get(ref.display_name(attr).lower())
        if isinstance(entry, dict):
            out[attr] = (entry.get("choice"), entry.get("motivation"))
        elif entry is not None:
            out[attr] = (entry, None)
        else:
            out[attr] = (None, "Attribuut ontbrak in het modelantwoord.")
    return out


# ------------------------------------------------------------------ enricher

class AttributeEnricher:
    def __init__(self, cfg: ClassifierConfig, ref: ReferenceData,
                 control: RunControl | None = None):
        self.cfg = cfg
        self.ref = ref
        self.control = control or RunControl()
        self._client: AsyncOpenAI | None = None
        if cfg.use_openai:
            if not cfg.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY niet gevonden. Vul de key in bij Instellingen.")
            self._client = AsyncOpenAI(api_key=cfg.openai_api_key, timeout=cfg.openai_timeout,
                                       max_retries=0)

    def run(self, work: pd.DataFrame, limit: int | None = None, redo: bool = False,
            progress: ProgressFn | None = None) -> dict[str, int]:
        """``work`` = werklijst met packshot (zie ``attach_packshots``).
        ``limit`` = max. aantal producten (calls) in deze run."""
        return asyncio.run(self._run(work, limit, redo, progress))

    def _todo(self, store: Store, work: pd.DataFrame, redo: bool
              ) -> tuple[list[tuple[str, pd.DataFrame]], int]:
        have = work[work["fetch_status"] == "ok"]
        no_packshot = work.loc[work["fetch_status"] != "ok", "variant_code"].nunique()
        if not redo:
            done = store.attr_results()
            done = done[done["error"].isna()]
            done_keys = set(zip(done["product_id"], done["attribute_key"], done["url"]))
            mask = [(p, a, u) not in done_keys for p, a, u in
                    zip(have["variant_code"], have["attribute_key"], have["packshot_url"])]
            have = have[mask]
        groups = [(pid, g) for pid, g in have.groupby("variant_code", sort=False)]
        return groups, no_packshot

    async def _run(self, work: pd.DataFrame, limit: int | None, redo: bool,
                   progress: ProgressFn | None) -> dict[str, int]:
        store = Store(self.cfg.db_path)
        try:
            if self.cfg.use_openai:
                n = store.clear_simulated_attr_results(SIM_MODEL)
                if n:
                    logger.info("%d gesimuleerde attribuutresultaten gewist vóór echte run.", n)
            groups, no_packshot = self._todo(store, work, redo)
            if limit:
                groups = groups[:limit]
            total = len(groups)
            counts = {"products": total, "ok": 0, "failed": 0, "values": 0, "invalid": 0,
                      "no_packshot": no_packshot}
            model = self.cfg.attribute_model if self.cfg.use_openai else SIM_MODEL
            logger.info("Attribuutverrijking gestart: %d producten, model %s (%s detail), "
                        "%d producten zonder packshot.", total, model,
                        self.cfg.attribute_detail, no_packshot)
            if progress:
                progress(0, total, "Attributen bepalen…")
            if not total:
                return counts
            sem = asyncio.Semaphore(self.cfg.concurrent_requests)
            done = 0

            async def worker(pid: str, g: pd.DataFrame) -> None:
                nonlocal done
                async with sem:
                    if not await self.control.checkpoint():
                        return
                    attrs = list(dict.fromkeys(g["attribute_key"]))
                    first = g.iloc[0]
                    url = first["packshot_url"]
                    try:
                        answers = await self._ask(pid, attrs, first, model)
                        for attr in attrs:
                            raw, motivation = answers[attr]
                            value, valid = validate_choice(raw, self.ref.options_for(attr))
                            if value and not valid:
                                motivation = (motivation or "") + " [waarde staat niet in de optielijst]"
                                counts["invalid"] += 1
                            counts["values"] += bool(value)
                            store.save_attr_result(pid, attr, url, value, valid, motivation, model)
                        counts["ok"] += 1
                    except QuotaExceeded as exc:
                        logger.error("Geen OpenAI-tegoed meer; verrijking gestopt. %s", exc)
                        counts["failed"] += 1
                        counts["quota"] = 1
                        self.control.cancel()
                    except Exception as exc:
                        logger.warning("Attribuutverrijking mislukt voor %s: %s", pid, exc)
                        for attr in attrs:
                            store.save_attr_result(pid, attr, url, None, True, None, model,
                                                   error=str(exc)[:500])
                        counts["failed"] += 1
                    done += 1
                    if progress:
                        progress(done, total, f"{counts['ok']} producten ok, {counts['failed']} mislukt, "
                                              f"{counts['values']} waarden ({counts['invalid']} ongeldig)")

            chunk = 300
            for start in range(0, total, chunk):
                if self.control.cancelled:
                    break
                await asyncio.gather(*(worker(p, g) for p, g in groups[start:start + chunk]))
            logger.info("Attribuutverrijking %s: %s",
                        "gestopt" if self.control.cancelled else "klaar", counts)
            return counts
        finally:
            store.close()

    async def _ask(self, pid: str, attrs: list[str], row: pd.Series, model: str
                   ) -> dict[str, tuple[object, str | None]]:
        if not self.cfg.use_openai:
            await asyncio.sleep(random.uniform(0.01, 0.05))
            out = {}
            for attr in attrs:
                opts = self.ref.options_for(attr)
                choice = random.choice(opts) if opts and random.random() > 0.15 else None
                out[attr] = (choice, "[SIMULATIE] willekeurige keuze")
            return out

        thumb = self.cfg.thumbs_dir / str(row["thumb"])
        b64 = base64.b64encode(await asyncio.to_thread(thumb.read_bytes)).decode("ascii")
        prompt = build_prompt(self.cfg, self.ref, attrs, str(row.get("brand") or ""),
                              str(row.get("productName") or ""))
        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                                                    "detail": self.cfg.attribute_detail}},
            ]}],
            "response_format": {"type": "json_object"},
        }
        if model.startswith("gpt-5") or model.startswith("o"):
            kwargs["max_completion_tokens"] = max(self.cfg.attribute_max_tokens, 2000)
        else:
            kwargs["max_tokens"] = self.cfg.attribute_max_tokens
            kwargs["temperature"] = 0.0
        logger.info("[%s] Prompt naar %s:\n%s", pid, model, prompt)

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                logger.info("[%s] Antwoord: %s", pid, content)
                return parse_attribute_response(content, self.ref, attrs)
            except openai.RateLimitError as exc:
                code = None
                try:
                    code = (exc.body or {}).get("error", {}).get("code")
                except AttributeError:
                    pass
                if code == "insufficient_quota":
                    raise QuotaExceeded(str(exc)) from exc
                last_exc = exc
                await asyncio.sleep(5 * attempt + random.random() * 3)
            except (openai.APITimeoutError, openai.APIConnectionError,
                    openai.InternalServerError) as exc:
                last_exc = exc
                await asyncio.sleep(2 * attempt)
            except (ValueError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt >= 2:
                    break
        raise RuntimeError(f"{type(last_exc).__name__}: {last_exc}")


# ------------------------------------------------------------------ resultaten

RESULT_COLUMNS = [
    "mainCategory", "brand", "productName", "productUrl", "variant_code", "baseProduct",
    "Product attribute", "attribute_key", "current value", "generated value", "new_sap_values",
    "valid", "motivation", "model", "error", "approved", "packshot_url", "thumb", "fetch_status",
]


def results_table(cfg: ClassifierConfig, ref: ReferenceData, work: pd.DataFrame) -> pd.DataFrame:
    """Werklijst + resultaten (alleen resultaten op het HUIDIGE packshot).
    ``generated value`` = manuele aanpassing, anders de modelkeuze.
    ``approved`` = opgeslagen vinkje, anders: aan bij een geldige waarde."""
    store = Store(cfg.db_path)
    try:
        res = store.attr_results()
    finally:
        store.close()
    res = res.rename(columns={"product_id": "variant_code", "url": "packshot_url"})
    t = work.merge(res, on=["variant_code", "attribute_key", "packshot_url"], how="left")
    # Een manuele aanpassing (ook een lege = bewust gewist) wint van de modelkeuze.
    t["generated value"] = [e if isinstance(e, str) else (c if isinstance(c, str) else None)
                            for e, c in zip(t["edited_value"], t["choice"])]
    t["generated value"] = t["generated value"].replace("", None)
    # Geldigheid altijd opnieuw bepalen: ook manuele aanpassingen moeten in de lijst staan.
    t["valid"] = [validate_choice(v, ref.options_for(a))[1]
                  for a, v in zip(t["attribute_key"], t["generated value"])]
    has_value = t["generated value"].notna()
    default_ok = has_value & t["valid"]
    t["approved"] = [bool(a) if pd.notna(a) else bool(d) for a, d in zip(t["approved"], default_ok)]
    t["new_sap_values"] = [sap_codes(ref, a, v) if ok else ""
                           for a, v, ok in zip(t["attribute_key"], t["generated value"], t["valid"])]
    for c in RESULT_COLUMNS:
        if c not in t.columns:
            t[c] = None
    return t[RESULT_COLUMNS]


# ------------------------------------------------------------------ export

# Zelfde kolommen als de attribuutverrijker, aangevuld met de beeldinfo.
EXPORT_COLUMNS = [
    "mainCategory", "brand", "productName", "productUrl", "variant_code", "baseProduct",
    "new_sap_values", "Product attribute", "generated value", "current value",
    "motivation", "packshot_url",
]


def approved_rows(table: pd.DataFrame) -> pd.DataFrame:
    """Aangevinkte rijen met een geldige waarde (= die een SAP-code krijgen)."""
    return table[table["approved"] & table["generated value"].notna() & table["valid"]].copy()


def export_attribute_excel(table: pd.DataFrame, path) -> int:
    rows = approved_rows(table)
    rows[[c for c in EXPORT_COLUMNS if c in rows.columns]].to_excel(path, index=False)
    logger.info("Attributen-Excel bewaard: %s (%d rijen)", path, len(rows))
    return len(rows)


def export_attribute_impex(table: pd.DataFrame, path, catalog: str) -> int:
    from reference import build_impex
    rows = approved_rows(table)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_impex(rows, catalog))
    logger.info("Impex bewaard: %s (%d rijen)", path, len(rows))
    return len(rows)

"""
Classificatie van thumbnails met een OpenAI-visiemodel.

- Eén call per unieke afbeelding, ``detail: low`` (vaste, lage tokenkost per beeld).
- Antwoord strikt als JSON: ``{"label": ..., "confidence": 0.0-1.0}``; het model
  wordt via ``response_format=json_object`` daartoe gedwongen, en het label moet
  in ``config.LABELS`` zitten — anders telt het als fout en wordt het later
  opnieuw geprobeerd. Nooit stilzwijgend een ongeldig label wegschrijven.
- Elk resultaat gaat meteen naar de database: onderbreken verliest niets.
- Optioneel escaleren naar een sterker model (standaard ``gpt-4o``) wanneer de
  confidence onder de drempel ligt; het eerste resultaat blijft bewaard.
- Simulatiemodus (``use_openai=False``): willekeurige labels, geen kosten.
  Gesimuleerde resultaten krijgen model 'simulatie' en worden bij de eerstvolgende
  echte run automatisch weer leeggemaakt.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
from pathlib import Path
from typing import Callable

import openai
from openai import AsyncOpenAI

from config import LABELS, ClassifierConfig
from runcontrol import RunControl
from store import Store

logger = logging.getLogger(__name__)

SIM_MODEL = "simulatie"

# Het model antwoordt soms in het Engels; die synoniemen vangen we op.
LABEL_SYNONYMS = {
    "lifestyle": "sfeerbeeld", "mood": "sfeerbeeld", "sfeer": "sfeerbeeld",
    "text": "tekst", "infographic": "tekst",
    "other": "overig", "texture": "swatch",
    "packshot": "packshot_product", "pack shot": "packshot_product",
    "product": "packshot_product", "packshot product": "packshot_product",
    "packaging": "packshot_verpakking", "box": "packshot_verpakking",
    "packshot verpakking": "packshot_verpakking", "packshot_packaging": "packshot_verpakking",
    "both": "packshot_beide", "packshot beide": "packshot_beide",
    "packshot_both": "packshot_beide",
}

ProgressFn = Callable[[int, int, str], None]


class QuotaExceeded(RuntimeError):
    """Geen tegoed meer bij OpenAI; wachten lost dit niet op."""


def parse_response(text: str) -> tuple[str, float]:
    """Parseert het modelantwoord. Werpt ValueError bij een ongeldig antwoord."""
    if not text or not text.strip():
        raise ValueError("leeg antwoord")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not m:
            raise ValueError(f"geen JSON: {cleaned[:120]}")
        data = json.loads(m.group(0))
    label = str(data.get("label", "")).strip().lower()
    label = LABEL_SYNONYMS.get(label, label)
    if label not in LABELS:
        raise ValueError(f"onbekend label '{label}'")
    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        raise ValueError(f"ongeldige confidence: {data.get('confidence')!r}")
    return label, max(0.0, min(1.0, conf))


class ImageClassifier:
    def __init__(self, cfg: ClassifierConfig, control: RunControl | None = None):
        self.cfg = cfg
        self.control = control or RunControl()
        self._client: AsyncOpenAI | None = None
        if cfg.use_openai:
            if not cfg.openai_api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY niet gevonden. Vul de key in bij Instellingen "
                    "(wordt bewaard in %APPDATA%/ProductImageClassifier/.env) of zet een "
                    ".env naast het programma.")
            self._client = AsyncOpenAI(api_key=cfg.openai_api_key, timeout=cfg.openai_timeout,
                                       max_retries=0)

    def run(self, limit: int | None = None, progress: ProgressFn | None = None) -> dict[str, int]:
        return asyncio.run(self._run(limit, progress))

    async def _run(self, limit: int | None, progress: ProgressFn | None) -> dict[str, int]:
        store = Store(self.cfg.db_path)
        try:
            if self.cfg.use_openai:
                n = store.conn.execute(
                    "UPDATE images SET label=NULL, confidence=NULL, model=NULL "
                    "WHERE model=?", (SIM_MODEL,)).rowcount
                if n:
                    logger.info("%d gesimuleerde classificaties gewist vóór echte run.", n)
            todo = store.urls_to_classify()
            if limit:
                todo = todo[:limit]
            total = len(todo)
            counts = {"ok": 0, "failed": 0, "escalated": 0, "total": total}
            model = self.cfg.openai_model if self.cfg.use_openai else SIM_MODEL
            logger.info("Classificatie gestart: %d beelden, model %s, concurrency %d.",
                        total, model, self.cfg.concurrent_requests)
            if progress:
                progress(0, total, "Classificeren…")
            if not total:
                return counts

            sem = asyncio.Semaphore(self.cfg.concurrent_requests)
            done = 0

            async def worker(url: str, thumb: str) -> None:
                nonlocal done
                async with sem:
                    if not await self.control.checkpoint():
                        return
                    try:
                        label, conf, raw = await self._classify(self.cfg.thumbs_dir / thumb,
                                                                model, url)
                        first = None
                        if (self.cfg.use_openai and self.cfg.escalate
                                and conf < self.cfg.confidence_threshold
                                and self.cfg.escalation_model != model):
                            first = (label, conf, model)
                            label, conf, raw = await self._classify(
                                self.cfg.thumbs_dir / thumb, self.cfg.escalation_model, url)
                            counts["escalated"] += 1
                        used_model = self.cfg.escalation_model if first else model
                        store.save_classification(url, label, conf, used_model, raw, first)
                        counts["ok"] += 1
                    except QuotaExceeded as exc:
                        logger.error("Geen OpenAI-tegoed meer; classificatie gestopt. %s", exc)
                        store.mark_classify_failed(url, "insufficient_quota")
                        counts["failed"] += 1
                        self.control.cancel()
                        counts["quota"] = 1
                    except Exception as exc:
                        logger.warning("Classificatie mislukt voor %s: %s", url, exc)
                        store.mark_classify_failed(url, str(exc))
                        counts["failed"] += 1
                    done += 1
                    if progress:
                        progress(done, total, f"{counts['ok']} ok, {counts['failed']} mislukt"
                                 + (f", {counts['escalated']} geëscaleerd" if counts["escalated"] else ""))

            chunk = 500
            for start in range(0, total, chunk):
                if self.control.cancelled:
                    break
                await asyncio.gather(*(worker(u, t) for u, t in todo[start:start + chunk]))

            logger.info("Classificatie %s: %s", "gestopt" if self.control.cancelled else "klaar", counts)
            return counts
        finally:
            store.close()

    async def _classify(self, thumb_path: Path, model: str, call_id: str) -> tuple[str, float, str]:
        if not self.cfg.use_openai:
            await asyncio.sleep(random.uniform(0.01, 0.05))
            label = random.choices(LABELS, weights=[4, 1, 2, 2, 1, 1, 1])[0]
            conf = round(random.uniform(0.4, 1.0), 2)
            return label, conf, json.dumps({"label": label, "confidence": conf})

        b64 = base64.b64encode(await asyncio.to_thread(thumb_path.read_bytes)).decode("ascii")
        kwargs: dict = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.cfg.prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}", "detail": "low"}},
                ],
            }],
            "response_format": {"type": "json_object"},
        }
        if model.startswith("gpt-5") or model.startswith("o"):
            # reasoning-modellen: geen temperature, ander tokens-parameter
            kwargs["max_completion_tokens"] = max(self.cfg.openai_max_tokens, 1000)
        else:
            kwargs["max_tokens"] = self.cfg.openai_max_tokens
            kwargs["temperature"] = 0.0

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                label, conf = parse_response(content)
                return label, conf, content
            except openai.RateLimitError as exc:
                code = None
                try:
                    code = (exc.body or {}).get("error", {}).get("code")
                except AttributeError:
                    pass
                if code == "insufficient_quota":
                    raise QuotaExceeded(str(exc)) from exc
                wait = 5 * attempt + random.random() * 3
                logger.warning("[%s] Rate limit, %ds wachten (poging %d/3).", call_id, wait, attempt)
                last_exc = exc
                await asyncio.sleep(wait)
            except (openai.APITimeoutError, openai.APIConnectionError,
                    openai.InternalServerError) as exc:
                last_exc = exc
                await asyncio.sleep(2 * attempt)
            except ValueError as exc:   # ongeldig antwoord: één keer opnieuw vragen
                last_exc = exc
                if attempt >= 2:
                    break
        raise RuntimeError(f"{type(last_exc).__name__}: {last_exc}")

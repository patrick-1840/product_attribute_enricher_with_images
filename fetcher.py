"""
Downloaden van productbeelden en bewaren als thumbnail in de cachemap.

- Elke unieke URL wordt maar één keer gedownload (de ``images``-tabel is per URL).
- Hervatbaar: beelden met status 'ok' worden overgeslagen; elk resultaat wordt
  meteen in de database gezet.
- Concurrency (standaard 10), timeouts en retries met exponentiële backoff.
  De retry-teller hoogt ALTIJD op, ook bij een timeout (valkuil uit de
  attribuutverrijker: anders oneindige lus).
- Kleine willekeurige pauze per request (0,05–0,3 s), zodat de parallelle load
  op de webshop niet in een strak ritme binnenkomt.
- Mislukte URL's komen in de database én in ``mislukte_urls.csv`` in de cachemap.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Callable

import aiohttp
from PIL import Image

from config import ClassifierConfig
from runcontrol import RunControl
from store import Store

logger = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Statuscodes waarbij opnieuw proberen zin heeft.
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

ProgressFn = Callable[[int, int, str], None]


def thumb_name(url: str) -> str:
    """Relatief pad van de thumbnail: twee niveaus om mappen klein te houden."""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return f"{h[:2]}/{h}.jpg"


def make_thumbnail(data: bytes, target: Path, max_px: int, quality: int) -> None:
    """Decodeert, verkleint en bewaart als JPEG. Transparantie wordt op wit
    gezet (zoals op de webshop), zodat packshots met alfakanaal niet zwart worden."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        img.save(tmp, "JPEG", quality=quality, optimize=True)
        tmp.replace(target)   # atomisch: nooit een half geschreven thumbnail


class ImageFetcher:
    def __init__(self, cfg: ClassifierConfig, control: RunControl | None = None):
        self.cfg = cfg
        self.control = control or RunControl()

    def run(self, retry_failed: bool = False, limit: int | None = None,
            progress: ProgressFn | None = None) -> dict[str, int]:
        """Blokkerend; draait in een worker-thread (of de CLI)."""
        return asyncio.run(self._run(retry_failed, limit, progress))

    async def _run(self, retry_failed: bool, limit: int | None,
                   progress: ProgressFn | None) -> dict[str, int]:
        store = Store(self.cfg.db_path)
        thumbs = self.cfg.thumbs_dir
        thumbs.mkdir(parents=True, exist_ok=True)
        try:
            reset = store.reset_missing_thumbs(thumbs)
            if reset:
                logger.info("%d beelden opnieuw in de wachtrij (thumbnail ontbrak).", reset)
            urls = store.urls_to_fetch(retry_failed=retry_failed)
            if limit:
                urls = urls[:limit]
            total = len(urls)
            counts = {"ok": 0, "failed": 0, "total": total}
            logger.info("Downloaden gestart: %d beelden (concurrency %d).",
                        total, self.cfg.concurrent_downloads)
            if progress:
                progress(0, total, "Downloaden…")
            if not total:
                return counts

            sem = asyncio.Semaphore(self.cfg.concurrent_downloads)
            timeout = aiohttp.ClientTimeout(total=self.cfg.download_timeout)
            connector = aiohttp.TCPConnector(limit=self.cfg.concurrent_downloads)
            done = 0

            async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                             headers={"User-Agent": USER_AGENT}) as session:

                async def worker(url: str) -> None:
                    nonlocal done
                    async with sem:
                        if not await self.control.checkpoint():
                            return
                        name = thumb_name(url)
                        ok, err = await self._fetch_one(session, url, thumbs / name)
                        if ok:
                            store.mark_fetched(url, name)
                            counts["ok"] += 1
                        else:
                            store.mark_fetch_failed(url, err)
                            self._log_failed(url, err)
                            counts["failed"] += 1
                        done += 1
                        if progress:
                            progress(done, total, f"{counts['ok']} ok, {counts['failed']} mislukt")

                # Taken in blokken aanmaken: 75k coroutines tegelijk is onnodig zwaar.
                chunk = 500
                for start in range(0, total, chunk):
                    if self.control.cancelled:
                        break
                    await asyncio.gather(*(worker(u) for u in urls[start:start + chunk]))

            logger.info("Downloaden %s: %s", "gestopt" if self.control.cancelled else "klaar", counts)
            return counts
        finally:
            store.close()

    async def _fetch_one(self, session: aiohttp.ClientSession, url: str,
                         target: Path) -> tuple[bool, str]:
        attempt = 0
        last_err = ""
        while attempt < self.cfg.retry_limit:
            attempt += 1   # altijd ophogen, ook bij timeout
            try:
                await asyncio.sleep(random.uniform(0.05, 0.3))
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        ctype = resp.headers.get("Content-Type", "")
                        if not data:
                            return False, "lege respons"
                        try:
                            await asyncio.to_thread(make_thumbnail, data, target,
                                                    self.cfg.thumb_max_px, self.cfg.thumb_quality)
                        except Exception as exc:  # geen (geldig) beeld
                            return False, f"geen geldig beeld ({ctype}): {exc}"
                        return True, ""
                    last_err = f"HTTP {resp.status}"
                    if resp.status not in RETRY_STATUS:
                        return False, last_err
            except asyncio.TimeoutError:
                last_err = "timeout"
            except aiohttp.ClientError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < self.cfg.retry_limit:
                await asyncio.sleep(self.cfg.retry_backoff ** attempt + random.random())
        logger.warning("Download mislukt na %d pogingen: %s (%s)", attempt, url, last_err)
        return False, last_err

    def _log_failed(self, url: str, error: str) -> None:
        path = self.cfg.failed_log
        try:
            new = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh, delimiter=";")
                if new:
                    w.writerow(["tijdstip", "url", "fout"])
                w.writerow([datetime.now().isoformat(timespec="seconds"), url, error])
        except OSError as exc:
            logger.warning("Kon %s niet bijwerken: %s", path, exc)

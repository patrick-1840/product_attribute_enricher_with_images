"""
Command-line-variant van dezelfde flow (handig voor testen en lange runs).

Voorbeelden:
    python cli.py load --file "C:/pad/naar/productlijst.xlsx"
    python cli.py fetch --limit 50
    python cli.py classify --limit 50 --simulate      # testrun zonder API-kosten
    python cli.py classify --model gpt-4o-mini
    python cli.py stats
    python cli.py export --out "C:/pad/naar/met_labels.xlsx"

Gebruikt dezelfde instellingen (%APPDATA%/ProductImageClassifier/settings.json)
en dezelfde cache als de desktop-app.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from config import ClassifierConfig, setup_logging

logger = logging.getLogger("cli")


def _progress(done: int, total: int, msg: str) -> None:
    if total and (done == total or done % 50 == 0):
        print(f"  {done}/{total}  {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Product Image Classifier (CLI)")
    ap.add_argument("--cache-dir", help="andere cachemap dan in de instellingen")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load", help="Excel inlezen en werklijst opbouwen")
    p.add_argument("--file", help="productbestand (.xlsx/.csv)")

    p = sub.add_parser("fetch", help="beelden downloaden + thumbnails maken")
    p.add_argument("--limit", type=int)
    p.add_argument("--retry-failed", action="store_true")

    p = sub.add_parser("classify", help="thumbnails classificeren")
    p.add_argument("--limit", type=int)
    p.add_argument("--simulate", action="store_true", help="geen API-calls, willekeurige labels")
    p.add_argument("--model")
    p.add_argument("--escalate", action="store_true")

    sub.add_parser("stats", help="voortgang tonen")

    p = sub.add_parser("export", help="Excel met labelkolommen schrijven")
    p.add_argument("--out", required=True)
    p.add_argument("--file", help="origineel productbestand (standaard: uit de instellingen)")

    args = ap.parse_args(argv)
    setup_logging()
    cfg = ClassifierConfig()
    cfg.load_user_settings()
    if args.cache_dir:
        cfg.cache_dir = Path(args.cache_dir)

    if args.cmd == "load":
        from loader import build_image_table, read_product_file
        from store import Store
        if args.file:
            cfg.product_file = Path(args.file)
        items, products, report = build_image_table(read_product_file(cfg.product_file), cfg)
        store = Store(cfg.db_path)
        store.replace_items(items, products)
        print(report.summary())
        print(store.stats())
        store.close()

    elif args.cmd == "fetch":
        from fetcher import ImageFetcher
        print(ImageFetcher(cfg).run(retry_failed=args.retry_failed, limit=args.limit,
                                    progress=_progress))

    elif args.cmd == "classify":
        from classifier import ImageClassifier
        cfg.use_openai = not args.simulate
        if args.model:
            cfg.openai_model = args.model
        if args.escalate:
            cfg.escalate = True
        print(ImageClassifier(cfg).run(limit=args.limit, progress=_progress))

    elif args.cmd == "stats":
        from store import Store
        store = Store(cfg.db_path)
        for k, v in store.stats().items():
            print(f"{k:>16}: {v}")
        store.close()

    elif args.cmd == "export":
        from exporter import export_excel
        if args.file:
            cfg.product_file = Path(args.file)
        matched, log_path = export_excel(cfg, cfg.product_file, Path(args.out))
        print(f"{matched} rijen met beelden geëxporteerd naar {args.out}; correctielog: {log_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

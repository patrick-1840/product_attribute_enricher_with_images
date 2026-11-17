"""
Command-line-variant van dezelfde flow (handig voor testen en lange runs).

Voorbeelden:
    python cli.py load --file "C:/pad/naar/productlijst.xlsx"
    python cli.py categories                          # overzicht + huidige keuze
    python cli.py categories --set PERFUME --set MAKE-UP   # alleen deze categorieën
    python cli.py fetch --limit 50
    python cli.py classify --limit 50 --simulate      # testrun zonder API-kosten
    python cli.py classify --model gpt-4o-mini
    python cli.py stats
    python cli.py export --out "C:/pad/naar/met_labels.xlsx"

    # visuele attribuutverrijking op het packshot (eerste beeld per product)
    python cli.py enrich --category PERFUME --attributes colorPackaging,shapeOfTheBottle --limit 20 --simulate
    python cli.py export-attrs --category PERFUME --attributes colorPackaging --out res.xlsx --impex res.impex

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

    p = sub.add_parser("categories", help="categorieën tonen en kiezen (downloaden, "
                                           "classificeren en review werken alleen daarop)")
    p.add_argument("--set", action="append", metavar="CATEGORIE",
                   help="mainCategory om te verwerken (meermaals te gebruiken)")
    p.add_argument("--all", action="store_true", help="keuze wissen = alle categorieën")

    p = sub.add_parser("fetch", help="beelden downloaden + thumbnails maken")
    p.add_argument("--limit", type=int)
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--all", action="store_true",
                   help="alle beelden downloaden (standaard enkel het packshot in modus 'first')")

    p = sub.add_parser("classify", help="thumbnails classificeren")
    p.add_argument("--limit", type=int)
    p.add_argument("--simulate", action="store_true", help="geen API-calls, willekeurige labels")
    p.add_argument("--model")
    p.add_argument("--escalate", action="store_true")

    sub.add_parser("stats", help="voortgang tonen")

    def attr_args(p):
        p.add_argument("--file", help="productbestand (standaard: uit de instellingen)")
        p.add_argument("--category", action="append", required=True,
                       help="mainCategory, bv. PERFUME (meermaals te gebruiken)")
        p.add_argument("--attributes", required=True, help="kommagescheiden, bv. colorPackaging")
        p.add_argument("--brand", action="append", help="merkfilter (meermaals te gebruiken)")
        p.add_argument("--all-values", action="store_true",
                       help="ook attributen die al een waarde hebben")

    p = sub.add_parser("enrich", help="attributen bepalen op het packshot")
    attr_args(p)
    p.add_argument("--limit", type=int, help="max. aantal producten (calls)")
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--redo", action="store_true", help="ook producten die al een resultaat hebben")
    p.add_argument("--model")

    p = sub.add_parser("export-attrs", help="attribuutresultaten naar Excel/impex")
    attr_args(p)
    p.add_argument("--out", required=True)
    p.add_argument("--impex")

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

    elif args.cmd == "categories":
        from store import Store
        store = Store(cfg.db_path)
        try:
            if args.all:
                store.set_scope(None)
            elif args.set:
                known = set(store.category_overview()["category"])
                unknown = [c for c in args.set if c.strip().upper() not in known]
                if unknown:
                    print(f"Onbekende categorie(ën): {unknown}")
                    return 1
                store.set_scope(args.set)
            scope = store.scope()
            ov = store.category_overview()
            print(f"Keuze: {'alle categorieën' if scope is None else ', '.join(scope) or '(geen)'}")
            for r in ov.itertuples(index=False):
                mark = "x" if scope is None or r.category in scope else " "
                print(f" [{mark}] {r.category or '(leeg)':<28} {r.products:>6} producten "
                      f"{r.images:>7} beelden  packshots {r.packshots_fetched}/{r.packshots}  "
                      f"gedownload {r.fetched}  geclassificeerd {r.classified}")
        finally:
            store.close()

    elif args.cmd == "fetch":
        from fetcher import ImageFetcher
        only_first = cfg.packshot_mode == "first" and cfg.only_fetch_packshots and not args.all
        print(ImageFetcher(cfg).run(retry_failed=args.retry_failed, limit=args.limit,
                                    progress=_progress, only_first=only_first))

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

    elif args.cmd in ("enrich", "export-attrs"):
        from attr_enricher import (AttributeEnricher, attach_packshots, export_attribute_excel,
                                   export_attribute_impex, results_table, select_packshots)
        from loader import read_product_file
        from reference import build_work_items, load_reference_data
        from store import Store
        if args.file:
            cfg.product_file = Path(args.file)
        ref = load_reference_data(cfg)
        catalog = read_product_file(cfg.product_file)
        attrs = [a.strip().lower() for a in args.attributes.split(",") if a.strip()]
        work = build_work_items(catalog, ref, {c: attrs for c in args.category},
                                only_empty=not args.all_values, brands=args.brand)
        if work.empty:
            print("Geen rijen in de werklijst voor deze selectie.")
            return 1
        store = Store(cfg.db_path)
        work = attach_packshots(work, select_packshots(cfg, store))
        store.close()
        print(f"Werklijst: {len(work)} rijen, {work['variant_code'].nunique()} producten, "
              f"{(work['fetch_status'] == 'ok').sum()} rijen met gedownload packshot.")
        if args.cmd == "enrich":
            cfg.use_openai = not args.simulate
            if args.model:
                cfg.attribute_model = args.model
            print(AttributeEnricher(cfg, ref).run(work, limit=args.limit, redo=args.redo,
                                                  progress=_progress))
        else:
            table = results_table(cfg, ref, work)
            n = export_attribute_excel(table, args.out)
            print(f"{n} goedgekeurde rijen naar {args.out}")
            if args.impex:
                export_attribute_impex(table, args.impex, cfg.impex_catalog)
                print(f"impex: {args.impex}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

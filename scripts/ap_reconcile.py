"""Supplier debt (кредиторлик): 1C against SAP B1 — the command line, read-only.

The comparison itself is ``integrations/onec/payables.py`` (the Director can
also ask OPS Manager Bot for it, 2026-10-10). This writes the same Excel
workbook to a file; nothing goes into the database, 1C, SAP or Telegram, and
no figure is printed — only counts.

Run:
    python scripts/ap_reconcile.py --onec-json 1c.json --sap sap.xlsx --out solishtirish.xlsx
    python scripts/ap_reconcile.py --onec-json 1c.json --sap-pushed --out solishtirish.xlsx
    python scripts/ap_reconcile.py --pull-1c 1c.json      # read 1C now (needs ONEC_* set)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.onec.payables import compare, pull_onec, pushed_sap_rows, read_table, write_workbook  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="1C vs SAP supplier debt, read-only")
    parser.add_argument("--pull-1c", type=Path, help="read 1C now (GET only) into this JSON file")
    parser.add_argument("--onec-json", type=Path, help="1C data read earlier (--pull-1c)")
    parser.add_argument("--sap", type=Path, help="SAP B1 export of the suppliers' balances (xlsx or csv)")
    parser.add_argument("--sap-pushed", action="store_true",
                        help="use the suppliers the SAP gateway pushed (get_supplier_balances) instead of a file")
    parser.add_argument("--out", type=Path, default=Path("kreditorlik-1c-sap.xlsx"))
    args = parser.parse_args()
    if args.pull_1c:
        print("1C read:", asyncio.run(pull_onec(args.pull_1c)))
        return
    data = json.loads(args.onec_json.read_text(encoding="utf-8"))
    if args.sap_pushed:
        sap_rows, sap_source = asyncio.run(pushed_sap_rows(close=True))
    else:
        sap_rows = read_table(args.sap) if args.sap else []
        sap_source = args.sap.name if args.sap else "йўқ"
    result = compare(data, sap_rows, sap_source)
    counts = write_workbook(result.pairs, result.onec, result.meta, args.out)
    print("written", args.out, counts)  # counts only, never amounts


if __name__ == "__main__":
    main()

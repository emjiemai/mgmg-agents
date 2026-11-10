"""What the analyst's query_db tool may read, described for the model (2026-11-09, «schema first, then rows»).

One source of truth for three things: the schema block in the analyst's system prompt (``render``), the views the
validator lets through (``allowed_views``), and the database role the query runs as (``role_for``). The views
themselves are in database/schema.sql (schema ``analyst``); selfcheck checks every column listed here exists there.

The block is the same text for the same asker every time (no dates, no figures), so the AI provider can cache it.
"""

from __future__ import annotations

DIRECTOR_ROLE = "mgmg_analyst_director"
SAP_ROLE = "mgmg_analyst_sap"
ROLES = frozenset({DIRECTOR_ROLE, SAP_ROLE})

# view -> (who: "sap" = anyone given SAP and the Director, "director" = the Director only, what it is, columns).
CATALOG: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {
    "sap_sales_docs": ("sap", "SAP sales documents (A/R invoices and credit notes), ONE ROW PER DOCUMENT, dated OR "
                       "entered in SAP in the last 14 days only. Net sales in so'm = SUM(signed_total_som) — the money "
                       "total to use (after document discounts). Older days: sales_month.", (
        ("obj_type", "'13' invoice / '14' credit note"), ("doc_kind", "'invoice' / 'credit_note'"),
        ("sign", "1 / -1 (credit note)"), ("doc_entry", "SAP internal id"), ("doc_num", "the number people see"),
        ("doc_date", "document date"), ("due_date", "payment due"), ("created_on", "date entered into SAP"),
        ("create_ts", "time entered, hhmmss"), ("card_code", "customer code"), ("card_name", "customer"),
        ("name_key", "customer name skeleton for search"), ("slp_code", "sales person code"),
        ("slp_name", "sales person"), ("user_name", "SAP user who entered it"), ("doc_currency", "UZS/USD/EUR"),
        ("total_usd", "total, USD"), ("total_som", "total, so'm"), ("total_doc", "total in doc_currency if foreign"),
        ("signed_total_som", "total so'm, credit notes negative"), ("doc_status", "'open' / 'closed'"),
        ("captured_at", "when SAP sent it"))),
    "sap_sales_lines": ("sap", "SAP sales LINES (what was sold), one row per line, the same 14 days. For pieces "
                        "(SUM(signed_quantity) — returns negative) and splits by item / warehouse. Line money is BEFORE "
                        "a whole-document discount (a cheque of 15 M can have lines of 18.4 M): money totals come from "
                        "sap_sales_docs.signed_total_som or sales_month. NEVER sum doc_total_som (repeats per line).", (
        ("obj_type", "'13'/'14'"), ("doc_kind", "'invoice'/'credit_note'"), ("sign", "1/-1"), ("doc_entry", ""),
        ("doc_num", ""), ("line_num", ""), ("doc_date", ""), ("created_on", "entered into SAP"), ("card_code", ""),
        ("card_name", "customer"), ("name_key", "customer skeleton"), ("slp_code", ""), ("slp_name", "sales person"),
        ("user_name", "SAP user"), ("item_code", ""), ("item_name", "product"), ("item_key", "product skeleton"),
        ("barcode", ""), ("whs_code", "warehouse"), ("quantity", "pieces, positive even on a return"),
        ("signed_quantity", "pieces, returns negative"), ("price", "unit price in line_currency"),
        ("line_currency", ""), ("line_usd", "line, USD, before document discount"),
        ("line_som_gross", "line, so'm with VAT, before document discount"),
        ("line_som_net", "line, so'm without VAT, before document discount"),
        ("signed_line_som_gross", "line so'm with VAT, credit notes negative, before document discount"),
        ("doc_total_som", "WHOLE document's total, repeated on each line"), ("captured_at", ""))),
    "sap_payments_in": ("sap", "Customers' payments received (SAP incoming payments), last ~45 days.", (
        ("doc_entry", ""), ("doc_num", ""), ("doc_date", ""), ("card_code", ""), ("card_name", "payer"),
        ("name_key", ""), ("doc_currency", ""), ("total_usd", "USD"), ("total_som", "so'm"),
        ("total_doc", "in doc_currency"), ("cash_usd", "cash part, USD"), ("transfer_usd", "bank transfer part, USD"),
        ("cheque_usd", "cheque part, USD"), ("card_usd", "card part, USD"), ("comments", ""), ("captured_at", ""))),
    "sap_payments_out": ("sap", "Payments we made to suppliers (SAP outgoing payments), last ~45 days.", (
        ("doc_entry", ""), ("doc_num", ""), ("doc_date", ""), ("card_code", ""), ("card_name", "payee"),
        ("name_key", ""), ("doc_currency", ""), ("total_usd", "USD"), ("total_som", "so'm"),
        ("total_doc", "in doc_currency"), ("cash_usd", ""), ("transfer_usd", ""), ("cheque_usd", ""),
        ("card_usd", ""), ("comments", ""), ("captured_at", ""))),
    "sap_ap_open": ("sap", "Open supplier invoices (what we still owe, by invoice).", (
        ("doc_entry", ""), ("doc_num", ""), ("supplier_ref", "supplier's own number"), ("doc_date", ""),
        ("due_date", ""), ("card_code", ""), ("card_name", "supplier"), ("name_key", ""), ("inn", "tax id"),
        ("doc_currency", "often EUR — never add total_doc across currencies"), ("total_usd", ""), ("paid_usd", ""),
        ("open_usd", "still to pay, USD"), ("total_som", ""), ("paid_som", ""), ("open_som", "still to pay, so'm"),
        ("total_doc", "in doc_currency"), ("paid_doc", "in doc_currency"), ("captured_at", ""))),
    "sap_supplier_balances": ("sap", "Each supplier's balance in SAP. balance_usd < 0 means WE OWE THEM.", (
        ("card_code", ""), ("card_name", "supplier"), ("name_key", ""), ("inn", ""), ("card_type", ""),
        ("currency", "account currency"), ("balance_usd", "USD; negative = we owe"), ("balance_som", "so'm"),
        ("balance_fc", "in currency"), ("we_owe", "true when we owe"), ("captured_at", ""))),
    "sap_customers": ("sap", "Every customer (business partner) with its SAP balance and group.", (
        ("card_code", ""), ("card_name", "customer"), ("name_key", ""), ("inn", ""), ("group_code", ""),
        ("group_name", ""), ("currency", ""), ("balance_usd", "what the customer owes, USD"),
        ("balance_som", "the same, so'm"), ("credit_line", ""), ("slp_code", "sales person code"), ("city", ""),
        ("created_on", ""), ("updated_on", ""), ("active", ""), ("captured_at", ""))),
    "sap_products": ("sap", "Every item (product) with group, totals over all warehouses (faulty ones included — "
                     "for pieces left use sap_stock) and last purchase.", (
        ("item_code", ""), ("item_name", ""), ("item_key", "skeleton for search"), ("group_code", ""),
        ("group_name", "item group"), ("barcode", ""), ("uom", "unit"), ("on_hand", "pieces, all warehouses"),
        ("committed", "reserved for orders"), ("on_order", "ordered from suppliers"), ("min_level", ""),
        ("max_level", ""), ("avg_price_usd", "average cost, USD"), ("last_purchase_price", "currency not sent"),
        ("last_purchase_date", ""), ("inventory_item", ""), ("sellable", ""), ("purchasable", ""), ("active", ""),
        ("is_dummy", "display / dummy unit — leave out unless asked"), ("captured_at", ""))),
    "sap_stock": ("sap", "Stock per item AND warehouse (pieces and value). «X нечта қолди?» → this, WHERE NOT "
                  "is_faulty AND NOT is_dummy AND on_hand > 0 (broken goods and display units aren't stock to sell; "
                  "mention them only if asked).", (
        ("item_code", ""), ("item_name", ""), ("item_key", ""), ("whs_code", ""), ("whs_name", "warehouse"),
        ("on_hand", "pieces"), ("avg_price_usd", ""), ("stock_value_usd", ""), ("is_dummy", "display / dummy unit"),
        ("is_faulty", "a faulty-goods warehouse"), ("captured_at", ""))),
    "sap_stock_by_warehouse": ("sap", "Stock totals per warehouse.", (
        ("whs_code", ""), ("whs_name", ""), ("items", "number of items"), ("on_hand", "pieces"),
        ("stock_value_usd", ""), ("captured_at", ""))),
    "sap_item_sales_90d": ("sap", "Pieces sold per item and warehouse in the last 90 days (credit notes deducted).", (
        ("item_code", ""), ("whs_code", ""), ("days", "90"), ("quantity_sold", "pieces"), ("line_usd", ""),
        ("last_sold", "last sale date"), ("captured_at", ""))),
    "sap_po_open": ("sap", "Open purchase-order LINES (ordered from suppliers, not yet arrived).", (
        ("doc_entry", ""), ("doc_num", ""), ("line_num", ""), ("doc_date", ""), ("due_date", ""), ("ship_date", ""),
        ("card_code", ""), ("card_name", "supplier"), ("name_key", ""), ("item_code", ""), ("item_name", ""),
        ("whs_code", ""), ("quantity", ""), ("open_qty", "still to arrive"), ("price", "in line_currency"),
        ("line_currency", ""), ("line_usd", ""), ("doc_currency", ""), ("captured_at", ""))),
    "sap_orders_open": ("sap", "Open customer sales orders.", (
        ("doc_entry", ""), ("doc_num", ""), ("doc_date", ""), ("due_date", ""), ("card_code", ""),
        ("card_name", "customer"), ("name_key", ""), ("slp_code", ""), ("doc_currency", ""), ("total_usd", ""),
        ("total_som", ""), ("total_doc", ""), ("comments", ""), ("captured_at", ""))),
    "sap_service_calls": ("sap", "Service calls: every open one and those of the last 180 days.", (
        ("call_id", ""), ("subject", ""), ("status", "SAP status code"), ("priority", ""), ("call_type", ""),
        ("problem_type", ""), ("origin", ""), ("created_on", ""), ("closed_on", "empty = still open"),
        ("customer_code", ""), ("customer_name", ""), ("name_key", ""), ("item_code", ""), ("item_name", ""),
        ("serial", ""), ("internal_serial", ""), ("technician_name", ""), ("assignee_name", ""), ("captured_at", ""))),
    "sap_equipment": ("sap", "Customer equipment cards (machines at customers, with serial numbers).", (
        ("ins_id", ""), ("customer_code", ""), ("customer_name", ""), ("name_key", ""), ("item_code", ""),
        ("item_name", ""), ("serial", ""), ("internal_serial", ""), ("status", ""), ("created_on", ""),
        ("captured_at", ""))),
    "sap_service_contracts": ("sap", "Service contracts with their dates.", (
        ("contract_id", ""), ("customer_code", ""), ("customer_name", ""), ("name_key", ""), ("contract_type", ""),
        ("status", ""), ("start_date", ""), ("end_date", ""), ("term_date", ""), ("description", ""),
        ("captured_at", ""))),
    "sap_warehouses": ("sap", "Warehouse codes and names.", (
        ("whs_code", ""), ("whs_name", ""), ("inactive", ""), ("captured_at", ""))),
    "sap_sales_people": ("sap", "Sales people (slp_code → name).", (
        ("slp_code", ""), ("slp_name", ""), ("name_key", ""), ("group_code", ""), ("active", ""), ("captured_at", ""))),
    "ar_open": ("sap", "Open customer invoices (receivables) with age. Totals: SUM(balance_as_written) GROUP BY "
                "written_currency over the rows where it is filled (as on the invoice, lead with it), and the other "
                "rows' SUM(balance_local) in local_currency on its own line — NEVER add the two together.", (
        ("doc_entry", ""), ("doc_num", ""), ("card_code", ""), ("card_name", "customer"), ("name_key", ""),
        ("doc_date", ""), ("due_date", ""), ("days_overdue", ""), ("aging_bucket", "current/1_30/31_60/61_90/90_plus"),
        ("balance_local", "open amount in local_currency"), ("local_currency", ""),
        ("balance_as_written", "open amount as on the invoice"), ("written_currency", ""),
        ("sales_person_name", ""), ("division", ""), ("captured_at", ""))),
    "sales_month": ("sap", "Sales lines kept beyond the 14-day window, signed, one row per line; amounts = the "
                    "document's total (with VAT, after discounts) shared over its lines. Complete from 01.11.2026; "
                    "October only from about 19.10 — say so for earlier dates.", (
        ("day", ""), ("obj_type", ""), ("doc_entry", ""), ("line_num", ""), ("slp_code", ""), ("item_code", ""),
        ("item_group", ""), ("whs_code", ""), ("amount_usd", ""), ("amount_som", ""))),
    "bot_tasks": ("director", "Tasks given through the bot. Same shared_key = one task sent to several people.", (
        ("code", "T-YYYY-MM-NNNN"), ("summary", ""), ("status", "sent/started/blocked/review/done/cancelled"),
        ("awaiting", "'evidence' = done, proof not sent yet"), ("due_date", ""), ("created_on", ""),
        ("completed_on", ""), ("accepted_on", ""), ("assignee", ""), ("assignee_role", ""), ("shared_key", ""),
        ("is_open", ""))),
    "bot_daily_reports": ("director", "Daily reports by person and day (states only, not the text).", (
        ("report_date", ""), ("person", ""), ("role", ""), ("status", "asked = not sent / submitted / excused"),
        ("auto_built", "true = put together by the bot from the chat at 22:00, not sent by the person"),
        ("submitted_at", "Tashkent time"), ("late_decision", ""),
        ("person_active", "false = left the company (past rows kept)"))),
}

RULES = """\
# query_db — SQL OVER THE COMPANY'S DATA (schema first, then rows)
For any SAP question (sales, payments, customer / supplier debt, stock, products, orders, purchase orders,
service) and, for the Director, tasks and report states: write ONE read-only SELECT over the views below and
call query_db. Prefer it to sap_records / sap_receivables / old_stock / company_data — it returns only the rows
you ask for. 1C, Billz, Verifix, Garmin data, report texts and files keep their own tools.
- Aggregate in SQL (SUM, COUNT, GROUP BY, ORDER BY … LIMIT); at most 200 rows come back.
- Names: «Хилтон» / «Hilton» / «Xilton» are one skeleton — filter with
  name_key LIKE '%' || analyst.name_key('Хилтон') || '%'  (products: item_key), or rank close spellings with
  ORDER BY analyst.similarity(name_key, analyst.name_key('…')) DESC LIMIT 5. Several close matches → list them
  and say which you used; never pick one silently.
- Dates: write literal dates from the "Today is" line ('2026-11-08'); view dates are Tashkent dates.
- Money: the unit is in the column name — _usd (SAP's local currency, USD), _som (so'm), _doc (doc_currency).
  Never add _usd and _som, never add _doc across currencies. SAP shows so'm for sales: lead with _som.
- Each view is SAP's latest push; captured_at says when — put it in the source line. A question beyond a view's
  window (sales docs and lines: 14 days; sales_month: complete from 01.11.2026; payments ~45 days): say what the
  data covers, never present a part as the whole.
- A name filter that returns 0 rows: try the similarity ranking before saying there is nothing.
- Rejected or an SQL error: you get the reason and the view's columns — fix it once; if it fails again, use the
  other tools.

# VIEWS (schema analyst)
"""


def allowed_views(scope: set[str] | None) -> frozenset[str]:
    """The views this asker may read: every view for the Director (None), the SAP ones for SAP access."""
    if scope is None:
        return frozenset(CATALOG)
    if "sap" in scope:
        return frozenset(v for v, (who, _, _) in CATALOG.items() if who == "sap")
    return frozenset()


def role_for(scope: set[str] | None) -> str | None:
    """The database role the asker's query runs as (None = no query_db for them)."""
    if scope is None:
        return DIRECTOR_ROLE
    return SAP_ROLE if "sap" in scope else None


def columns(view: str) -> list[str]:
    return [name for name, _ in CATALOG.get(view, ("", "", ()))[2]]


def render(scope: set[str] | None) -> str:
    """The schema block for this asker's system prompt: the rules, then each allowed view and its columns."""
    allowed = allowed_views(scope)
    if not allowed:
        return ""
    lines = [RULES]
    for view, (_, about, cols) in CATALOG.items():
        if view not in allowed:
            continue
        described = ", ".join(f"{name} ({meaning})" if meaning else name for name, meaning in cols)
        lines.append(f"analyst.{view} — {about}\n  {described}")
    return "\n".join(lines)

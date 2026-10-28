# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

"""Map Kras's Bejerman query views: `Saldos al dia` and the sales VAT book."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from odoo.addons.collection_connector_bejerman.service.mapping import (
    _text,
    as_rows,
    debt_state,
    trim_code,
)

DEFAULT_QUOTATION_KINDS = "PRE"


def quotation_kind_set(raw: Any) -> set[str]:
    """Parse comma-separated (or sequence) quotation document-kind codes."""
    if raw in (None, False, ""):
        return set()
    if isinstance(raw, (set, list, tuple)):
        parts: Iterable[Any] = raw
    else:
        parts = str(raw).split(",")
    return {str(part).strip().upper() for part in parts if str(part).strip()}


def document_kind(raw: Any, quotation_kinds: Any = None) -> str:
    """Map FC-style → invoice; configured order/budget → quotation; else invoice."""
    text = str(raw or "").strip().upper()
    if text.startswith("FC"):
        return "invoice"
    if text in quotation_kind_set(quotation_kinds):
        return "quotation"
    return "invoice"


def _date(value: Any) -> str:
    text = _text(value)
    if "T" in text:
        return text.split("T", 1)[0]
    return text


def _amount(value: Any) -> float | None:
    """A finite number, or None when the cell is blank or unreadable.

    Never guesses: an unreadable amount must not become 0.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _count_unparsable(stats: dict[str, int] | None) -> None:
    if stats is not None:
        stats["unparsable_amounts"] = stats.get("unparsable_amounts", 0) + 1


def debt_filter(field: str, erp_code: str) -> list[dict[str, str]]:
    """Query-view filter selecting one customer's rows."""
    return [{"Campo": field, "Valor": erp_code}]


# Real `Saldos al dia` rows: every key is `<generated prefix><alias>`.
_DEBT_ALIASES = {
    "tipo": "CVETCO_COD",
    "punto": "CVE_CODPVT",
    "letra": "CVE_LETRA",
    "numero": "CVE_NRO",
    "cliente": "CVE_CODCLI",
    "vendedor": "CVEVEN_COD",
    "emision": "CVE_FEMISION",
    "vencimiento": "CVE_FVTO",
    "importe": "CVE_IMPMONLOC",
    "saldo": "CVE_SALDOMONLOC",
}
_AMBIGUOUS = object()


# The voucher seller carried by every `Saldos al dia` row, open or not.
_SELLER_ALIASES = {"codigo": "CVEVEN_COD", "nombre": "VEN_DESC"}

# Real sales VAT book rows (`Libro IVA Ventas/<view>`): the internal voucher type
# (`COMP_CODINT`, e.g. FHK) is the debt view's `CVETCO_COD`; `COMP_COD` is not.
_UNTAXED_ALIASES = {
    "tipo": "COMP_CODINT",
    "comprobante": "COMPROBANTE",
    "cliente": "CLIENTE_COD",
    "neto": "NETO",
    "ngex": "NGEX",
    "total": "TOTAL",
}
# How many unmatched invoice ids a refresh keeps for its log line.
_UNMATCHED_SAMPLE = 5
# The sales VAT book view's two variable filters on the issue date. Bejerman has
# no "not applicable" for them over the SDK: both must be sent, each by the name
# Queries shows in its parameters dialog, as DD/MM/YYYY. One `Campo` for both sets
# both to the last value, and the column's internal name is silently ignored.
UNTAXED_DATE_FILTERS = ("Fecha_Emision >=", "Fecha_Emision <=")
UNTAXED_WINDOW_DAYS = 365


def _resolve_debt_keys(
    keys: Iterable[Any], aliases: Mapping[str, str] = _DEBT_ALIASES
) -> dict[str, Any]:
    """Map each alias to the one key ending with it; ambiguous stays unusable."""
    resolved: dict[str, Any] = {}
    for name, alias in aliases.items():
        matches = [key for key in keys if str(key).endswith(alias)]
        if len(matches) == 1:
            resolved[name] = matches[0]
        elif matches:
            resolved[name] = _AMBIGUOUS
        else:
            resolved[name] = None
    return resolved


def _row_cells(
    row: Mapping[str, Any],
    cache: dict[tuple[Any, ...], dict[str, Any]],
    aliases: Mapping[str, str] = _DEBT_ALIASES,
) -> Any:
    """Cell reader for one query row, or None when an alias is ambiguous."""
    shape = tuple(row)
    keys = cache.get(shape)
    if keys is None:
        keys = cache[shape] = _resolve_debt_keys(shape, aliases)
    if _AMBIGUOUS in keys.values():
        return None

    def cell(name: str) -> Any:
        key = keys[name]
        return None if key is None else row.get(key)

    return cell


def _voucher_identity(cell: Any) -> tuple[str, str, str, str, str] | None:
    """(type, point, letter, number, customer code), or None when incomplete."""
    tipo = _text(cell("tipo"))
    punto = _text(cell("punto"))
    letra = _text(cell("letra"))
    numero = _text(cell("numero"))
    customer_code = trim_code(cell("cliente"))
    if not (tipo and numero and customer_code):
        return None
    return tipo, punto, letra, numero, customer_code


def _open_entry(
    cell: Any,
    identity: tuple[str, str, str, str, str],
    quotation_kinds: Any,
    stats: dict[str, int] | None,
) -> dict[str, Any] | None:
    """Open-debt entry of an identified row, or None.

    A settled row (balance <= 0) is skipped without reading its amount. An
    unreadable balance or amount skips the row and is counted in ``stats``.
    """
    balance = _amount(cell("saldo"))
    if balance is None:
        _count_unparsable(stats)
        return None
    if balance <= 0:
        return None
    amount = _amount(cell("importe"))
    if amount is None:
        _count_unparsable(stats)
        return None
    kind = document_kind(identity[0], quotation_kinds)
    return _voucher_entry(cell, identity, amount, balance, kind)


def _voucher_entry(
    cell: Any,
    identity: tuple[str, str, str, str, str],
    amount: float,
    balance: float,
    kind: str,
) -> dict[str, Any]:
    tipo, punto, letra, numero, customer_code = identity
    human_number = "-".join(part for part in (punto, numero) if part)
    return {
        "external_invoice_id": "|".join((tipo, punto, letra, numero)),
        "number": " ".join(part for part in (tipo, letra, human_number) if part),
        "customer_code": customer_code,
        "seller_code": trim_code(cell("vendedor")),
        "issue_date": _date(cell("emision")),
        "due_date": _date(cell("vencimiento")),
        "amount": round(amount, 2),
        "balance": round(balance, 2),
        "document_kind": kind,
    }


def _debt_row_entry(
    row: Mapping[str, Any],
    quotation_kinds: Any,
    cache: dict[tuple[Any, ...], dict[str, Any]],
    stats: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    cell = _row_cells(row, cache)
    if cell is None:
        return None
    identity = _voucher_identity(cell)
    if identity is None:
        return None
    return _open_entry(cell, identity, quotation_kinds, stats)


def map_debt_row(
    row: Mapping[str, Any], quotation_kinds: Any = None
) -> dict[str, Any] | None:
    """Open-debt entry for one real `Saldos al dia` row, or None when not open."""
    return _debt_row_entry(row, quotation_kinds, {})


def map_open_debt(
    payload: Any,
    quotation_kinds: Any = None,
    customer_code: Any = None,
    customer_codes: Iterable[str] | None = None,
    stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Open entries (balance > 0); first id wins.

    ``customer_code`` keeps one customer, ``customer_codes`` keeps only those
    normalized codes (the enabled customers being cached). Rows of the kept
    customers whose balance or amount cannot be read are skipped and counted
    in ``stats["unparsable_amounts"]``.
    """
    wanted = trim_code(customer_code) if customer_code is not None else None
    allowed = None if customer_codes is None else frozenset(customer_codes)
    cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in as_rows(payload):
        cell = _row_cells(row, cache)
        if cell is None:
            continue
        identity = _voucher_identity(cell)
        if identity is None:
            continue
        code = identity[4]
        if wanted is not None and code != wanted:
            continue
        if allowed is not None and code not in allowed:
            continue
        entry = _open_entry(cell, identity, quotation_kinds, stats)
        if entry is None:
            continue
        if entry["external_invoice_id"] in seen:
            continue
        seen.add(entry["external_invoice_id"])
        entries.append(entry)
    return entries


def map_customer_credits(
    payload: Any,
    customer_code: Any = None,
    customer_codes: Iterable[str] | None = None,
) -> dict[str, float]:
    """Open credit per customer code, as a positive amount (decision D4).

    A row with a negative balance (a credit note, an advance) is money the
    customer has in favour; the customer total subtracts it while the invoice
    list stays as is. First id wins, and the customer filters work as in
    ``map_open_debt``.
    """
    wanted = trim_code(customer_code) if customer_code is not None else None
    allowed = None if customer_codes is None else frozenset(customer_codes)
    cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    totals: dict[str, float] = {}
    seen: set[str] = set()
    for row in as_rows(payload):
        cell = _row_cells(row, cache)
        if cell is None:
            continue
        # Unreadable balances are counted by the open-debt pass over the
        # same rows; here they are simply not credits.
        balance = _amount(cell("saldo"))
        if balance is None or balance >= 0:
            continue
        identity = _voucher_identity(cell)
        if identity is None:
            continue
        tipo, punto, letra, numero, code = identity
        if wanted is not None and code != wanted:
            continue
        if allowed is not None and code not in allowed:
            continue
        external_id = "|".join((tipo, punto, letra, numero))
        if external_id in seen:
            continue
        seen.add(external_id)
        totals[code] = totals.get(code, 0.0) - balance
    return {code: round(total, 2) for code, total in totals.items()}


def map_open_credits(
    payload: Any,
    customer_codes: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Open credit notes (balance < 0) as debt-list entries; first id wins.

    Same rows as ``map_customer_credits``, one entry each, with the negative
    balance Bejerman reports and ``document_kind`` ``credit_note``. They are
    listed only: the customer's ``credit_total`` is what nets its debt.
    """
    allowed = None if customer_codes is None else frozenset(customer_codes)
    cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in as_rows(payload):
        cell = _row_cells(row, cache)
        if cell is None:
            continue
        balance = _amount(cell("saldo"))
        if balance is None or balance >= 0:
            continue
        identity = _voucher_identity(cell)
        if identity is None:
            continue
        if allowed is not None and identity[4] not in allowed:
            continue
        # An unreadable amount still lists the credit its balance counts.
        amount = _amount(cell("importe"))
        entry = _voucher_entry(
            cell, identity, balance if amount is None else amount, balance, "credit_note"
        )
        if entry["external_invoice_id"] in seen:
            continue
        seen.add(entry["external_invoice_id"])
        entries.append(entry)
    return entries


def _untaxed_voucher_key(cell: Any) -> tuple[str, str] | None:
    """(debt external invoice id, customer code) of a VAT book row, or None.

    ``COMPROBANTE`` reads ``A 00004-00056383``, or ``00020-00130206`` when the
    voucher has no letter (the debt view's letter is then blank too).
    """
    tipo = _text(cell("tipo"))
    customer_code = trim_code(cell("cliente"))
    parts = _text(cell("comprobante")).split()
    if len(parts) == 2:
        letra, numbers = parts
    elif len(parts) == 1:
        letra, numbers = "", parts[0]
    else:
        return None
    punto, _sep, numero = numbers.rpartition("-")
    if not (tipo and numero and customer_code):
        return None
    return "|".join((tipo, punto, letra, numero)), customer_code


def untaxed_window_start(today: date) -> date:
    """First issue date of the sales VAT book window that ends ``today``."""
    return today - timedelta(days=UNTAXED_WINDOW_DAYS)


def untaxed_date_filter(today: date) -> list[dict[str, str]]:
    """Variable filters of the sales VAT book view: the last 365 days to today."""
    days = (untaxed_window_start(today), today)
    return [
        {"Campo": name, "Valor": day.strftime("%d/%m/%Y")}
        for name, day in zip(UNTAXED_DATE_FILTERS, days, strict=True)
    ]


def map_untaxed_shares(payload: Any) -> dict[tuple[str, str], float]:
    """Untaxed share of each sales VAT book voucher: (NETO + NGEX) / TOTAL.

    Keyed like the debt: (external invoice id, customer code). Rows of one
    voucher add up (one row per VAT rate). A blank NGEX is no exempt amount. A
    voucher with an unreadable NETO or TOTAL, a zero total, or a share outside
    (0, 1] gets no share, so its debt keeps the total.
    """
    cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    sums: dict[tuple[str, str], tuple[float, float]] = {}
    unusable: set[tuple[str, str]] = set()
    for row in as_rows(payload):
        cell = _row_cells(row, cache, _UNTAXED_ALIASES)
        if cell is None:
            continue
        key = _untaxed_voucher_key(cell)
        if key is None:
            continue
        neto = _amount(cell("neto"))
        total = _amount(cell("total"))
        if neto is None or total is None:
            unusable.add(key)
            continue
        untaxed, gross = sums.get(key, (0.0, 0.0))
        sums[key] = (untaxed + neto + (_amount(cell("ngex")) or 0.0), gross + total)
    shares: dict[tuple[str, str], float] = {}
    for key, (untaxed, gross) in sums.items():
        if key in unusable or not gross:
            continue
        share = untaxed / gross
        if 0.0 < share <= 1.0:
            shares[key] = share
    return shares


def apply_untaxed(
    entries: list[dict[str, Any]],
    shares: Mapping[tuple[str, str], float] | None,
    stats: dict[str, Any] | None = None,
    since: date | None = None,
) -> list[dict[str, Any]]:
    """Open-debt entries kept by the sales VAT book, with their untaxed amounts.

    ``amount_untaxed`` and ``balance_untaxed`` are the voucher's untaxed share of
    its amount and of its open balance, so a partly paid voucher's open untaxed
    amount is proportional. When ``shares`` is None (no VAT book view) every entry
    is kept with its total. Otherwise an invoice the VAT book does not list is left
    out, counted in ``stats["untaxed_unmatched"]`` with its id, for the first few,
    in ``stats["untaxed_unmatched_ids"]``. A quotation is untaxed (share 1) and is
    kept only when issued on or after ``since``; older ones are counted in
    ``stats["quotations_outside_window"]``.
    """
    kept = []
    for entry in entries:
        share = 1.0
        if shares is not None and entry["document_kind"] == "quotation":
            if since is not None and entry["issue_date"] < since.isoformat():
                _count(stats, "quotations_outside_window")
                continue
        elif shares is not None:
            found = shares.get((entry["external_invoice_id"], entry["customer_code"]))
            if found is None:
                _count_untaxed_unmatched(stats, entry["external_invoice_id"])
                continue
            share = found
        entry["amount_untaxed"] = round(entry["amount"] * share, 2)
        entry["balance_untaxed"] = round(entry["balance"] * share, 2)
        kept.append(entry)
    return kept


def _count(stats: dict[str, Any] | None, name: str) -> None:
    if stats is not None:
        stats[name] = stats.get(name, 0) + 1


def _count_untaxed_unmatched(stats: dict[str, Any] | None, external_id: str) -> None:
    if stats is None:
        return
    _count(stats, "untaxed_unmatched")
    sample = stats.setdefault("untaxed_unmatched_ids", [])
    if len(sample) < _UNMATCHED_SAMPLE:
        sample.append(external_id)


def map_debt_sellers(payload: Any) -> list[dict[str, str]]:
    """Unique voucher sellers over every debt row, closed rows included.

    Codes are trimmed; the first non-blank name seen for a code wins. A row whose
    code or name alias is ambiguous, or whose code is missing or blank, is skipped.
    """
    cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    sellers: dict[str, str] = {}
    for row in as_rows(payload):
        shape = tuple(row)
        keys = cache.get(shape)
        if keys is None:
            keys = cache[shape] = _resolve_debt_keys(shape, _SELLER_ALIASES)
        if _AMBIGUOUS in keys.values() or keys["codigo"] is None:
            continue
        code = trim_code(row.get(keys["codigo"]))
        if not code:
            continue
        name = "" if keys["nombre"] is None else _text(row.get(keys["nombre"]))
        if not sellers.get(code):
            sellers[code] = name
    return [{"seller_code": code, "name": name} for code, name in sellers.items()]


def entry_to_invoice(
    entry: Mapping[str, Any], today: date | str | None
) -> dict[str, Any]:
    """Sqlite-raw invoice row (collection `_format_customer_debt` input)."""
    return {
        "id": entry["external_invoice_id"],
        "number": entry["number"],
        "issue_date": entry["issue_date"],
        "due_date": entry["due_date"],
        "amount": entry["amount"],
        "balance": entry["balance"],
        "amount_untaxed": entry.get("amount_untaxed", entry["amount"]),
        "state": debt_state(entry["due_date"], today),
        "document_kind": entry["document_kind"],
    }

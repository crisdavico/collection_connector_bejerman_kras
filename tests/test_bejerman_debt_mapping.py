# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).


from odoo.tests import TransactionCase

from .test_bejerman_connector import (
    _DEBT_ROWS,
    _debt_row,
)


class TestBejermanDebtMapping(TransactionCase):
    """Pure mapping of real `Saldos al dia` rows (no SOAP, no database)."""

    def _mapping(self):
        from odoo.addons.collection_connector_bejerman_kras.service import mapping

        return mapping

    def test_debt_sellers_are_unique_trimmed_and_cover_every_row(self):
        """All rows count (closed ones too); codes are trimmed; first name wins."""
        rows = [
            _debt_row(
                "FHK",
                "1",
                "A",
                "1",
                "1",
                "2026-03-01",
                10,
                vendedor="DO ",
                vendedor_nombre="",
            ),
            _debt_row(
                "FHK",
                "1",
                "A",
                "2",
                "1",
                "2026-03-01",
                0,
                vendedor="DO",
                vendedor_nombre="DOMENECH OSCAR",
            ),
            _debt_row(
                "FHK",
                "1",
                "A",
                "3",
                "1",
                "2026-03-01",
                0,
                vendedor="0SA1",
                vendedor_nombre="  SANCHEZ ANA ",
            ),
            _debt_row("FHK", "1", "A", "4", "1", "2026-03-01", 5, vendedor="  "),
            _debt_row("FHK", "1", "A", "5", "1", "2026-03-01", 5, vendedor=None),
        ]
        self.assertEqual(
            self._mapping().map_debt_sellers(rows),
            [
                {"seller_code": "DO", "name": "DOMENECH OSCAR"},
                {"seller_code": "SA1", "name": "SANCHEZ ANA"},
            ],
        )

    def test_debt_sellers_resolve_prefixed_keys_by_alias(self):
        """A generated key prefix never reaches the seller code or name."""
        row = _debt_row(
            "FHK",
            "1",
            "A",
            "1",
            "1",
            "2026-03-01",
            10,
            vendedor="DO",
            vendedor_nombre="DOMENECH OSCAR",
            prefix="GENERATEDPREFIX_",
        )
        sellers = self._mapping().map_debt_sellers([row])
        self.assertEqual(sellers, [{"seller_code": "DO", "name": "DOMENECH OSCAR"}])

    def test_debt_sellers_fail_closed_on_ambiguous_or_missing_aliases(self):
        """Two keys for one alias skip the row; a missing name keeps the code."""
        good = _debt_row("FHK", "1", "A", "1", "1", "2026-03-01", 10, vendedor="OK")
        ambiguous_code = dict(good, OTHERCVEVEN_COD="ZZ")
        ambiguous_name = _debt_row(
            "FHK", "1", "A", "2", "1", "2026-03-01", 10, vendedor="AMB"
        )
        ambiguous_name["OTHERVEN_DESC"] = "other"
        no_code = {k: v for k, v in good.items() if not k.endswith("CVEVEN_COD")}
        no_name = {k: v for k, v in good.items() if not k.endswith("VEN_DESC")}
        no_name = {
            k: ("NN" if k.endswith("CVEVEN_COD") else v) for k, v in no_name.items()
        }
        sellers = self._mapping().map_debt_sellers(
            [ambiguous_code, ambiguous_name, no_code, no_name]
        )
        self.assertEqual(sellers, [{"seller_code": "NN", "name": ""}])

    def test_debt_sellers_reject_a_payload_that_is_not_a_list(self):
        """Same fail-closed rule as every other mapper."""
        with self.assertRaises(TypeError):
            self._mapping().map_debt_sellers({"not": "a list"})
        self.assertEqual(self._mapping().map_debt_sellers(None), [])

    def test_open_row_maps_to_snapshot_entry(self):
        """An open invoice row maps id, number, dates, amounts, and kind."""
        row = _debt_row(
            "FHK",
            "00004",
            "A",
            "00001234",
            "000123",
            "2026-03-01T00:00:00.000Z",
            500,
            imp=1000,
            femision="2026-01-02T00:00:00.000Z",
            vendedor="DN",
        )
        entry = self._mapping().map_debt_row(row, "PRE")
        self.assertEqual(
            entry,
            {
                "external_invoice_id": "FHK|00004|A|00001234",
                "number": "FHK A 00004-00001234",
                "customer_code": "123",
                "seller_code": "DN",
                "issue_date": "2026-01-02",
                "due_date": "2026-03-01",
                "amount": 1000.0,
                "balance": 500.0,
                "document_kind": "invoice",
            },
        )

    def test_blank_padded_parts_are_dropped_cleanly(self):
        """Blank pvt/letra keep the id shape and vanish from the number."""
        mapping = self._mapping()
        bare = mapping.map_debt_row(
            _debt_row("SID", "    ", " ", "00000005", "ZZ0001", "2026-02-15", 80),
            "PRE",
        )
        self.assertEqual(bare["external_invoice_id"], "SID|||00000005")
        self.assertEqual(bare["number"], "SID 00000005")
        no_letter = mapping.map_debt_row(
            _debt_row("PRE", "00004", " ", "00000077", "ZZ0001", "2026-02-15", 5),
            "PRE",
        )
        self.assertEqual(no_letter["external_invoice_id"], "PRE|00004||00000077")
        self.assertEqual(no_letter["number"], "PRE 00004-00000077")
        self.assertEqual(no_letter["customer_code"], "ZZ0001")

    def test_kind_pre_is_quotation_everything_else_invoice(self):
        """Only the configured kinds are quotations."""
        mapping = self._mapping()
        kinds = {}
        for tco in ("PRE", "FHK", "PRS", "ND", "DHK"):
            entry = mapping.map_debt_row(
                _debt_row(tco, "00004", "A", "00000001", "1", "2026-02-15", 1),
                "PRE",
            )
            kinds[tco] = entry["document_kind"]
        self.assertEqual(
            kinds,
            {
                "PRE": "quotation",
                "FHK": "invoice",
                "PRS": "invoice",
                "ND": "invoice",
                "DHK": "invoice",
            },
        )

    def test_non_positive_balance_is_not_open(self):
        """Zero (reconciled) and negative (credit note) balances are dropped."""
        mapping = self._mapping()
        for saldo in (0, -50, -0.01):
            row = _debt_row("FHK", "00004", "A", "00000001", "1", "2026-02-15", saldo)
            self.assertIsNone(mapping.map_debt_row(row, "PRE"))
        positive = _debt_row("FHK", "00004", "A", "00000001", "1", "2026-02-15", 0.01)
        self.assertIsNotNone(mapping.map_debt_row(positive, "PRE"))

    def test_unidentifiable_rows_are_dropped(self):
        """Rows without a voucher type, number, or customer cannot be keyed."""
        mapping = self._mapping()
        for tco, nro, codcli in (
            ("", "00000001", "1"),
            ("FHK", " ", "1"),
            ("FHK", "00000001", " "),
            (None, "00000001", "1"),
        ):
            row = _debt_row(tco, "00004", "A", nro, codcli, "2026-02-15", 10)
            self.assertIsNone(mapping.map_debt_row(row, "PRE"))

    def test_keys_match_by_exact_alias_for_any_prefix(self):
        """The generated prefix is irrelevant; ambiguous aliases fail closed."""
        mapping = self._mapping()
        row = _debt_row(
            "FHK", "00004", "A", "00000001", "1", "2026-02-15", 10, prefix="ANY_PFX_"
        )
        self.assertIsNotNone(mapping.map_debt_row(row, "PRE"))
        ambiguous = dict(row)
        ambiguous["OTHER_PFX_CVE_NRO"] = "00000002"
        self.assertIsNone(mapping.map_debt_row(ambiguous, "PRE"))

    def test_missing_dates_map_to_blank_iso(self):
        """A voucher without dates stays listable with blank ISO dates."""
        entry = self._mapping().map_debt_row(
            _debt_row(
                "FHK",
                "00004",
                "A",
                "00000001",
                "1",
                None,
                10,
                femision=None,
            ),
            "PRE",
        )
        self.assertEqual(entry["issue_date"], "")
        self.assertEqual(entry["due_date"], "")

    def test_map_open_debt_filters_customer_and_dedupes(self):
        """Batch mapping keeps open rows, optionally one customer, first-wins."""
        mapping = self._mapping()
        entries = mapping.map_open_debt(_DEBT_ROWS, "PRE")
        self.assertEqual(len(entries), 4)
        ids = [entry["external_invoice_id"] for entry in entries]
        self.assertEqual(len(ids), len(set(ids)))
        mine = mapping.map_open_debt(_DEBT_ROWS, "PRE", customer_code="123")
        self.assertEqual({entry["customer_code"] for entry in mine}, {"123"})
        self.assertEqual(len(mine), 3)
        duplicated = mapping.map_open_debt(_DEBT_ROWS[:1] + _DEBT_ROWS[:1], "PRE")
        self.assertEqual(len(duplicated), 1)

    def test_map_open_debt_restricts_to_given_customer_codes(self):
        """`customer_codes` keeps only entries of those normalized codes."""
        mapping = self._mapping()
        kept = mapping.map_open_debt(_DEBT_ROWS, "PRE", customer_codes={"124"})
        self.assertEqual({entry["customer_code"] for entry in kept}, {"124"})
        self.assertEqual(
            mapping.map_open_debt(_DEBT_ROWS, "PRE", customer_codes=set()), []
        )
        self.assertEqual(len(mapping.map_open_debt(_DEBT_ROWS, "PRE")), 4)

    def test_map_open_debt_rejects_non_list(self):
        """A non-list payload is a malformed answer, never an empty snapshot."""
        mapping = self._mapping()
        with self.assertRaises(TypeError):
            mapping.map_open_debt("not-a-list", "PRE")
        self.assertEqual(mapping.map_open_debt(None, "PRE"), [])

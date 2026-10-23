# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo.tests import TransactionCase

from odoo.addons.collection_connector_bejerman.tests.common import (
    _CLIENTES,
    _patch_today,
)
from odoo.addons.collection_connector_bejerman_kras.service import mapping

from .test_bejerman_connector import (
    _DEBT_ROWS,
    BejermanTestMixin,
    _debt_row,
    _patch_httpx,
)

_ACME = "20-12345678-9"


class TestBejermanCreditMapping(TransactionCase):
    """Negative-balance rows are the customer's open credits (decision D4)."""

    def test_credits_are_summed_per_customer_as_positive_amounts(self):
        rows = [
            _debt_row("NC", "00004", "A", "00000001", "000123", "2026-04-01", -300),
            _debt_row("NC", "00004", "A", "00000002", "000123", "2026-04-01", -20.5),
            _debt_row("NC", "00004", "A", "00000003", "000124", "2026-04-01", -7),
            _debt_row("FHK", "00004", "A", "00000004", "000123", "2026-04-01", 1000),
            _debt_row("FHK", "00004", "A", "00000005", "000123", "2026-04-01", 0),
        ]
        self.assertEqual(mapping.map_customer_credits(rows), {"123": 320.5, "124": 7.0})

    def test_a_repeated_credit_note_counts_once(self):
        note = _debt_row("NC", "00004", "A", "00000001", "000123", "2026-04-01", -300)
        self.assertEqual(mapping.map_customer_credits([note, note]), {"123": 300.0})

    def test_credits_follow_the_customer_filters(self):
        rows = [
            _debt_row("NC", "00004", "A", "00000001", "000123", "2026-04-01", -300),
            _debt_row("NC", "00004", "A", "00000002", "000125", "2026-04-01", -40),
        ]
        self.assertEqual(
            mapping.map_customer_credits(rows, customer_codes={"123"}),
            {"123": 300.0},
        )
        self.assertEqual(
            mapping.map_customer_credits(rows, customer_code="000125"),
            {"125": 40.0},
        )

    def test_open_debt_still_lists_invoices_only(self):
        """The invoice list is unchanged: allocation targets invoices."""
        ids = {
            entry["external_invoice_id"] for entry in mapping.map_open_debt(_DEBT_ROWS)
        }
        self.assertNotIn("NC|00004|A|00000009", ids)


class TestBejermanCreditSnapshot(BejermanTestMixin, TransactionCase):
    """The snapshot keeps each customer's credit total next to its invoices."""

    def _customer(self, connector, code):
        return self.env["collection.connector.bejerman.customer"].search(
            [("connector_id", "=", connector.id), ("customer_code", "=", code)]
        )

    def test_refresh_stores_the_credit_total_and_keeps_the_invoices(self):
        connector = self._make_connector()
        self._refresh(connector)
        self.assertEqual(self._customer(connector, "123").credit_total, 50.0)
        self.assertEqual(self._customer(connector, "124").credit_total, 0.0)
        self.assertEqual(connector.bejerman_snapshot_debt_count, 4)

    def test_customer_total_is_net_of_open_credit_notes(self):
        """1000 invoice and a 300 credit note: the customer owes 700."""
        connector = self._make_connector()
        pack = {
            "clientes": _CLIENTES,
            "debts": [
                _debt_row(
                    "FHK", "00004", "A", "00000100", "000123", "2026-06-01", 1000
                ),
                _debt_row("NC", "00004", "A", "00000101", "000123", "2026-06-01", -300),
            ],
        }
        self._refresh(connector, payloads_by_url={"*": pack})
        with _patch_today(connector):
            debt = connector._bejerman_get_customer_debt("E", _ACME)
        self.assertEqual(debt["credit_total"], 300.0)
        self.assertEqual([row["balance"] for row in debt["invoices"]], [1000.0])
        summary = connector._format_customer_debt(debt)["summary"]
        self.assertEqual(summary["gross_total"], 1000.0)
        self.assertEqual(summary["credit_total"], 300.0)
        self.assertEqual(summary["net_total"], 700.0)
        self.assertEqual(summary["total_balance"], 700.0)

    def test_a_customer_with_credits_only_has_a_balance_in_favour(self):
        connector = self._make_connector()
        pack = {
            "clientes": _CLIENTES,
            "debts": [
                _debt_row("NC", "00004", "A", "00000101", "000123", "2026-06-01", -300)
            ],
        }
        self._refresh(connector, payloads_by_url={"*": pack})
        debt = connector._bejerman_get_customer_debt("E", _ACME)
        self.assertEqual(debt["invoices"], [])
        summary = connector._format_customer_debt(debt)["summary"]
        self.assertEqual(summary["net_total"], -300.0)

    def test_live_path_nets_credits_too(self):
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        with _patch_httpx(), _patch_today(connector):
            debt = connector._bejerman_get_customer_debt("E", _ACME)
        self.assertEqual(debt["credit_total"], 50.0)
        summary = connector._format_customer_debt(debt)["summary"]
        self.assertEqual(summary["gross_total"], 780.0)
        self.assertEqual(summary["net_total"], 730.0)

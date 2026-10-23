# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo.tests import TransactionCase

from odoo.addons.collection_connector_bejerman.tests.common import (
    _CLIENTES,
    _patch_today,
)
from odoo.addons.collection_connector_bejerman_kras.service import mapping

from .test_bejerman_connector import (
    _DEBT_PREFIX,
    BejermanTestMixin,
    _debt_row,
    _patch_httpx,
)

_CONNECTOR_LOGGER = (
    "odoo.addons.collection_connector_bejerman.models.collection_connector"
)
_SALDO = f"{_DEBT_PREFIX}CVE_SALDOMONLOC"
_IMPORTE = f"{_DEBT_PREFIX}CVE_IMPMONLOC"


def _row(number, saldo=100.0, importe=None, customer="000123"):
    """A real-shaped row whose balance or amount may be unreadable."""
    row = _debt_row("FHK", "00004", "A", number, customer, "2026-06-01", 1)
    row[_SALDO] = saldo
    row[_IMPORTE] = saldo if importe is None else importe
    return row


class TestBejermanAmountParsing(TransactionCase):
    """An unreadable amount skips the row and is counted, never read as 0."""

    def test_unreadable_balances_and_amounts_are_skipped_and_counted(self):
        rows = [
            _row("00000001"),
            _row("00000002", saldo="abc"),
            _row("00000003", saldo="1.234,56"),
            _row("00000004", saldo=None),
            _row("00000005", saldo="nan"),
            _row("00000006", importe="abc"),
            _row("00000007", saldo="250.5"),
        ]
        stats = {}
        entries = mapping.map_open_debt(rows, stats=stats)
        self.assertEqual(
            [entry["external_invoice_id"] for entry in entries],
            ["FHK|00004|A|00000001", "FHK|00004|A|00000007"],
        )
        self.assertEqual(entries[1]["balance"], 250.5)
        self.assertEqual(stats["unparsable_amounts"], 5)

    def test_closed_rows_are_not_counted(self):
        """A settled row (balance 0) is skipped without reading its amount."""
        stats = {}
        entries = mapping.map_open_debt([_row("00000001", 0, "abc")], stats=stats)
        self.assertEqual(entries, [])
        self.assertEqual(stats.get("unparsable_amounts", 0), 0)

    def test_rows_of_other_customers_are_not_counted(self):
        stats = {}
        mapping.map_open_debt(
            [_row("00000001", "abc", customer="000999")],
            customer_codes={"123"},
            stats=stats,
        )
        self.assertEqual(stats.get("unparsable_amounts", 0), 0)

    def test_credits_ignore_unreadable_balances(self):
        rows = [_row("00000001", -30.0), _row("00000002", "-x")]
        self.assertEqual(mapping.map_customer_credits(rows), {"123": 30.0})


class TestBejermanSkippedRows(BejermanTestMixin, TransactionCase):
    """The refresh reports how many rows it skipped; the log names no values."""

    def test_refresh_counts_skipped_rows_on_the_connector(self):
        connector = self._make_connector()
        pack = {
            "clientes": _CLIENTES,
            "debts": [
                _row("00000001"),
                _row("00000002", saldo="abc"),
                _row("00000003", importe="1.234,56"),
            ],
        }
        with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING") as logs:
            self._refresh(connector, payloads_by_url={"*": pack})
        self.assertEqual(connector.bejerman_snapshot_skipped_count, 1 + 1)
        self.assertEqual(connector.bejerman_snapshot_debt_count, 1)
        blob = "\n".join(logs.output)
        self.assertIn("2", blob)
        self.assertNotIn("abc", blob)
        self.assertNotIn("1.234,56", blob)
        self._refresh(connector)
        self.assertEqual(connector.bejerman_snapshot_skipped_count, 0)

    def test_live_path_skips_and_logs_unreadable_rows(self):
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        pack = {
            "clientes": _CLIENTES,
            "debts": [_row("00000001"), _row("00000002", saldo="abc")],
        }
        with (
            _patch_httpx(payloads_by_url={"*": pack}),
            _patch_today(connector),
            self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"),
        ):
            debt = connector._bejerman_get_customer_debt("E", "20-12345678-9")
        self.assertEqual(
            [row["id"] for row in debt["invoices"]], ["FHK|00004|A|00000001"]
        )

    def test_form_shows_the_skipped_count_to_managers(self):
        manager = self._create_user(
            "skipped_manager", "collection.group_collection_manager"
        )
        arch = self._form_arch_for(manager)
        self.assertTrue(arch.xpath("//field[@name='bejerman_snapshot_skipped_count']"))

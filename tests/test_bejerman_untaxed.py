# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import json
from datetime import date

from odoo.exceptions import UserError
from odoo.tests import TransactionCase

from odoo.addons.collection_connector_bejerman.tests.common import (
    _REFRESH_FAILED,
    _SOAPACTION_CIERRE,
    _TODAY,
    _call_body,
    _call_soap_action,
    _parametros_json,
    _patch_today,
)

from .test_bejerman_connector import (
    _DEBT_PREFIX,
    _DEBT_ROWS,
    _UNTAXED_SCENARIO,
    _UNTAXED_VIEW,
    BejermanTestMixin,
    _debt_row,
    _patch_httpx,
    _untaxed_row,
)

_CONNECTOR_LOGGER = (
    "odoo.addons.collection_connector_bejerman.models.collection_connector"
)
_KRAS_LOGGER = (
    "odoo.addons.collection_connector_bejerman_kras.models.collection_connector"
)


def _date_filter(since, until):
    return [
        {"Campo": "Fecha_Emision >=", "Valor": since},
        {"Campo": "Fecha_Emision <=", "Valor": until},
    ]


class TestBejermanUntaxedMapping(TransactionCase):
    """Pure mapping of the sales VAT book view (no SOAP, no database)."""

    def _mapping(self):
        from odoo.addons.collection_connector_bejerman_kras.service import mapping

        return mapping

    def test_untaxed_share_is_neto_plus_ngex_over_total(self):
        """Taxes and perceptions are left out; the key is the debt's voucher."""
        mapping = self._mapping()
        shares = mapping.map_untaxed_shares(
            [_untaxed_row("FHK", "A 00004-00001234", "000123", 700, 100, 1000)]
        )
        self.assertEqual(shares, {("FHK|00004|A|00001234", "123"): 0.8})

    def test_voucher_without_letter_is_read(self):
        """`00020-00130206` has no letter, like the debt row's blank one."""
        mapping = self._mapping()
        shares = mapping.map_untaxed_shares(
            [_untaxed_row("PRS", "00020-00130206", "NG1840", 50, 0, 100)]
        )
        self.assertEqual(shares, {("PRS|00020||00130206", "NG1840"): 0.5})

    def test_unusable_rows_give_no_share(self):
        """A zero total, a share above the total or a blank amount is not used."""
        mapping = self._mapping()
        blank = _untaxed_row("FHK", "A 00004-00000003", "1", 10, 0, 20)
        blank[next(key for key in blank if key.endswith("NETO"))] = None
        rows = [
            _untaxed_row("FHK", "A 00004-00000001", "1", 0, 0, 0),
            _untaxed_row("FHK", "A 00004-00000002", "1", 120, 0, 100),
            blank,
        ]
        self.assertEqual(mapping.map_untaxed_shares(rows), {})

    def test_split_rows_of_one_voucher_add_up(self):
        """Two rows of the same voucher (one per rate) count as one voucher."""
        mapping = self._mapping()
        rows = [
            _untaxed_row("FHK", "A 00004-00000001", "1", 100, 0, 121),
            _untaxed_row("FHK", "A 00004-00000001", "1", 100, 0, 110.5),
        ]
        shares = mapping.map_untaxed_shares(rows)
        self.assertAlmostEqual(shares[("FHK|00004|A|00000001", "1")], 200 / 231.5)

    def test_date_filter_covers_the_last_365_days(self):
        """Both variable filters of the view: 365 days ago and today, DD/MM/YYYY."""
        mapping = self._mapping()
        self.assertEqual(
            mapping.untaxed_date_filter(date(2026, 2, 15)),
            _date_filter("15/02/2025", "15/02/2026"),
        )

    def test_apply_untaxed_keeps_only_listed_invoices(self):
        """Matched invoice: share of amount and balance; unlisted ones are dropped."""
        mapping = self._mapping()
        entries = mapping.map_open_debt(_DEBT_ROWS, "PRE")
        shares = {("FHK|00004|A|00001234", "123"): 0.8}
        stats = {}
        kept = mapping.apply_untaxed(entries, shares, stats, since=date(2025, 2, 15))
        by_id = {entry["external_invoice_id"]: entry for entry in kept}
        self.assertEqual(set(by_id), {"FHK|00004|A|00001234", "PRE|00004||00000077"})
        partial = by_id["FHK|00004|A|00001234"]
        self.assertEqual(partial["amount"], 1000.0)
        self.assertEqual(partial["balance"], 500.0)
        self.assertEqual(partial["amount_untaxed"], 800.0)
        self.assertEqual(partial["balance_untaxed"], 400.0)
        quotation = by_id["PRE|00004||00000077"]
        self.assertEqual(quotation["amount_untaxed"], quotation["amount"])
        self.assertEqual(quotation["balance_untaxed"], quotation["balance"])
        self.assertEqual(stats["untaxed_unmatched"], 2)
        self.assertEqual(
            stats["untaxed_unmatched_ids"],
            ["SID|||00000005", "FHK|00004|A|00001235"],
        )
        self.assertNotIn("quotations_outside_window", stats)

    def test_apply_untaxed_keeps_quotations_of_the_last_365_days(self):
        """A quotation issued on the first day of the window stays; older ones go.

        Every `_DEBT_ROWS` voucher is issued on 2026-01-01.
        """
        mapping = self._mapping()
        shares = {("FHK|00004|A|00001234", "123"): 0.8}
        for since, kept_quotation in (
            (date(2026, 1, 1), True),
            (date(2026, 1, 2), False),
        ):
            with self.subTest(since=since):
                entries = mapping.map_open_debt(_DEBT_ROWS, "PRE")
                stats = {}
                kept = mapping.apply_untaxed(entries, shares, stats, since=since)
                ids = {entry["external_invoice_id"] for entry in kept}
                self.assertEqual("PRE|00004||00000077" in ids, kept_quotation)
                self.assertEqual(
                    stats.get("quotations_outside_window", 0),
                    0 if kept_quotation else 1,
                )

    def test_apply_untaxed_without_the_view_keeps_every_entry(self):
        """No configured view: nothing is dropped, untaxed equals the total."""
        mapping = self._mapping()
        entries = mapping.map_open_debt(_DEBT_ROWS, "PRE")
        stats = {}
        kept = mapping.apply_untaxed(entries, None, stats, since=date(2026, 2, 1))
        self.assertEqual(len(kept), len(entries))
        for entry in kept:
            self.assertEqual(entry["amount_untaxed"], entry["amount"])
            self.assertEqual(entry["balance_untaxed"], entry["balance"])
        self.assertEqual(stats, {})


class TestBejermanUntaxedSnapshot(BejermanTestMixin, TransactionCase):
    """The refresh crosses the debt view with the sales VAT book view."""

    def _untaxed_connector(self, **overrides):
        vals = {
            "bejerman_untaxed_scenario": _UNTAXED_SCENARIO,
            "bejerman_untaxed_view": _UNTAXED_VIEW,
        }
        vals.update(overrides)
        return self._make_connector(**vals)

    def _debts(self, connector):
        return {
            debt.external_invoice_id: debt
            for debt in self.env["collection.connector.bejerman.debt"].search(
                [("connector_id", "=", connector.id)]
            )
        }

    def _refresh_on(self, connector, today=_TODAY, **kwargs):
        with _patch_today(connector, today):
            return self._refresh(connector, **kwargs)

    def test_refresh_keeps_only_invoices_of_both_views(self):
        """The VAT book is read for the last 365 days; unlisted invoices are dropped."""
        connector = self._untaxed_connector()
        with self.assertLogs(_KRAS_LOGGER, level="WARNING") as logs:
            mock_post = self._refresh_on(connector)
        calls = mock_post.call_args_list
        self.assertEqual(len(calls), 5)
        self.assertEqual(_call_soap_action(calls[-1]), _SOAPACTION_CIERRE)
        scenario, view, filters = _parametros_json(_call_body(calls[3]))
        self.assertEqual((scenario, view), (_UNTAXED_SCENARIO, _UNTAXED_VIEW))
        self.assertEqual(json.loads(filters), _date_filter("15/02/2025", "15/02/2026"))
        debts = self._debts(connector)
        self.assertEqual(set(debts), {"FHK|00004|A|00001234", "PRE|00004||00000077"})
        self.assertEqual(connector.bejerman_snapshot_debt_count, 2)
        partial = debts["FHK|00004|A|00001234"]
        self.assertEqual(partial.amount_untaxed, 800.0)
        self.assertEqual(partial.balance_untaxed, 400.0)
        quotation = debts["PRE|00004||00000077"]
        self.assertEqual(quotation.amount_untaxed, 200.0)
        self.assertEqual(quotation.balance_untaxed, 200.0)
        blob = "\n".join(logs.output)
        self.assertIn(
            "2 open invoices are not in the sales VAT book view of the last 365 "
            "days and were left out",
            blob,
        )
        self.assertIn("FHK|00004|A|00001235", blob)

    def test_refresh_logs_skipped_rows_before_the_untaxed_left_out(self):
        """Same log order as before the split: skipped rows, then the VAT book."""
        unreadable = _debt_row(
            "FHK", "00004", "A", "00009999", "000123", "2026-06-01", 1
        )
        unreadable[f"{_DEBT_PREFIX}CVE_SALDOMONLOC"] = "n/a"
        connector = self._untaxed_connector()
        with self.assertLogs("odoo.addons", level="WARNING") as logs:
            self._refresh_on(
                connector, payloads_by_url={"*": {"debts": [*_DEBT_ROWS, unreadable]}}
            )
        skipped = [i for i, line in enumerate(logs.output) if "rows skipped" in line]
        left_out = [i for i, line in enumerate(logs.output) if "left out" in line]
        self.assertEqual(len(skipped), 1)
        self.assertTrue(left_out)
        self.assertLess(skipped[0], min(left_out))

    def test_refresh_leaves_out_quotations_older_than_365_days(self):
        """A year later the window moves: the old quotation is dropped and logged."""
        connector = self._untaxed_connector()
        with self.assertLogs(_KRAS_LOGGER, level="WARNING") as logs:
            mock_post = self._refresh_on(connector, today=date(2027, 1, 20))
        _scenario, _view, filters = _parametros_json(
            _call_body(mock_post.call_args_list[3])
        )
        self.assertEqual(json.loads(filters), _date_filter("20/01/2026", "20/01/2027"))
        self.assertEqual(set(self._debts(connector)), {"FHK|00004|A|00001234"})
        self.assertIn(
            "1 open quotations were issued before 2026-01-20 and were left out",
            "\n".join(logs.output),
        )

    def test_refresh_without_the_view_makes_no_extra_request(self):
        """Not configured: every open debt stays and untaxed is the total, as before."""
        connector = self._make_connector()
        mock_post = self._refresh_on(connector)
        self.assertEqual(len(mock_post.call_args_list), 4)
        debts = self._debts(connector)
        self.assertEqual(
            set(debts),
            {
                "FHK|00004|A|00001234",
                "PRE|00004||00000077",
                "SID|||00000005",
                "FHK|00004|A|00001235",
            },
        )
        for debt in debts.values():
            self.assertEqual(debt.amount_untaxed, debt.amount)
            self.assertEqual(debt.balance_untaxed, debt.balance)

    def test_failed_untaxed_read_keeps_the_previous_snapshot(self):
        """The VAT book read fails like the debt view: nothing is replaced."""
        connector = self._untaxed_connector()
        self._refresh(connector)
        before = {key: debt.id for key, debt in self._debts(connector).items()}
        stamp = connector.bejerman_snapshot_refreshed_at
        with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"):
            with self.assertRaises(UserError) as ctx:
                self._refresh(connector, fail=("untaxed_view",))
        self.assertEqual(str(ctx.exception), _REFRESH_FAILED)
        after = {key: debt.id for key, debt in self._debts(connector).items()}
        self.assertEqual(after, before)
        self.assertEqual(connector.bejerman_snapshot_refreshed_at, stamp)

    def test_half_configured_view_is_refused_without_calling_bejerman(self):
        """A scenario without a view (or the reverse) is a configuration error."""
        for overrides, missing in (
            ({"bejerman_untaxed_view": False}, "Untaxed View"),
            ({"bejerman_untaxed_scenario": "  "}, "Untaxed Scenario"),
        ):
            with self.subTest(missing=missing):
                connector = self._untaxed_connector(**overrides)
                with _patch_httpx() as mock_post:
                    with self.assertRaises(UserError) as ctx:
                        connector._bejerman_refresh_snapshot()
                mock_post.assert_not_called()
                self.assertIn(missing, str(ctx.exception))

    def test_customer_debt_carries_the_untaxed_amount(self):
        """The snapshot hands `amount_untaxed` to `collection`."""
        connector = self._untaxed_connector()
        with self.assertLogs(_KRAS_LOGGER, level="WARNING"):
            self._refresh_on(connector)
        with _patch_today(connector):
            debt = connector._dispatch("get_customer_debt", "E", "20123456789")
        by_id = {row["id"]: row for row in debt["invoices"]}
        self.assertEqual(by_id["FHK|00004|A|00001234"]["amount_untaxed"], 800.0)
        self.assertEqual(by_id["PRE|00004||00000077"]["amount_untaxed"], 200.0)

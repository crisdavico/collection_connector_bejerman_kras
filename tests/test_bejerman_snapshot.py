# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import json
from datetime import date
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.collection_connector_bejerman.tests.common import (
    _CLIENTES,
    _CUSTOMER_KEYS,
    _INVOICE_KEYS,
    _NOT_CONFIGURED,
    _REFRESH_FAILED,
    _SOAPACTION_CIERRE,
    _SOAPACTION_REGISTRO,
    _WS_URL,
    _WS_URL_B,
    _call_body,
    _call_soap_action,
    _parametros_json,
    _patch_today,
)

from .test_bejerman_connector import (
    _DEBT_ROWS,
    _DEBT_VIEW,
    BejermanTestMixin,
    _debt_row,
    _erp_post,
    _patch_httpx,
)

_CONNECTOR_LOGGER = (
    "odoo.addons.collection_connector_bejerman.models.collection_connector"
)
_COLLECTION_USER_GROUP = "collection.group_collection_user"
_SNAPSHOT_MODELS = (
    "collection.connector.bejerman.debt",
    "collection.connector.bejerman.customer",
    "collection.connector.bejerman.seller",
)
_SECRETS = (_WS_URL, "hunter2-bejerman-clave", "SESSIONTOKENDEADBEEFCAFE")
_ONE_ROW_PACK = {
    "clientes": _CLIENTES[:1],
    "debts": [
        _debt_row("FHK", "00004", "A", "00009999", "000123", "2026-06-01", 42),
    ],
}


class TestBejermanSnapshot(BejermanTestMixin, TransactionCase):
    """Debt snapshot: atomic refresh, fail closed, company isolation, ACL."""

    def _debts(self, connector):
        return self.env["collection.connector.bejerman.debt"].search(
            [("connector_id", "=", connector.id)], order="external_invoice_id"
        )

    def _customers(self, connector):
        return self.env["collection.connector.bejerman.customer"].search(
            [("connector_id", "=", connector.id)], order="customer_code"
        )

    def _sellers(self, connector):
        return self.env["collection.connector.bejerman.seller"].search(
            [("connector_id", "=", connector.id)], order="seller_code"
        )

    def test_refresh_replaces_snapshot_in_one_session(self):
        """One Registro, both reads, one Cierre; rows and counters are stored."""
        connector = self._make_connector()
        mock_post = self._refresh(connector)
        actions = [_call_soap_action(call) for call in mock_post.call_args_list]
        self.assertEqual(len(actions), 4)
        self.assertEqual(actions[0], _SOAPACTION_REGISTRO)
        self.assertEqual(actions[-1], _SOAPACTION_CIERRE)
        bodies = [_call_body(call) for call in mock_post.call_args_list]
        self.assertIn("ObtenerClientes", bodies[1])
        self.assertIn("ObtenerJsonQueryVista", bodies[2])

        debts = self._debts(connector)
        self.assertEqual(
            debts.mapped("external_invoice_id"),
            [
                "FHK|00004|A|00001234",
                "FHK|00004|A|00001235",
                "NC|00004|A|00000009",
                "PRE|00004||00000077",
                "SID|||00000005",
            ],
        )
        invoice = debts.filtered(lambda r: r.external_invoice_id.endswith("1234"))
        self.assertEqual(invoice.number, "FHK A 00004-00001234")
        self.assertEqual(invoice.customer_code, "123")
        self.assertEqual(invoice.seller_code, "DN")
        self.assertEqual(invoice.issue_date, date(2026, 1, 1))
        self.assertEqual(invoice.due_date, date(2026, 3, 1))
        self.assertEqual(invoice.amount, 1000.0)
        self.assertEqual(invoice.balance, 500.0)
        self.assertEqual(invoice.document_kind, "invoice")
        quotation = debts.filtered(lambda r: r.external_invoice_id.startswith("PRE"))
        self.assertEqual(quotation.document_kind, "quotation")
        self.assertEqual(invoice.company_id, connector.company_id)

        customers = self._customers(connector)
        self.assertEqual(customers.mapped("customer_code"), ["123", "124"])
        acme = customers[0]
        self.assertEqual(acme.name, "Acme SA")
        self.assertEqual(acme.identification, "20-12345678-9")
        self.assertEqual(acme.seller_code, "E")

        self.assertTrue(connector.bejerman_snapshot_refreshed_at)
        self.assertEqual(connector.bejerman_snapshot_debt_count, 4)
        self.assertEqual(connector.bejerman_snapshot_customer_count, 2)

        sellers = self._sellers(connector)
        self.assertEqual(sellers.mapped("seller_code"), ["DN"])
        self.assertEqual(sellers.name, "SYNTHETIC SELLER")
        self.assertEqual(sellers.company_id, connector.company_id)

    def test_refresh_stores_sellers_of_every_debt_row_in_the_same_download(self):
        """Closed rows count too, and the seller list costs no extra request."""
        connector = self._make_connector()
        closed = _debt_row(
            "FHK",
            "00004",
            "A",
            "00005555",
            "000123",
            "2026-03-01",
            0,
            vendedor="00SA1",
            vendedor_nombre="SANCHEZ ANA",
        )
        pack = {"clientes": _CLIENTES, "debts": [*_DEBT_ROWS, closed]}
        mock_post = self._refresh(connector, payloads_by_url={"*": pack})
        actions = [_call_soap_action(call) for call in mock_post.call_args_list]
        self.assertEqual(len(actions), 4)
        sellers = self._sellers(connector)
        self.assertEqual(sellers.mapped("seller_code"), ["DN", "SA1"])
        self.assertEqual(
            sellers.filtered(lambda r: r.seller_code == "SA1").name, "SANCHEZ ANA"
        )
        self.assertEqual(connector.bejerman_snapshot_debt_count, 4)

    def test_second_refresh_replaces_previous_sellers(self):
        """A seller that left the view disappears from the snapshot."""
        connector = self._make_connector()
        other = _debt_row(
            "FHK",
            "00004",
            "A",
            "00006666",
            "000123",
            "2026-03-01",
            5,
            vendedor="QQ",
            vendedor_nombre="QUIROGA Q",
        )
        pack = {"clientes": _CLIENTES, "debts": [*_DEBT_ROWS, other]}
        self._refresh(connector, payloads_by_url={"*": pack})
        self.assertEqual(self._sellers(connector).mapped("seller_code"), ["DN", "QQ"])
        self._refresh(connector)
        self.assertEqual(self._sellers(connector).mapped("seller_code"), ["DN"])

    def test_second_refresh_replaces_previous_rows(self):
        """A refresh drops reconciled rows and keeps only the new snapshot."""
        connector = self._make_connector()
        self._refresh(connector)
        self._refresh(connector, payloads_by_url={"*": _ONE_ROW_PACK})
        self.assertEqual(
            self._debts(connector).mapped("external_invoice_id"),
            ["FHK|00004|A|00009999"],
        )
        self.assertEqual(self._customers(connector).mapped("customer_code"), ["123"])
        self.assertEqual(connector.bejerman_snapshot_debt_count, 1)
        self.assertEqual(connector.bejerman_snapshot_customer_count, 1)

    def test_empty_open_debt_is_a_valid_snapshot(self):
        """No open rows empties the debt table but still stamps the refresh."""
        connector = self._make_connector()
        self._refresh(connector)
        pack = {"clientes": _CLIENTES, "debts": [_DEBT_ROWS[3]]}
        self._refresh(connector, payloads_by_url={"*": pack})
        self.assertFalse(self._debts(connector))
        self.assertEqual(connector.bejerman_snapshot_debt_count, 0)
        self.assertEqual(connector.bejerman_snapshot_customer_count, 2)

    def test_duplicate_ids_in_the_view_do_not_break_refresh(self):
        """Duplicate composite ids keep the first row instead of failing."""
        connector = self._make_connector()
        pack = {"clientes": _CLIENTES, "debts": _DEBT_ROWS[:1] + _DEBT_ROWS[:1]}
        self._refresh(connector, payloads_by_url={"*": pack})
        self.assertEqual(len(self._debts(connector)), 1)

    def test_debt_of_disabled_or_unknown_customers_is_not_stored(self):
        """Only debt of the enabled, cached customers is kept in the snapshot."""
        connector = self._make_connector()
        disabled_debt = _debt_row(
            "FHK", "00004", "A", "00007777", "000125", "2026-03-01", 70
        )
        unknown_debt = _debt_row(
            "FHK", "00004", "A", "00008888", "999999", "2026-03-01", 80
        )
        pack = {
            "clientes": _CLIENTES,
            "debts": [*_DEBT_ROWS, disabled_debt, unknown_debt],
        }
        self._refresh(connector, payloads_by_url={"*": pack})
        ids = self._debts(connector).mapped("external_invoice_id")
        self.assertNotIn("FHK|00004|A|00007777", ids)
        self.assertNotIn("FHK|00004|A|00008888", ids)
        # The 4 open vouchers and the credit note of customer 123.
        self.assertEqual(len(ids), 5)
        self.assertEqual(connector.bejerman_snapshot_debt_count, 4)
        self.assertEqual(
            set(self._debts(connector).mapped("customer_code")),
            set(self._customers(connector).mapped("customer_code")),
        )

    def test_blank_quotation_kinds_fall_back_to_pre(self):
        """A connector with no configured kinds still treats PRE as quotation."""
        connector = self._make_connector(bejerman_quotation_kinds=False)
        self._refresh(connector)
        kinds = {
            row.external_invoice_id[:3]: row.document_kind
            for row in self._debts(connector)
        }
        self.assertEqual(kinds["PRE"], "quotation")
        self.assertEqual(kinds["FHK"], "invoice")

    def test_refresh_refuses_a_blank_debt_view_without_calling_bejerman(self):
        """A blank debt view is a configuration error, not a failed download."""
        connector = self._make_connector(bejerman_debt_view="  ")
        with _patch_httpx() as mock_post:
            with self.assertRaises(UserError) as ctx:
                connector._bejerman_refresh_snapshot()
        mock_post.assert_not_called()
        self.assertIn("Debt View", str(ctx.exception))

    def test_failed_refresh_keeps_previous_snapshot(self):
        """A failure at any ERP step leaves rows and counters untouched."""
        connector = self._make_connector()
        self._refresh(connector)
        before_ids = self._debts(connector).ids
        before_sellers = self._sellers(connector).ids
        self.assertTrue(before_sellers)
        before_stamp = connector.bejerman_snapshot_refreshed_at
        for fail in (("debt_view",), ("ObtenerClientes",), ("Registro",)):
            with self.subTest(fail=fail):
                with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING") as logs:
                    with self.assertRaises(UserError) as ctx:
                        self._refresh(connector, fail=fail)
                message = str(ctx.exception)
                self.assertIn(
                    message,
                    (_REFRESH_FAILED, "The collection connector is not configured."),
                )
                self._assert_no_leak(message)
                blob = "\n".join(logs.output)
                for secret in _SECRETS:
                    self.assertNotIn(secret, blob)
                self.assertEqual(self._debts(connector).ids, before_ids)
                self.assertEqual(self._sellers(connector).ids, before_sellers)
                self.assertEqual(
                    self._customers(connector).mapped("customer_code"), ["123", "124"]
                )
                self.assertEqual(connector.bejerman_snapshot_refreshed_at, before_stamp)
                self.assertEqual(connector.bejerman_snapshot_debt_count, 4)

    def test_database_failure_rolls_back_the_replacement(self):
        """If writing the new rows fails, the old rows survive and nothing leaks."""
        connector = self._make_connector()
        self._refresh(connector)
        before_ids = self._debts(connector).ids
        debt_model = type(self.env["collection.connector.bejerman.debt"])

        # A plain function, not a MagicMock: the ORM scans model attributes for
        # `_ondelete` markers and a mock answers every attribute name.
        def _boom(_self, _vals_list):
            raise RuntimeError("boom secret-detail " + _WS_URL)

        with patch.object(debt_model, "create", new=_boom):
            with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING") as logs:
                with self.assertRaises(UserError) as ctx:
                    self._refresh(connector, payloads_by_url={"*": _ONE_ROW_PACK})
        self.assertEqual(str(ctx.exception), _REFRESH_FAILED)
        self.assertNotIn("secret-detail", "\n".join(logs.output))
        self.assertNotIn(_WS_URL, "\n".join(logs.output))
        self.assertEqual(self._debts(connector).ids, before_ids)
        self.assertEqual(
            self._customers(connector).mapped("customer_code"), ["123", "124"]
        )

    def test_seller_write_failure_rolls_back_the_whole_replacement(self):
        """If storing sellers fails, debts, customers and sellers all stay."""
        connector = self._make_connector()
        self._refresh(connector)
        before = (
            self._debts(connector).ids,
            self._customers(connector).ids,
            self._sellers(connector).ids,
        )
        seller_model = type(self.env["collection.connector.bejerman.seller"])

        def _boom(_self, _vals_list):
            raise RuntimeError("boom secret-detail " + _WS_URL)

        with patch.object(seller_model, "create", new=_boom):
            with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING") as logs:
                with self.assertRaises(UserError) as ctx:
                    self._refresh(connector, payloads_by_url={"*": _ONE_ROW_PACK})
        self.assertEqual(str(ctx.exception), _REFRESH_FAILED)
        self.assertNotIn("secret-detail", "\n".join(logs.output))
        self.assertEqual(
            (
                self._debts(connector).ids,
                self._customers(connector).ids,
                self._sellers(connector).ids,
            ),
            before,
        )

    def test_unique_seller_code_per_connector(self):
        """The same seller code twice on one connector is refused by the database."""
        connector = self._make_connector()
        other = self._make_connector(name="Second connector")
        Seller = self.env["collection.connector.bejerman.seller"]
        vals = {"connector_id": connector.id, "seller_code": "DO", "name": "D O"}
        Seller.create(vals)
        Seller.create(dict(vals, connector_id=other.id))
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                Seller.create(vals)

    def test_unique_external_invoice_id_per_connector(self):
        """The same id twice on one connector is refused by the database."""
        connector = self._make_connector()
        other = self._make_connector(name="Second connector")
        Debt = self.env["collection.connector.bejerman.debt"]
        vals = {
            "connector_id": connector.id,
            "external_invoice_id": "FHK|00004|A|00000001",
            "number": "FHK A 00004-00000001",
            "customer_code": "1",
            "document_kind": "invoice",
        }
        Debt.create(vals)
        Debt.create(dict(vals, connector_id=other.id))
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                Debt.create(vals)

    def test_snapshot_rows_cascade_with_the_connector(self):
        """Deleting the connector deletes its snapshot rows."""
        connector = self._make_connector()
        self._refresh(connector)
        debts, customers = self._debts(connector), self._customers(connector)
        sellers = self._sellers(connector)
        self.assertTrue(debts and customers and sellers)
        connector.unlink()
        self.assertFalse(debts.exists())
        self.assertFalse(customers.exists())
        self.assertFalse(sellers.exists())

    def test_company_scoping_and_isolation(self):
        """Rows carry the connector company; another company never sees them."""
        connector_a = self._make_connector(name="Bejerman A")
        company_b = self._create_company("Snapshot Co B")
        connector_b = (
            self.env["collection.connector"]
            .with_company(company_b)
            .create(
                self._bejerman_vals(
                    name="Bejerman B",
                    company_id=company_b.id,
                    bejerman_ws_url=_WS_URL_B,
                )
            )
        )
        self._refresh(connector_a)
        self._refresh(connector_b, payloads_by_url={"*": _ONE_ROW_PACK})
        Debt = self.env["collection.connector.bejerman.debt"]
        Seller = self.env["collection.connector.bejerman.seller"]
        for model in (Debt, Seller):
            field = model._fields["company_id"]
            self.assertEqual(field.related, "connector_id.company_id")
            self.assertTrue(field.store)
        seller_reader = Seller.with_user(
            self._create_user("snapshot_seller_reader", _COLLECTION_USER_GROUP)
        )
        self.assertEqual(seller_reader.search([]).connector_id, connector_a)
        internal = self._create_user("snapshot_internal", _COLLECTION_USER_GROUP)
        visible = Debt.with_user(internal).search([])
        self.assertTrue(visible)
        self.assertEqual(visible.connector_id, connector_a)
        self.assertNotIn(connector_b, visible.connector_id)
        self.assertEqual(self._debts(connector_b).company_id, company_b)

    def test_access_rights_read_only_for_collection_users(self):
        """Collections Users (and MCP) read the snapshot; admins write it."""
        connector = self._make_connector()
        self._refresh(connector)
        internal = self._create_user("snapshot_reader", _COLLECTION_USER_GROUP)
        system = self._create_user(
            "snapshot_admin", "base.group_user", "base.group_system"
        )
        for name in _SNAPSHOT_MODELS:
            reader = self.env[name].with_user(internal)
            self.assertTrue(reader.search([("connector_id", "=", connector.id)]))
            reader.check_access("read")
            for operation in ("create", "write", "unlink"):
                with self.assertRaises(AccessError):
                    reader.check_access(operation)
            admin = self.env[name].with_user(system)
            for operation in ("read", "create", "write", "unlink"):
                admin.check_access(operation)
        stamp = connector.with_user(internal).read(["bejerman_snapshot_refreshed_at"])
        self.assertTrue(stamp[0]["bejerman_snapshot_refreshed_at"])

    def test_plain_internal_users_cannot_read_the_snapshot(self):
        """CRM users and seller identities get no ERP customer, debt or seller."""
        connector = self._make_connector()
        self._refresh(connector)
        plain = self._create_user("snapshot_plain", "base.group_user")
        for name in _SNAPSHOT_MODELS:
            denied = self.env[name].with_user(plain)
            for operation in ("read", "create", "write", "unlink"):
                with (
                    self.subTest(model=name, operation=operation),
                    self.assertRaises(AccessError),
                ):
                    denied.check_access(operation)
            with self.subTest(model=name, operation="search"):
                with self.assertRaises(AccessError):
                    denied.search([("connector_id", "=", connector.id)])


class TestBejermanRefreshEntryPoints(BejermanTestMixin, TransactionCase):
    """Cron every 4 hours and the company manager Refresh Debt button."""

    def test_cron_record_runs_every_four_hours(self):
        """The cron is shipped active, every 4 hours, on the connector model."""
        cron = self.env.ref(
            "collection_connector_bejerman.ir_cron_bejerman_refresh_snapshot"
        )
        self.assertEqual(cron.interval_number, 4)
        self.assertEqual(cron.interval_type, "hours")
        self.assertTrue(cron.active)
        self.assertEqual(cron.model_id.model, "collection.connector")
        self.assertEqual(cron.state, "code")
        self.assertIn("_bejerman_cron_refresh_snapshot", cron.code)

    def test_cron_refreshes_active_bejerman_connectors_in_isolation(self):
        """Every active bejerman connector refreshes; one failure stops no other."""
        Connector = self.env["collection.connector"]
        good = self._make_connector(name="Good")
        bad = self._make_connector(name="Bad")
        good_too = self._make_connector(name="Good too")
        self._make_connector(name="Inactive", active=False)
        Connector.create({"name": "Typeless"})
        calls = []

        def _fake_refresh(connector):
            calls.append(connector.name)
            if connector == bad:
                raise RuntimeError("exploded " + _WS_URL)
            return True

        with (
            patch.object(type(good), "_bejerman_refresh_snapshot", new=_fake_refresh),
            self.assertLogs(_CONNECTOR_LOGGER, level="WARNING") as logs,
        ):
            Connector._bejerman_cron_refresh_snapshot()
        self.assertCountEqual(calls, ["Good", "Bad", "Good too"])
        self.assertIn(good_too.name, calls)
        blob = "\n".join(logs.output)
        self.assertNotIn(_WS_URL, blob)
        self.assertNotIn("exploded", blob)

    def test_cron_survives_a_failure_notice_it_cannot_post(self):
        """A notice that cannot be posted (say, its activity type was deleted)
        neither stops the cron nor loses the failure record."""
        Connector = self.env["collection.connector"]
        bad = self._make_connector(name="Bad")
        good = self._make_connector(name="Good")
        calls = []

        def _fake_refresh(connector):
            calls.append(connector.name)
            if connector == bad:
                raise RuntimeError("exploded " + _WS_URL)
            return True

        def _broken_notice(connector, reason):
            raise ValueError("notice exploded " + _WS_URL)

        with (
            patch.object(type(good), "_bejerman_refresh_snapshot", new=_fake_refresh),
            patch.object(
                type(good), "_bejerman_notify_sync_failure", new=_broken_notice
            ),
            self.assertLogs(_CONNECTOR_LOGGER, level="WARNING") as logs,
        ):
            Connector._bejerman_cron_refresh_snapshot()
        self.assertEqual(calls, ["Bad", "Good"])
        self.assertEqual(bad.bejerman_failure_count, 1)
        self.assertEqual(bad.bejerman_last_failure_reason, "other")
        self.assertFalse(good.bejerman_failure_count)
        blob = "\n".join(logs.output)
        self.assertNotIn("exploded", blob)
        self.assertNotIn(_WS_URL, blob)

    def test_cron_failure_keeps_each_previous_snapshot(self):
        """A failing connector keeps its snapshot while a healthy one updates."""
        healthy = self._make_connector(name="Healthy")
        broken = self._make_connector(name="Broken", bejerman_ws_url=_WS_URL_B)
        Debt = self.env["collection.connector.bejerman.debt"]
        with _patch_httpx():
            healthy._bejerman_refresh_snapshot()
            broken._bejerman_refresh_snapshot()
        before = Debt.search([("connector_id", "=", broken.id)]).ids

        def _post(url, content=None, headers=None, **kwargs):
            if url == _WS_URL_B:
                return _erp_post(
                    url, content=content, headers=headers, fail=("Registro",)
                )
            return _erp_post(
                url,
                content=content,
                headers=headers,
                payloads_by_url={"*": _ONE_ROW_PACK},
            )

        with (
            patch(
                "odoo.addons.collection_connector_bejerman.service.session"
                ".httpx.Client.post",
                side_effect=_post,
            ),
            self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"),
        ):
            self.env["collection.connector"]._bejerman_cron_refresh_snapshot()
        self.assertEqual(Debt.search([("connector_id", "=", broken.id)]).ids, before)
        self.assertEqual(
            Debt.search([("connector_id", "=", healthy.id)]).mapped(
                "external_invoice_id"
            ),
            ["FHK|00004|A|00009999"],
        )

    def test_button_refreshes_and_reports_counts(self):
        """The admin button refreshes and answers a success notification."""
        connector = self._make_connector()
        with _patch_httpx():
            action = connector.action_bejerman_refresh_snapshot()
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "success")
        self.assertIn("4", action["params"]["message"])
        self.assertIn("2", action["params"]["message"])
        self.assertEqual(connector.bejerman_snapshot_debt_count, 4)

    def test_button_rejects_regular_users_and_other_types(self):
        """Non-managers are refused; other connector types are unavailable."""
        connector = self._make_connector()
        internal = self._create_user("refresh_internal", _COLLECTION_USER_GROUP)
        with _patch_httpx() as mock_post, self.assertRaises(AccessError):
            connector.with_user(internal).action_bejerman_refresh_snapshot()
        mock_post.assert_not_called()
        other = self.env["collection.connector"].create({"name": "Typeless"})
        with self.assertRaises(UserError) as ctx:
            other.action_bejerman_refresh_snapshot()
        self.assertEqual(
            str(ctx.exception), "The requested connector operation is not available."
        )

    def test_manager_refreshes_from_connector_form(self):
        connector = self._make_connector()
        manager = self._create_user(
            "refresh_manager", "collection.group_collection_manager"
        )
        with _patch_httpx():
            action = connector.with_user(manager).action_bejerman_refresh_snapshot()
        self.assertEqual(action["tag"], "display_notification")
        self.assertTrue(connector.bejerman_snapshot_refreshed_at)
        arch = self._form_arch_for(manager)
        self.assertEqual(
            len(arch.xpath("//button[@name='action_bejerman_refresh_snapshot']")), 1
        )

    def test_form_shows_button_and_snapshot_fields_to_admins_only(self):
        """Refresh button, last refresh and counters are in the admin form only."""
        internal = self._create_user("form_internal", _COLLECTION_USER_GROUP)
        system = self._create_user("form_admin", "base.group_user", "base.group_system")
        admin_arch = self._form_arch_for(system)
        user_arch = self._form_arch_for(internal)
        button = "//button[@name='action_bejerman_refresh_snapshot']"
        self.assertTrue(admin_arch.xpath(button))
        self.assertFalse(user_arch.xpath(button))
        for name in (
            "bejerman_snapshot_refreshed_at",
            "bejerman_snapshot_debt_count",
            "bejerman_snapshot_customer_count",
        ):
            nodes = admin_arch.xpath(f"//field[@name='{name}']")
            self.assertTrue(nodes, name)
            self.assertEqual(nodes[0].get("readonly"), "1", name)
            self.assertFalse(user_arch.xpath(f"//field[@name='{name}']"), name)


class TestBejermanSnapshotReads(BejermanTestMixin, TransactionCase):
    """`list_customers` / `get_customer_debt` read the snapshot, never SOAP."""

    def setUp(self):
        super().setUp()
        self.connector = self._make_connector()

    def test_get_customer_debt_reads_snapshot_without_soap(self):
        """Debt comes from the snapshot; no POST; collection can format it."""
        self._refresh(self.connector)
        with _patch_httpx() as mock_post, _patch_today(self.connector):
            debt = self.connector._bejerman_get_customer_debt("E", "20-12345678-9")
        mock_post.assert_not_called()
        # T25 added `as_of` (the snapshot time); T27 the open `credit_total`.
        self.assertEqual(set(debt), {"customer", "invoices", "as_of", "credit_total"})
        self.assertEqual(debt["credit_total"], 50.0)
        self.assertEqual(set(debt["customer"]), _CUSTOMER_KEYS)
        self.assertEqual(debt["customer"]["id"], "123")
        self.assertEqual(debt["customer"]["name"], "Acme SA")
        self.assertEqual(
            [row["id"] for row in debt["invoices"]],
            ["PRE|00004||00000077", "SID|||00000005", "FHK|00004|A|00001234"],
        )
        for row in debt["invoices"]:
            self.assertEqual(set(row), _INVOICE_KEYS)
        states = {row["id"]: row["state"] for row in debt["invoices"]}
        self.assertEqual(
            states,
            {
                "PRE|00004||00000077": "overdue",
                "SID|||00000005": "open",
                "FHK|00004|A|00001234": "open",
            },
        )
        formatted = self.connector._format_customer_debt(debt)
        # Decision D4: the 50 credit note nets the customer total (780 gross).
        self.assertEqual(formatted["summary"]["total_balance"], 730.0)
        self.assertEqual(formatted["summary"]["gross_total"], 780.0)
        self.assertEqual(formatted["summary"]["overdue_balance"], 200.0)
        self.assertEqual(formatted["summary"]["open_balance"], 580.0)
        self.assertEqual(formatted["summary"]["invoice_count"], 3)
        self.assertEqual(
            formatted["invoices"][0]["external_invoice_id"], "PRE|00004||00000077"
        )

    def test_state_is_computed_at_read_time(self):
        """The same snapshot is open or overdue depending on today's date."""
        self._refresh(self.connector)
        with _patch_today(self.connector, date(2026, 1, 1)):
            early = self.connector._bejerman_get_customer_debt("E", "20-12345678-9")
        with _patch_today(self.connector, date(2026, 12, 31)):
            late = self.connector._bejerman_get_customer_debt("E", "20-12345678-9")
        self.assertEqual({row["state"] for row in early["invoices"]}, {"open"})
        self.assertEqual({row["state"] for row in late["invoices"]}, {"overdue"})

    def test_list_customers_reads_snapshot_seller_scoped(self):
        """Only the seller's active customers, canonical CUIT, no POST."""
        self._refresh(self.connector)
        with _patch_httpx() as mock_post:
            rows = self.connector._bejerman_list_customers("E")
            padded = self.connector._bejerman_list_customers("000E")
        mock_post.assert_not_called()
        self.assertEqual(
            rows,
            [
                {
                    "id": "123",
                    "name": "Acme SA",
                    "identification": "20-12345678-9",
                    "customer_code": "123",
                    "active": True,
                }
            ],
        )
        self.assertEqual(padded, rows)
        self.assertEqual(self.connector._bejerman_list_customers("ZZ"), [])

    def test_unknown_other_seller_or_inactive_customer_is_empty(self):
        """No snapshot customer for that seller and CUIT -> sqlite empty shape."""
        self._refresh(self.connector)
        empty = {"customer": None, "invoices": []}
        for ident in ("20999999991", "27987654321", "23000000009"):
            self.assertEqual(
                self.connector._bejerman_get_customer_debt("E", ident), empty
            )

    def test_customer_without_open_debt_has_empty_invoices(self):
        """A known customer with no open rows keeps the customer and no invoices."""
        pack = {"clientes": _CLIENTES, "debts": []}
        self._refresh(self.connector, payloads_by_url={"*": pack})
        debt = self.connector._bejerman_get_customer_debt("E", "20123456789")
        self.assertEqual(debt["customer"]["id"], "123")
        self.assertEqual(debt["invoices"], [])

    def test_no_snapshot_fails_closed_without_download(self):
        """Before the first refresh both reads raise the generic error, no POST."""
        with _patch_httpx() as mock_post:
            for call in (
                lambda: self.connector._bejerman_list_customers("E"),
                lambda: self.connector._bejerman_get_customer_debt("E", "20123456789"),
            ):
                with self.assertRaises(UserError) as ctx:
                    call()
                self.assertEqual(str(ctx.exception), _NOT_CONFIGURED)
                self._assert_no_leak(str(ctx.exception))
        mock_post.assert_not_called()

    def test_internal_user_reads_snapshot_without_credentials(self):
        """A non-admin caller reads the snapshot; credentials stay unreadable."""
        self._refresh(self.connector)
        internal = self._create_user("snapshot_caller", _COLLECTION_USER_GROUP)
        as_user = self.connector.with_user(internal)
        with _patch_today(self.connector):
            debt = as_user._bejerman_get_customer_debt("E", "20-12345678-9")
        self.assertEqual(len(debt["invoices"]), 3)
        self.assertEqual(as_user._bejerman_list_customers("E")[0]["id"], "123")
        with self.assertRaises(AccessError):
            as_user.read(["bejerman_clave"])

    def test_live_path_filters_the_debt_view_by_customer(self):
        """With a filter field set, one customer's debt is read live, filtered."""
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        with _patch_httpx() as mock_post, _patch_today(connector):
            debt = connector._bejerman_get_customer_debt("E", "20-12345678-9")
        bodies = [_call_body(call) for call in mock_post.call_args_list]
        debt_body = next(body for body in bodies if _DEBT_VIEW in body)
        params = _parametros_json(debt_body)
        self.assertEqual(
            json.loads(params[2]), [{"Campo": "CVE_CODCLI", "Valor": "000123"}]
        )
        self.assertEqual(
            sorted(row["id"] for row in debt["invoices"]),
            [
                "FHK|00004|A|00001234",
                "PRE|00004||00000077",
                "SID|||00000005",
            ],
        )
        self.assertEqual(debt["customer"]["id"], "123")

    def test_live_path_needs_no_snapshot_and_skips_view_for_unknown(self):
        """An unknown customer never triggers the debt view; no snapshot needed."""
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        with _patch_httpx() as mock_post:
            debt = connector._bejerman_get_customer_debt("E", "20999999991")
        self.assertEqual(debt, {"customer": None, "invoices": []})
        bodies = [_call_body(call) for call in mock_post.call_args_list]
        self.assertFalse(any(_DEBT_VIEW in body for body in bodies))
        self.assertEqual(len(bodies), 3)

    def test_live_path_never_returns_debt_of_a_disabled_customer(self):
        """A disabled customer is unknown live too; its debt view is never read."""
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        disabled_debt = _debt_row(
            "FHK", "00004", "A", "00007777", "000125", "2026-03-01", 70
        )
        pack = {"clientes": _CLIENTES, "debts": [*_DEBT_ROWS, disabled_debt]}
        with _patch_httpx(payloads_by_url={"*": pack}) as mock_post:
            debt = connector._bejerman_get_customer_debt("E", "23000000009")
        self.assertEqual(debt, {"customer": None, "invoices": []})
        bodies = [_call_body(call) for call in mock_post.call_args_list]
        self.assertFalse(any(_DEBT_VIEW in body for body in bodies))

    def test_live_path_works_for_internal_callers(self):
        """The live read uses stored credentials on behalf of a non-admin caller."""
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        internal = self._create_user("live_caller", _COLLECTION_USER_GROUP)
        with _patch_httpx(), _patch_today(connector):
            debt = connector.with_user(internal)._bejerman_get_customer_debt(
                "E", "20-12345678-9"
            )
        self.assertEqual(len(debt["invoices"]), 3)
        with self.assertRaises(AccessError):
            connector.with_user(internal).read(["bejerman_debt_filter_field"])

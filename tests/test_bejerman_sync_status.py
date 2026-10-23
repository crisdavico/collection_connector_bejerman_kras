# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from datetime import datetime, timedelta
from unittest.mock import patch

import httpx

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Command
from odoo.tests import TransactionCase

from odoo.addons.collection.models.collection_common import (
    ConnectorDataStaleError,
    privileged,
)
from odoo.addons.collection_connector_bejerman.tests.common import (
    _CLAVE,
    _SESSION_TOKEN,
    _WS_URL,
    _patch_today,
)

from .test_bejerman_connector import (
    BejermanTestMixin,
    _patch_httpx,
)

_CONNECTOR_LOGGER = (
    "odoo.addons.collection_connector_bejerman.models.collection_connector"
)
_SESSION_POST = (
    "odoo.addons.collection_connector_bejerman.service.session.httpx.Client.post"
)
_SYNC_ACTIVITY = "collection_connector_bejerman.mail_activity_type_bejerman_sync_failed"
_SNAPSHOT_SCOPE = "collection_connector_bejerman_snapshot"
_STALE_MESSAGE = (
    "The customer debt is out of date: the last Bejerman update is older than "
    "24 hours. Ask a collections manager to check the Bejerman connection."
)
_MANAGER_GROUP = "collection.group_collection_manager"
_USER_GROUP = "collection.group_collection_user"


class TestBejermanSyncStatus(BejermanTestMixin, TransactionCase):
    """Cron failures are recorded, managers are told once, stale debt is refused."""

    def setUp(self):
        super().setUp()
        self.connector = self._make_connector()
        self.manager = self._create_user("sync_manager", _MANAGER_GROUP)
        self.operator = self._create_user("sync_operator", _USER_GROUP)
        company_b = self._create_company("Sync Co B")
        self.manager_b = self._create_user("sync_manager_b", _MANAGER_GROUP)
        self.manager_b.write(
            {"company_ids": [Command.set(company_b.ids)], "company_id": company_b.id}
        )

    def _cron(self, **kwargs):
        with _patch_httpx(**kwargs):
            self.env["collection.connector"]._bejerman_cron_refresh_snapshot()

    def _notices(self, connector=None):
        connector = connector or self.connector
        activity_type = self.env.ref(_SYNC_ACTIVITY)
        return (
            self.env["mail.activity"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "collection.connector"),
                    ("res_id", "=", connector.id),
                    ("activity_type_id", "=", activity_type.id),
                ]
            )
        )

    def _age_snapshot(self, hours):
        stamp = fields.Datetime.now() - timedelta(hours=hours)
        with privileged(_SNAPSHOT_SCOPE):
            self.connector.write(
                {
                    "bejerman_snapshot_started_at": stamp,
                    "bejerman_snapshot_refreshed_at": stamp,
                }
            )

    def test_cron_failure_records_the_reason_and_tells_each_manager_once(self):
        """One notice per company manager; a second failure adds none."""
        self._refresh(self.connector)
        with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"):
            self._cron(fail=("Registro",))
        self.assertTrue(self.connector.bejerman_last_failure_at)
        self.assertEqual(self.connector.bejerman_last_failure_reason, "erp")
        self.assertEqual(self.connector.bejerman_failure_count, 1)
        notices = self._notices()
        self.assertIn(self.manager, notices.user_id)
        self.assertNotIn(self.operator, notices.user_id)
        self.assertNotIn(self.manager_b, notices.user_id)
        self.assertEqual(len(notices), len(notices.user_id))
        for notice in notices:
            self._assert_no_leak(str(notice.note or ""))
            self._assert_no_leak(str(notice.summary or ""))

        with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"):
            self._cron(fail=("Registro",))
        self.assertEqual(self.connector.bejerman_failure_count, 2)
        self.assertEqual(self._notices(), notices)

    def test_success_clears_the_notice_and_keeps_the_last_failure(self):
        """The next good refresh closes the notice and resets the counter."""
        with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"):
            self._cron(fail=("debt_view",))
        failed_at = self.connector.bejerman_last_failure_at
        self.assertTrue(self._notices())
        self._cron()
        self.assertFalse(self._notices())
        self.assertEqual(self.connector.bejerman_failure_count, 0)
        self.assertEqual(self.connector.bejerman_last_failure_at, failed_at)
        self.assertTrue(self.connector.bejerman_snapshot_started_at)
        self.assertLessEqual(
            self.connector.bejerman_snapshot_started_at,
            self.connector.bejerman_snapshot_refreshed_at,
        )
        with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"):
            self._cron(fail=("debt_view",))
        self.assertTrue(self._notices(), "a new outage opens a new notice")

    def test_failure_reasons_are_generic_classes(self):
        """The stored reason names a class of problem, never ERP text."""
        cases = {
            "erp": {"fail": ("ObtenerClientes",)},
            "data": {"payloads_by_url": {"*": {"debts": {"not": "a list"}}}},
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"):
                    self._cron(**kwargs)
                self.assertEqual(self.connector.bejerman_last_failure_reason, reason)

        def _boom(_url, content=None, headers=None, **_kwargs):
            raise httpx.ConnectError(f"connect {_WS_URL} clave={_CLAVE}")

        with (
            patch(_SESSION_POST, side_effect=_boom),
            self._no_sleep(),
            self.assertLogs(_CONNECTOR_LOGGER, level="WARNING") as logs,
        ):
            self.env["collection.connector"]._bejerman_cron_refresh_snapshot()
        self.assertEqual(self.connector.bejerman_last_failure_reason, "connection")
        blob = "\n".join(logs.output)
        for secret in (_WS_URL, _CLAVE, _SESSION_TOKEN):
            self.assertNotIn(secret, blob)

        debt_model = type(self.env["collection.connector.bejerman.debt"])

        def _db_boom(_self, _vals_list):
            raise RuntimeError("boom " + _WS_URL)

        with (
            patch.object(debt_model, "create", new=_db_boom),
            self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"),
        ):
            self._cron()
        self.assertEqual(self.connector.bejerman_last_failure_reason, "storage")

        self.connector.bejerman_ws_url = False
        with self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"):
            self._cron()
        self.assertEqual(self.connector.bejerman_last_failure_reason, "configuration")

    def _no_sleep(self):
        """Retried transport errors must not sleep in tests."""
        return patch("odoo.addons.collection_connector_bejerman.service.session._sleep")

    def test_stale_snapshot_is_refused_with_a_generic_error(self):
        """Debt older than the threshold is refused, not served."""
        self._refresh(self.connector)
        self._age_snapshot(25)
        for call in (
            lambda: self.connector._bejerman_get_customer_debt("E", "20123456789"),
            lambda: self.connector._dispatch("get_customer_debt", "E", "20-12345678-9"),
        ):
            with self.assertRaises(ConnectorDataStaleError) as ctx:
                call()
            self.assertEqual(str(ctx.exception), _STALE_MESSAGE)
            self._assert_no_leak(str(ctx.exception))

    def test_threshold_is_a_manager_setting(self):
        """Managers raise the threshold; then the same snapshot is served."""
        self._refresh(self.connector)
        self._age_snapshot(25)
        self.connector.with_user(self.manager).write(
            {"bejerman_snapshot_max_age_hours": 48}
        )
        with _patch_today(self.connector):
            debt = self.connector._bejerman_get_customer_debt("E", "20123456789")
        self.assertEqual(len(debt["invoices"]), 3)
        with self.assertRaises(ValidationError):
            self.connector.write({"bejerman_snapshot_max_age_hours": 0})
        with self.assertRaises(AccessError):
            self.connector.with_user(self.operator).write(
                {"bejerman_snapshot_max_age_hours": 72}
            )

    def test_default_threshold_is_one_day(self):
        self.assertEqual(self.connector.bejerman_snapshot_max_age_hours, 24)

    def test_fresh_read_carries_the_snapshot_time(self):
        """A fresh snapshot read says when it was taken, in the company timezone."""
        self._refresh(self.connector)
        stamp = datetime(2026, 10, 1, 12, 0, 0)
        with privileged(_SNAPSHOT_SCOPE):
            self.connector.write({"bejerman_snapshot_refreshed_at": stamp})
        self.connector.company_id.partner_id.tz = "America/Argentina/Buenos_Aires"
        with patch.object(
            fields.Datetime, "now", return_value=stamp + timedelta(hours=1)
        ):
            debt = self.connector._bejerman_get_customer_debt("E", "20123456789")
        self.assertEqual(debt["as_of"], fields.Datetime.to_string(stamp))
        formatted = self.connector._format_customer_debt(debt)
        self.assertEqual(formatted["as_of"], "2026-10-01T09:00:00-03:00")

    def test_live_path_has_no_age_limit_and_reads_now(self):
        """The live filter path reads the ERP itself: no snapshot, no age check."""
        live = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        before = fields.Datetime.now().replace(microsecond=0)
        with _patch_httpx(), _patch_today(live):
            debt = live._bejerman_get_customer_debt("E", "20-12345678-9")
        self.assertGreaterEqual(fields.Datetime.to_datetime(debt["as_of"]), before)
        self.assertEqual(len(debt["invoices"]), 3)

    def test_snapshot_bookkeeping_cannot_be_written_from_outside(self):
        """Nobody can make a stale snapshot look fresh with a plain write."""
        self._refresh(self.connector)
        for user in (self.manager, self.env.user):
            for name in (
                "bejerman_snapshot_refreshed_at",
                "bejerman_snapshot_started_at",
                "bejerman_failure_count",
            ):
                with self.subTest(user=user.login, field=name):
                    with self.assertRaises(UserError):
                        self.connector.with_user(user).write(
                            {name: fields.Datetime.now() if "_at" in name else 0}
                        )

    def test_form_shows_the_sync_status_to_managers_only(self):
        """Managers and admins see the sync status; operators do not."""
        names = (
            "bejerman_snapshot_started_at",
            "bejerman_snapshot_refreshed_at",
            "bejerman_last_failure_at",
            "bejerman_last_failure_reason",
            "bejerman_failure_count",
            "bejerman_snapshot_max_age_hours",
        )
        manager_arch = self._form_arch_for(self.manager)
        operator_arch = self._form_arch_for(self.operator)
        for name in names:
            with self.subTest(field=name):
                self.assertTrue(manager_arch.xpath(f"//field[@name='{name}']"))
                self.assertFalse(operator_arch.xpath(f"//field[@name='{name}']"))

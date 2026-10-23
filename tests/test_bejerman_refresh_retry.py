# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from unittest.mock import MagicMock, patch

import httpx

from odoo.tests import TransactionCase

from odoo.addons.collection_connector_bejerman.tests.common import (
    _CIERRE_OK,
    _CLIENTES,
    _REGISTRO_OK,
    _SOAPACTION_CIERRE,
    _SOAPACTION_EJECUTAR,
    _SOAPACTION_REGISTRO,
    _call_soap_action,
    _ejecutar_ok,
    _xml_response,
)
from odoo.addons.collection_connector_bejerman.tests.test_bejerman_session_retry import (  # noqa: E501
    _Clock,
    _ok_session,
    _status_response,
)

from .test_bejerman_connector import (
    BejermanTestMixin,
)

_SESSION = "odoo.addons.collection_connector_bejerman.service.session"
_CONNECTOR_LOGGER = (
    "odoo.addons.collection_connector_bejerman.models.collection_connector"
)


class TestBejermanRetryInTheRefresh(BejermanTestMixin, TransactionCase):
    """The connector refresh retries too, and records an exhausted one (T25)."""

    def test_refresh_survives_one_transient_error(self):
        connector = self._make_connector()
        script = _ok_session(
            Ejecutar=[
                _status_response(503),
                _xml_response(_ejecutar_ok(_CLIENTES)),
                _xml_response(_ejecutar_ok("[]")),
            ]
        )
        with (
            patch(f"{_SESSION}.httpx.Client.post", side_effect=script),
            patch(f"{_SESSION}._sleep") as sleep,
        ):
            connector._bejerman_refresh_snapshot()
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(connector.bejerman_snapshot_customer_count, 2)

    def test_exhausted_retries_are_recorded_as_a_connection_failure(self):
        connector = self._make_connector()
        script = _ok_session(Ejecutar=[httpx.ConnectError("refused")])
        with (
            patch(f"{_SESSION}.httpx.Client.post", side_effect=script),
            patch(f"{_SESSION}._sleep"),
            self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"),
        ):
            self.env["collection.connector"]._bejerman_cron_refresh_snapshot()
        self.assertEqual(connector.bejerman_last_failure_reason, "connection")
        self.assertEqual(connector.bejerman_failure_count, 1)

    def test_deadline_is_recorded_as_a_timeout(self):
        connector = self._make_connector(bejerman_deadline=30)
        clock = _Clock()

        def _slow(url, content=None, headers=None, **kwargs):
            clock.now += 20.0
            return _erp_ok(headers)

        with (
            patch(f"{_SESSION}.httpx.Client.post", side_effect=_slow),
            patch(f"{_SESSION}._clock", new=clock),
            self.assertLogs(_CONNECTOR_LOGGER, level="WARNING"),
        ):
            self.env["collection.connector"]._bejerman_cron_refresh_snapshot()
        self.assertEqual(connector.bejerman_last_failure_reason, "timeout")

    def test_deadline_setting_is_admin_only_and_defaults_to_fifteen_minutes(self):
        connector = self._make_connector()
        self.assertEqual(connector.bejerman_deadline, 900)
        field = connector._fields["bejerman_deadline"]
        self.assertEqual(field.groups, "base.group_system")
        self.assertEqual(connector._bejerman_build_settings().deadline, 900.0)


def _erp_ok(headers):
    action = _call_soap_action(MagicMock(kwargs={"headers": headers}))
    if action == _SOAPACTION_REGISTRO:
        return _xml_response(_REGISTRO_OK)
    if action == _SOAPACTION_CIERRE:
        return _xml_response(_CIERRE_OK)
    assert action == _SOAPACTION_EJECUTAR
    return _xml_response(_ejecutar_ok(_CLIENTES))

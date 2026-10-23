# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

"""Kras's query views on the base fake ERP, and the connector tests."""

import json
from unittest.mock import patch

import httpx
from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.modules.module import load_manifest
from odoo.tests import TransactionCase

from odoo.addons.collection_connector_bejerman.tests import common
from odoo.addons.collection_connector_bejerman.tests.common import (
    _CIERRE_OK,
    _CLAVE,
    _CLIENTES,
    _CUSTOMER_KEYS,
    _EJECUTAR_ERROR,
    _ERROR_MSG,
    _INVOICE_KEYS,
    _REFRESH_FAILED,
    _REGISTRO_OK,
    _SELLER_KEYS,
    _SESSION_TOKEN,
    _SOAPACTION_CIERRE,
    _SOAPACTION_EJECUTAR,
    _SOAPACTION_REGISTRO,
    _UNAVAILABLE,
    _USUARIO,
    _WS_URL,
    _WS_URL_B,
    _call_body,
    _call_soap_action,
    _ejecutar_ok,
    _patch_today,
    _xml_response,
)

_SELLER_SCENARIO = "VENDEDORES"
_SELLER_VIEW = "Vendedores"
_DEBT_SCENARIO = "CTACTE"
_DEBT_VIEW = "Saldos"
_UNTAXED_SCENARIO = "IVAVENTAS"
_UNTAXED_VIEW = "Libro IVA Ventas/IA CRIS"
_QUERY_PREFIX = "GENERATEDPREFIX"
_BEJERMAN_CONFIG_FIELDS = common._BEJERMAN_CONFIG_FIELDS + (
    "bejerman_debt_scenario",
    "bejerman_debt_view",
    "bejerman_debt_filter_field",
    "bejerman_untaxed_scenario",
    "bejerman_untaxed_view",
    "bejerman_quotation_kinds",
)
_LEAK_TOKENS = common._LEAK_TOKENS + (
    _SELLER_VIEW,
    _DEBT_VIEW,
    _SELLER_SCENARIO,
    _DEBT_SCENARIO,
)
_DEBT_PREFIX = "STA_COMPROBANTESVENTA_001"


def _debt_row(
    tco,
    pvt,
    letra,
    nro,
    codcli,
    fvto,
    saldo,
    imp=None,
    femision="2026-01-01T00:00:00.000Z",
    vendedor="DN",
    prefix=_DEBT_PREFIX,
    vendedor_nombre="SYNTHETIC SELLER",
):
    """Return a row shaped like the real `Saldos al dia` view."""
    imp = saldo if imp is None else imp
    return {
        f"{prefix}CVE_FEMISION": femision,
        f"{prefix}CVE_FCONTAB": femision,
        f"{prefix}CVE_FVTO": fvto,
        f"{prefix}CVETCO_COD": tco,
        f"{prefix}CVE_CODPVT": pvt,
        f"{prefix}CVE_LETRA": letra,
        f"{prefix}CVE_NRO": nro,
        f"{prefix}CVE_CODCLI": codcli,
        f"{prefix}CVECLI_RAZSOC": "SYNTHETIC CUSTOMER",
        f"{prefix}CVEVEN_COD": vendedor,
        f"{prefix}VEN_DESC": vendedor_nombre,
        f"{prefix}CVECVT_COD": None,
        f"{prefix}CVT_DESC": None,
        f"{prefix}CVEMON_CODIGO": "1",
        f"{prefix}MON_DESCRIP": "Pesos",
        f"{prefix}MON_SIMBOLO": "$",
        f"{prefix}CVE_IMPMONLOC": float(imp),
        f"{prefix}CVE_IMPMONCC": float(imp),
        f"{prefix}CVE_IMPMONEMIS": float(imp),
        f"{prefix}CVE_SALDOMONLOC": float(saldo),
        f"{prefix}CVE_SALDOMONCC": float(saldo),
        f"{prefix}CVEMCOT_COTIZA": 1.0,
    }


_DEBT_ROWS = [
    _debt_row(
        "FHK",
        "00004",
        "A",
        "00001234",
        "000123",
        "2026-03-01T00:00:00.000Z",
        500,
        imp=1000,
    ),
    _debt_row(
        "PRE", "00004", " ", "00000077", "000123", "2026-01-15T00:00:00.000Z", 200
    ),
    _debt_row("SID", "    ", " ", "00000005", "000123", "2026-02-15T00:00:00.000Z", 80),
    _debt_row("FHK", "00004", "A", "00001000", "000123", "2025-02-01T00:00:00.000Z", 0),
    _debt_row(
        "NC", "00004", "A", "00000009", "000123", "2026-04-01T00:00:00.000Z", -50
    ),
    _debt_row(
        "FHK", "00004", "A", "00001235", "000124", "2026-05-01T00:00:00.000Z", 999
    ),
]
_UNTAXED_PREFIX = "STA_LIBROIVAVENTAS_2_001"


def _untaxed_row(
    codint, comprobante, codcli, neto, ngex, total, prefix=_UNTAXED_PREFIX
):
    """Return a row shaped like the real `Libro IVA Ventas/IA CRIS` view.

    Taxes and perceptions fill the gap between the untaxed amount and the total.
    """
    taxes = round(float(total) - float(neto) - float(ngex), 2)
    return {
        f"{prefix}FECHA_EMISION": "2026-01-01T00:00:00.000Z",
        f"{prefix}FECHA_CONTAB": "2026-01-01T00:00:00.000Z",
        f"{prefix}CLIENTE_COD": codcli,
        f"{prefix}CUIT": "20-12345678-9",
        f"{prefix}TOTAL": float(total),
        f"{prefix}NETO": float(neto),
        f"{prefix}IVA": taxes,
        f"{prefix}NGEX": float(ngex),
        f"{prefix}CVEPRE_COD": "KRAS",
        f"{prefix}PERIVA": 0.0,
        f"{prefix}PERIIBB_PBA": 0.0,
        f"{prefix}PERIIBB_CABA": 0.0,
        f"{prefix}PERGAN": 0.0,
        f"{prefix}CLIENTE": "SYNTHETIC CUSTOMER",
        f"{prefix}COMPROBANTE": comprobante,
        f"{prefix}COMP_COD": "FC",
        f"{prefix}COMP_CODINT": codint,
    }


# The sales VAT book row of `_DEBT_ROWS`' partly paid invoice: untaxed 80 %.
_UNTAXED_ROWS = [
    _untaxed_row("FHK", "A 00004-00001234", "000123", 700, 100, 1000),
]

_PREFIX_DEBT = [
    _debt_row(
        "FHK",
        "00004",
        "A",
        "00000009",
        "000123",
        "2026-02-01T00:00:00.000Z",
        10,
        prefix=_QUERY_PREFIX,
    ),
]


def _query_view(scenario, view, pack, fail):
    """Kras's views: `Saldos al dia` and the sales VAT book; nothing else."""
    if view == _UNTAXED_VIEW or scenario == _UNTAXED_SCENARIO:
        if "untaxed_view" in fail:
            return _EJECUTAR_ERROR
        return _ejecutar_ok(pack.get("untaxed", _UNTAXED_ROWS), double_encoded=True)
    if view == _DEBT_VIEW or scenario == _DEBT_SCENARIO:
        if "debt_view" in fail:
            return _EJECUTAR_ERROR
        return _ejecutar_ok(pack.get("debts", _DEBT_ROWS), double_encoded=True)
    # Bejerman has no seller view: any other view must never be asked for.
    return None


def _erp_post(url, content=None, headers=None, payloads_by_url=None, fail=(), **kw):
    return common._erp_post(
        url,
        content=content,
        headers=headers,
        payloads_by_url=payloads_by_url,
        fail=fail,
        query_view=_query_view,
        **kw,
    )


def _patch_httpx(payloads_by_url=None, fail=()):
    return common._patch_httpx(payloads_by_url, fail, query_view=_query_view)


class BejermanTestMixin(common.BejermanTestMixin):
    """Kras connectors: the base builders plus the Kras profile and views."""

    def _bejerman_vals(self, **overrides):
        vals = super()._bejerman_vals(
            bejerman_profile="kras",
            bejerman_debt_scenario=_DEBT_SCENARIO,
            bejerman_debt_view=_DEBT_VIEW,
            bejerman_quotation_kinds="PRE",
        )
        vals.update(overrides)
        return vals

    def _patch_erp(self, **kwargs):
        return _patch_httpx(**kwargs)

    def _assert_no_leak(self, message, *extra):
        for token in _LEAK_TOKENS + extra:
            if token:
                self.assertNotIn(token, message)


class TestBejermanConnector(BejermanTestMixin, TransactionCase):
    """Bejerman connector: type registration, SOAP session, no leak."""

    def test_bejerman_selection_add_and_cascade(self):
        """Type bejerman is selectable; uninstall cascade is registered."""
        field = self.env["collection.connector"]._fields["type"]
        keys = {value for value, _label in field._description_selection(self.env)}
        self.assertIn("bejerman", keys)
        self.assertEqual(field.ondelete.get("bejerman"), "cascade")
        connector = self.env["collection.connector"].create(
            {
                "name": "Bejerman Connector",
                "type": "bejerman",
            }
        )
        self.assertEqual(connector.type, "bejerman")
        manifest = load_manifest("collection_connector_bejerman")
        self.assertIn("collection", manifest["depends"])

    def test_session_cierre_on_ejecutar_failure(self):
        """Cierre still POSTs after Ejecutar fail; Token/xClave/ErrorMsg stay out."""
        from odoo.addons.collection_connector_bejerman.service.errors import (
            BejermanError,
        )
        from odoo.addons.collection_connector_bejerman.service.session import (
            BejermanClient,
        )
        from odoo.addons.collection_connector_bejerman.service.settings import (
            Credentials,
            Settings,
        )

        settings = Settings(
            ws_url=_WS_URL,
            credentials=Credentials(
                user="ADMIN",
                password=_CLAVE,
                company="MODE",
                workstation="1",
                branch="",
            ),
        )
        session_logger = "odoo.addons.collection_connector_bejerman.service.session"
        with patch(
            "odoo.addons.collection_connector_bejerman.service.session"
            ".httpx.Client.post"
        ) as mock_post:
            mock_post.side_effect = [
                _xml_response(_REGISTRO_OK),
                _xml_response(_EJECUTAR_ERROR),
                _xml_response(_CIERRE_OK),
            ]
            with self.assertLogs(session_logger, level="DEBUG") as captured:
                with self.assertRaises(BejermanError) as ctx:
                    with BejermanClient(settings) as client:
                        client.tablas.get_customers()

        actions = [_call_soap_action(call) for call in mock_post.call_args_list]
        self.assertEqual(actions[0], _SOAPACTION_REGISTRO)
        self.assertEqual(actions[1], _SOAPACTION_EJECUTAR)
        self.assertEqual(actions[-1], _SOAPACTION_CIERRE)
        self.assertEqual(len(actions), 3)
        cierre_body = _call_body(mock_post.call_args_list[-1])
        self.assertIn("EFlexSDK_WSCierreProceso", cierre_body)
        self.assertIn(_SESSION_TOKEN, cierre_body)

        exception_text = str(ctx.exception)
        self.assertNotIn(_ERROR_MSG, exception_text)
        self.assertNotIn(_SESSION_TOKEN, exception_text)
        self.assertNotIn(_CLAVE, exception_text)
        self.assertNotIn(_WS_URL, exception_text)

        log_blob = "\n".join(captured.output)
        self.assertTrue(log_blob)
        self.assertIn("***", log_blob)
        self.assertNotIn(_SESSION_TOKEN, log_blob)
        self.assertNotIn(_CLAVE, log_blob)
        self.assertNotIn(_ERROR_MSG, log_blob)
        self.assertNotIn(_WS_URL, log_blob)
        registro_body = _call_body(mock_post.call_args_list[0])
        self.assertIn("xClave", registro_body)
        self.assertIn(_CLAVE, registro_body)

    def test_mapping_value_by_suffix_trim_cuit_kind_outstanding(self):
        """Suffix map, trim pad, CUIT, FC/quotation kind, outstanding-only."""
        from odoo.addons.collection_connector_bejerman.service import mapping
        from odoo.addons.collection_connector_bejerman_kras.service import (
            mapping as kras_mapping,
        )

        row = {"CpoCalc_id": "000E", "CpoCalc_name": "Ana"}
        self.assertEqual(mapping.value_by_suffix(row, "id"), "000E")
        self.assertEqual(mapping.value_by_suffix(row, "missing"), None)
        self.assertEqual(mapping.trim_code("000E"), "E")
        self.assertEqual(mapping.trim_code("0"), "0")
        self.assertEqual(mapping.trim_code("000"), "0")
        self.assertEqual(mapping.canonicalize_cuit("20123456789"), "20-12345678-9")
        self.assertEqual(mapping.canonicalize_cuit("20-12345678-9"), "20-12345678-9")
        self.assertEqual(kras_mapping.document_kind("FC A", "NP,PR"), "invoice")
        self.assertEqual(kras_mapping.document_kind("NP", "NP,PR"), "quotation")
        self.assertEqual(kras_mapping.document_kind("RC", "NP,PR"), "invoice")
        self.assertFalse(hasattr(mapping, "map_sellers"))

    def test_four_ops_return_sqlite_raw_shapes(self):
        """Four read ops return sqlite raw keys with no MCP source."""
        connector = self._make_connector()
        self._refresh(connector)
        with _patch_httpx() as mock_post:
            via_dispatch = connector._dispatch("test_connection")
            via_private = connector._bejerman_test_connection()
            sellers = connector._dispatch("list_sellers")
        customers = connector._bejerman_list_customers("E")
        debt = connector._dispatch("get_customer_debt", "E", "20123456789")
        self.assertTrue(via_dispatch)
        self.assertIs(via_private, True)
        self.assertTrue(mock_post.called)
        self.assertIsInstance(sellers, list)
        self.assertTrue(sellers)
        self.assertEqual(set(sellers[0]), _SELLER_KEYS)
        self.assertNotIn("source", sellers[0])
        self.assertIsInstance(customers, list)
        self.assertTrue(customers)
        self.assertEqual(set(customers[0]), _CUSTOMER_KEYS)
        self.assertNotIn("source", customers[0])
        # T25 added `as_of`, the snapshot time (an Odoo UTC datetime string),
        # and T27 the customer's open `credit_total`.
        self.assertEqual(set(debt), {"customer", "invoices", "as_of", "credit_total"})
        self.assertNotIn("source", debt)
        self.assertEqual(set(debt["customer"]), _CUSTOMER_KEYS)
        self.assertTrue(debt["invoices"])
        self.assertEqual(set(debt["invoices"][0]), _INVOICE_KEYS)

    def test_create_receipt_unavailable_no_post(self):
        """create_receipt is omitted; dispatch raises; httpx is never POSTed."""
        connector = self._make_connector()
        with _patch_httpx() as mock_post:
            with self.assertRaises(UserError) as dispatched:
                connector._dispatch("create_receipt", {})
            with self.assertRaises(UserError) as public:
                connector.create_receipt({})
        self.assertEqual(str(dispatched.exception), _UNAVAILABLE)
        self.assertEqual(str(public.exception), _UNAVAILABLE)
        mock_post.assert_not_called()
        self.assertFalse(hasattr(connector, "_bejerman_create_receipt"))

    def test_connection_probe_then_cierre(self):
        """Test Connection: Registro, ObtenerClientes, then Cierre.

        There is no seller view, and the ~200 MB debt view is exercised by the
        snapshot refresh only.
        """
        connector = self._make_connector()
        with _patch_httpx() as mock_post:
            result = connector._bejerman_test_connection()
        self.assertIs(result, True)
        actions = [_call_soap_action(call) for call in mock_post.call_args_list]
        self.assertEqual(actions[0], _SOAPACTION_REGISTRO)
        self.assertEqual(actions[-1], _SOAPACTION_CIERRE)
        self.assertEqual(len(actions), 3)
        bodies = [_call_body(call) for call in mock_post.call_args_list]
        self.assertIn("ObtenerClientes", bodies[1])
        self.assertIn("EFlexSDK_WSCierreProceso", bodies[-1])
        self.assertFalse(any("ObtenerJsonQueryVista" in body for body in bodies))

    def test_missing_get_customers_fails_closed(self):
        """A missing ObtenerClientes is a UserError; Cierre still runs."""
        connector = self._make_connector()
        with _patch_httpx(fail=("ObtenerClientes",)) as mock_post:
            with self.assertRaises(UserError) as ctx:
                connector._bejerman_test_connection()
        self.assertEqual(
            str(ctx.exception),
            "Connected to Bejerman, but the customer list could not be read.",
        )
        self._assert_no_leak(str(ctx.exception))
        actions = [_call_soap_action(call) for call in mock_post.call_args_list]
        self.assertEqual(actions[0], _SOAPACTION_REGISTRO)
        self.assertEqual(actions[-1], _SOAPACTION_CIERRE)
        self.assertEqual(len(actions), 3)

    def test_list_sellers_before_first_refresh_fails_closed_without_a_call(self):
        """No snapshot yet: the generic error, and nothing is sent to Bejerman."""
        connector = self._make_connector()
        with _patch_httpx() as mock_post:
            with self.assertRaises(UserError) as ctx:
                connector._bejerman_list_sellers()
        self._assert_clean_user_error(ctx.exception)
        mock_post.assert_not_called()

    def _seller_pack(self):
        rows = [
            *_DEBT_ROWS,
            _debt_row(
                "FHK",
                "00004",
                "A",
                "00005555",
                "000123",
                "2026-03-01",
                0,
                vendedor="00SA1",
                vendedor_nombre="SANCHEZ ANA",
            ),
        ]
        return {"clientes": _CLIENTES, "debts": rows}

    def _bind_seller(self, connector, code, phone, commission, enabled=True):
        """Bind a fresh internal user to ``code``; optionally enable it."""
        company = connector.company_id
        user = company._collection_create_connector_seller_users(
            [{"external_seller_id": code, "name": f"User {code}", "mobile": phone}]
        )
        self.env["collection.connector.seller.binding"].create(
            {
                "connector_id": connector.id,
                "external_seller_id": code,
                "user_id": user.id,
                "commission_percent": commission,
            }
        )
        if enabled:
            company.write({"enabled_seller_ids": [(4, user.id)]})
        return user

    def test_list_sellers_reads_the_snapshot_without_calling_bejerman(self):
        """New sellers come in inactive, commission 0, blank mobile; no POST."""
        connector = self._make_connector()
        self._refresh(connector, payloads_by_url={"*": self._seller_pack()})
        with _patch_httpx() as mock_post:
            sellers = connector._bejerman_list_sellers()
        mock_post.assert_not_called()
        self.assertEqual(
            sellers,
            [
                {
                    "id": "DN",
                    "name": "SYNTHETIC SELLER",
                    "mobile": "",
                    "active": False,
                    "commission_percent": 0.0,
                },
                {
                    "id": "SA1",
                    "name": "SANCHEZ ANA",
                    "mobile": "",
                    "active": False,
                    "commission_percent": 0.0,
                },
            ],
        )
        self.assertEqual(set(sellers[0]), _SELLER_KEYS)

    def test_list_sellers_merges_odoo_commission_phone_and_enabled(self):
        """The bound user's phone, the binding commission and enabled come back."""
        connector = self._make_connector()
        self._refresh(connector, payloads_by_url={"*": self._seller_pack()})
        self._bind_seller(connector, "DN", "5491155510001", 7.5)
        self._bind_seller(connector, "SA1", "5491166660002", 3.0, enabled=False)
        with _patch_httpx() as mock_post:
            by_id = {row["id"]: row for row in connector._bejerman_list_sellers()}
        mock_post.assert_not_called()
        self.assertEqual(by_id["DN"]["commission_percent"], 7.5)
        self.assertEqual(by_id["DN"]["mobile"], "5491155510001")
        self.assertTrue(by_id["DN"]["active"])
        # Bound but not enabled: inactive, yet its admin values are not lost.
        self.assertEqual(by_id["SA1"]["commission_percent"], 3.0)
        self.assertEqual(by_id["SA1"]["mobile"], "5491166660002")
        self.assertFalse(by_id["SA1"]["active"])
        self.assertEqual(by_id["DN"]["name"], "SYNTHETIC SELLER")

    def test_import_round_trip_keeps_admin_values(self):
        """A bound, enabled seller survives an import with commission and phone."""
        connector = self._make_connector()
        company = connector.company_id
        company.collection_connector_id = connector
        self._refresh(connector, payloads_by_url={"*": self._seller_pack()})
        user = self._bind_seller(connector, "DN", "5491155510001", 7.5)
        users_before = self.env["res.users"].search_count([])
        bindings = self.env["collection.connector.seller.binding"]
        with _patch_httpx() as mock_post:
            company._sync_enabled_sellers_from_connector()
        mock_post.assert_not_called()
        binding = bindings.search([("connector_id", "=", connector.id)])
        self.assertEqual(binding.mapped("external_seller_id"), ["DN"])
        self.assertEqual(binding.commission_percent, 7.5)
        self.assertEqual(binding.user_id, user)
        self.assertEqual(user.partner_id.phone, "5491155510001")
        self.assertEqual(company.enabled_seller_ids, user)
        # Actual behaviour: the generic list drops inactive rows, so the not yet
        # enabled seller SA1 gets neither a user nor a binding from this import.
        self.assertEqual(self.env["res.users"].search_count([]), users_before)

    def test_import_ignores_sellers_that_are_not_enabled(self):
        """Inactive rows are dropped by the generic list: no user, no binding."""
        connector = self._make_connector()
        company = connector.company_id
        company.collection_connector_id = connector
        self._refresh(connector, payloads_by_url={"*": self._seller_pack()})
        users_before = self.env["res.users"].search_count([])
        self.assertEqual(connector.list_sellers()["sellers"], [])
        # T28: an empty answer is refused instead of being applied silently.
        with self.assertRaises(UserError):
            company._sync_enabled_sellers_from_connector()
        self.assertFalse(connector.seller_binding_ids)
        self.assertFalse(company.enabled_seller_ids)
        self.assertEqual(self.env["res.users"].search_count([]), users_before)

    def test_list_customers_seller_scope_cuit(self):
        """Only seller E's active customers; CUIT is XX-XXXXXXXX-X."""
        connector = self._make_connector()
        self._refresh(connector)
        rows = connector._bejerman_list_customers("E")
        padded = connector._bejerman_list_customers("000E")
        ids = [row["id"] for row in rows]
        self.assertEqual(ids, ["123"])
        self.assertEqual(rows[0]["identification"], "20-12345678-9")
        self.assertEqual(rows[0]["customer_code"], "123")
        self.assertNotIn("124", ids)
        self.assertNotIn("125", ids)
        self.assertEqual([row["id"] for row in padded], ["123"])

    def test_get_customer_debt_kind_outstanding(self):
        """PRE→quotation, others→invoice; zero/negative/other customer omitted."""
        connector = self._make_connector()
        self._refresh(connector)
        with _patch_today(connector):
            debt = connector._bejerman_get_customer_debt("E", "20-12345678-9")
        self.assertEqual(debt["customer"]["identification"], "20-12345678-9")
        by_id = {row["id"]: row for row in debt["invoices"]}
        self.assertEqual(
            set(by_id),
            {"FHK|00004|A|00001234", "PRE|00004||00000077", "SID|||00000005"},
        )
        self.assertEqual(by_id["FHK|00004|A|00001234"]["document_kind"], "invoice")
        self.assertEqual(by_id["PRE|00004||00000077"]["document_kind"], "quotation")
        self.assertEqual(by_id["SID|||00000005"]["document_kind"], "invoice")
        self.assertEqual(by_id["FHK|00004|A|00001234"]["state"], "open")
        self.assertEqual(by_id["PRE|00004||00000077"]["state"], "overdue")
        self.assertEqual(by_id["SID|||00000005"]["state"], "open")
        self.assertEqual(
            by_id["FHK|00004|A|00001234"]["number"], "FHK A 00004-00001234"
        )
        self.assertEqual(by_id["FHK|00004|A|00001234"]["amount"], 1000.0)
        self.assertEqual(by_id["FHK|00004|A|00001234"]["balance"], 500.0)
        self.assertEqual(by_id["FHK|00004|A|00001234"]["issue_date"], "2026-01-01")
        self.assertEqual(by_id["FHK|00004|A|00001234"]["due_date"], "2026-03-01")

    def test_get_customer_debt_unknown_empty(self):
        """Unknown, other-seller, or inactive identification returns empty debt."""
        connector = self._make_connector()
        empty = {"customer": None, "invoices": []}
        self._refresh(connector)
        with _patch_today(connector):
            unknown = connector._bejerman_get_customer_debt("E", "20999999991")
            other = connector._bejerman_get_customer_debt("E", "27987654321")
            inactive = connector._bejerman_get_customer_debt("E", "23000000009")
        self.assertEqual(unknown, empty)
        self.assertEqual(other, empty)
        self.assertEqual(inactive, empty)

    def test_get_customer_debt_matches_code_and_unique_number(self):
        """A customer code, or its number when only one customer has it, reads debt."""
        connector = self._make_connector()
        self._refresh(connector)
        empty = {"customer": None, "invoices": []}
        with _patch_today(connector):
            by_cuit = connector._bejerman_get_customer_debt("E", "20-12345678-9")
            by_code = connector._bejerman_get_customer_debt("E", "123")
            by_padded = connector._bejerman_get_customer_debt("E", "000123")
            other_code = connector._bejerman_get_customer_debt("E", "124")
        self.assertEqual(by_code["customer"]["customer_code"], "123")
        self.assertEqual(by_padded["customer"]["customer_code"], "123")
        self.assertEqual(by_code["invoices"], by_cuit["invoices"])
        self.assertEqual(other_code, empty)

        norte = {
            "Cliente_Codigo": "NS0160",
            "Cliente_RazonSocial": "Norte SA",
            "Cliente_CUIT": "30765432109",
            "Cliente_Habilitado": True,
            "Cliente_VendedorCodigo": "000E",
        }
        ambiguous = {
            "Cliente_Codigo": "AB0160",
            "Cliente_RazonSocial": "Ambiguous SA",
            "Cliente_CUIT": "30111222339",
            "Cliente_Habilitado": True,
            "Cliente_VendedorCodigo": "000E",
        }
        debts = [
            _debt_row("FHK", "1", "A", "9", "NS0160", "2026-03-01", 80, vendedor="E"),
        ]
        self._refresh(
            connector,
            payloads_by_url={
                "*": {"clientes": [norte, ambiguous], "debts": debts},
            },
        )
        with _patch_today(connector):
            by_full = connector._bejerman_get_customer_debt("E", "NS0160")
            by_lower = connector._bejerman_get_customer_debt("E", "ns0160")
            shared_number = connector._bejerman_get_customer_debt("E", "0160")
        self.assertEqual(by_full["customer"]["customer_code"], "NS0160")
        self.assertEqual(by_lower["customer"]["customer_code"], "NS0160")
        self.assertEqual(by_full["invoices"][0]["balance"], 80.0)
        self.assertEqual(shared_number, empty)

        self._refresh(
            connector,
            payloads_by_url={"*": {"clientes": [norte], "debts": debts}},
        )
        with _patch_today(connector):
            by_number = connector._bejerman_get_customer_debt("E", "0160")
            by_short = connector._bejerman_get_customer_debt("E", "160")
        self.assertEqual(by_number["customer"]["customer_code"], "NS0160")
        self.assertEqual(by_short["customer"]["customer_code"], "NS0160")
        self.assertEqual(by_number["invoices"], by_full["invoices"])

    def test_emitted_ids_omit_query_prefixes(self):
        """Query-generated key prefixes must not appear in emitted ids/codes."""
        connector = self._make_connector()
        payloads = {
            _WS_URL: {
                "clientes": _CLIENTES,
                "debts": _PREFIX_DEBT,
            }
        }
        self._refresh(connector, payloads_by_url=payloads)
        sellers = connector._bejerman_list_sellers()
        with _patch_today(connector):
            customers = connector._bejerman_list_customers("E")
            debt = connector._bejerman_get_customer_debt("E", "20123456789")
        blob = json.dumps({"sellers": sellers, "customers": customers, "debt": debt})
        self.assertNotIn(_QUERY_PREFIX, blob)
        self.assertEqual(sellers[0]["id"], "DN")
        self.assertEqual(customers[0]["id"], "123")
        self.assertEqual(debt["invoices"][0]["id"], "FHK|00004|A|00000009")

    def test_company_a_does_not_read_company_b(self):
        """Company A must not POST to B's URL or list B's sellers."""
        connector_a = self._make_connector(name="Bejerman A")
        company_b = self._create_company("Bejerman Co B")
        connector_b = (
            self.env["collection.connector"]
            .with_company(company_b)
            .create(
                self._bejerman_vals(
                    name="Bejerman B",
                    company_id=company_b.id,
                    bejerman_ws_url=_WS_URL_B,
                    bejerman_clave="other-clave-b",
                    bejerman_usuario="USERB",
                )
            )
        )
        payloads = {
            _WS_URL: {"clientes": _CLIENTES, "debts": _DEBT_ROWS},
            _WS_URL_B: {
                "clientes": [],
                "debts": [
                    _debt_row(
                        "FHK",
                        "1",
                        "A",
                        "1",
                        "9",
                        "2026-03-01",
                        1,
                        vendedor="B1",
                        vendedor_nombre="COMPANY B SELLER",
                    )
                ],
            },
        }
        posted = []

        def _post(url, content=None, headers=None, **kwargs):
            posted.append(url)
            return _erp_post(
                url,
                content=content,
                headers=headers,
                payloads_by_url=payloads,
                **kwargs,
            )

        with patch(
            "odoo.addons.collection_connector_bejerman.service.session"
            ".httpx.Client.post",
            side_effect=_post,
        ):
            connector_a._bejerman_refresh_snapshot()
            posted_after_a = list(posted)
            connector_b._bejerman_refresh_snapshot()
            sellers_a = connector_a._bejerman_list_sellers()
            sellers_b = connector_b._bejerman_list_sellers()
        self.assertTrue(posted_after_a)
        self.assertTrue(all(url == _WS_URL for url in posted_after_a))
        self.assertNotIn(_WS_URL_B, posted_after_a)
        self.assertEqual({row["id"] for row in sellers_a}, {"DN"})
        self.assertEqual({row["id"] for row in sellers_b}, {"B1"})
        self.assertIn(_WS_URL_B, posted)
        # Listing is a snapshot read: no further request after the refreshes.
        count = len(posted)
        connector_a._bejerman_list_sellers()
        self.assertEqual(len(posted), count)

    def test_connector_form_permissions(self):
        """One form exposes manager settings while protecting system fields."""
        Connector = self.env["collection.connector"]
        manager_fields = {
            "bejerman_empresa",
            "bejerman_puesto",
            "bejerman_sucursal",
            "bejerman_timeout",
            "bejerman_debt_scenario",
            "bejerman_debt_view",
            "bejerman_debt_filter_field",
            "bejerman_untaxed_scenario",
            "bejerman_untaxed_view",
            "bejerman_quotation_kinds",
        }
        for name in _BEJERMAN_CONFIG_FIELDS:
            groups = set(Connector._fields[name].groups.split(","))
            expected = {"base.group_system"}
            if name in manager_fields:
                expected.add("collection.group_collection_manager")
            self.assertEqual(groups, expected, name)

        manifest = load_manifest("collection_connector_bejerman")
        self.assertEqual(
            list(manifest.get("data") or []),
            [
                "security/ir.model.access.csv",
                "security/collection_connector_bejerman_security.xml",
                "views/collection_connector_bejerman_seller_enable_views.xml",
                "views/collection_connector_views.xml",
                "views/collection_connector_bejerman_debt_views.xml",
                "data/ir_cron.xml",
                "data/mail_activity_type_data.xml",
            ],
        )
        self.assertFalse(manifest.get("post_init_hook"))

        view = self.env.ref(
            "collection_connector_bejerman.view_collection_connector_form_bejerman"
        )
        self.assertEqual(view.model, "collection.connector")
        self.assertEqual(
            view.inherit_id,
            self.env.ref("collection.view_collection_connector_form"),
        )
        raw = etree.fromstring(view.arch)
        self.assertFalse(raw.xpath("//button[@name='test_connection']"))
        kras_view = self.env.ref(
            "collection_connector_bejerman_kras.view_collection_connector_form_bejerman_kras"
        )
        self.assertEqual(kras_view.inherit_id, view)
        kras_raw = etree.fromstring(kras_view.arch)
        for name in _BEJERMAN_CONFIG_FIELDS:
            nodes = raw.xpath(f"//field[@name='{name}']") or kras_raw.xpath(
                f"//field[@name='{name}']"
            )
            self.assertTrue(nodes, f"form inherit must expose {name}")
            self.assertIn("bejerman", nodes[0].get("invisible") or "")
        clave_node = raw.xpath("//field[@name='bejerman_clave']")[0]
        password_flag = (clave_node.get("password") or "").lower()
        widget = (clave_node.get("widget") or "").lower()
        self.assertTrue(
            password_flag in ("1", "true") or widget == "password",
            "clave must use the password widget",
        )

        internal = self._create_user(
            "bejerman_internal", "collection.group_collection_user"
        )
        system = self._create_user(
            "bejerman_system",
            "base.group_user",
            "base.group_system",
        )
        admin_fields = Connector.with_user(system).fields_get()
        user_fields = Connector.with_user(internal).fields_get()
        for name in _BEJERMAN_CONFIG_FIELDS:
            self.assertIn(name, admin_fields)
            self.assertNotIn(name, user_fields)

        admin_arch = self._form_arch_for(system)
        user_arch = self._form_arch_for(internal)
        admin_button = admin_arch.xpath("//button[@name='test_connection']")
        self.assertTrue(admin_button, "admin form must expose Bejerman Test Connection")
        self.assertFalse(
            user_arch.xpath("//button[@name='test_connection']"),
            "non-admin form must omit Bejerman Test Connection",
        )
        for name in ("bejerman_clave", "bejerman_usuario", "bejerman_ws_url"):
            self.assertTrue(
                admin_arch.xpath(f"//field[@name='{name}']"),
                f"admin form must expose {name}",
            )
            self.assertFalse(
                user_arch.xpath(f"//field[@name='{name}']"),
                f"non-admin form must omit {name}",
            )

        manager = self._create_user(
            "bejerman_settings_manager", "collection.group_collection_manager"
        )
        manager_arch = self._form_arch_for(manager)
        self.assertEqual(
            len(manager_arch.xpath("//button[@name='test_connection']")), 1
        )
        self.assertEqual(len(manager_arch.xpath("//header")), 1)
        for name in manager_fields:
            self.assertEqual(
                len(manager_arch.xpath(f"//field[@name='{name}']")), 1, name
            )
        for name in set(_BEJERMAN_CONFIG_FIELDS) - manager_fields:
            self.assertFalse(manager_arch.xpath(f"//field[@name='{name}']"), name)

        connector = self._make_connector()
        read = connector.with_user(system).read(["bejerman_clave", "bejerman_usuario"])
        self.assertEqual(read[0]["bejerman_clave"], _CLAVE)
        self.assertEqual(read[0]["bejerman_usuario"], _USUARIO)
        with self.assertRaises(AccessError):
            connector.with_user(internal).read(["bejerman_clave"])
        with self.assertRaises(AccessError):
            connector.with_user(internal).read(["bejerman_usuario"])

    def test_manager_tests_stored_credentials_without_reading_them(self):
        connector = self._make_connector()
        manager = self._create_user(
            "bejerman_test_manager", "collection.group_collection_manager"
        )
        managed = connector.with_user(manager)
        with self.assertRaises(AccessError):
            managed.read(["bejerman_clave"])
        with _patch_httpx():
            result = managed.test_connection()
        self.assertEqual(result["tag"], "display_notification")

    def test_errors_no_leak(self):
        """SOAP, view, and credential failures omit _LEAK_TOKENS from UserError."""
        connector = self._make_connector()

        with _patch_httpx(fail=("ObtenerClientes",)):
            with self.assertRaises(UserError) as ctx:
                connector.test_connection()
        self.assertNotIn(_CLAVE, str(ctx.exception))
        self._assert_no_leak(str(ctx.exception))

        with self.assertRaises(UserError) as ctx:
            connector.list_sellers()
        self._assert_clean_user_error(ctx.exception)

        with _patch_httpx(fail=("debt_view",)):
            with self.assertRaises(UserError) as ctx:
                connector._bejerman_refresh_snapshot()
        self.assertEqual(str(ctx.exception), _REFRESH_FAILED)
        self._assert_no_leak(str(ctx.exception))

        with _patch_httpx(fail=("Registro",)):
            with self.assertRaises(UserError) as ctx:
                connector._bejerman_test_connection()
        self.assertNotIn(_CLAVE, str(ctx.exception))
        self._assert_no_leak(str(ctx.exception))

        bad_url = "http://erp.secret.example/leaky-ws"
        bad = self._make_connector(name="Bejerman Bad URL", bejerman_ws_url=bad_url)
        with self.assertRaises(UserError) as ctx:
            bad.test_connection()
        self.assertNotIn("leaky-ws", str(ctx.exception))
        self._assert_no_leak(str(ctx.exception), bad_url)

        def _boom(_url, content=None, headers=None, **_kwargs):
            del content, headers
            raise httpx.ConnectError(
                f"connect {_WS_URL} user={_USUARIO} clave={_CLAVE} "
                f"Token={_SESSION_TOKEN} ErrorMsg={_ERROR_MSG} "
                f"view={_SELLER_VIEW} SOAP {_DEBT_VIEW}"
            )

        with (
            patch(
                "odoo.addons.collection_connector_bejerman.service.session"
                ".httpx.Client.post",
                side_effect=_boom,
            ),
            # Connect errors are retried (T26); the test must not sleep.
            patch("odoo.addons.collection_connector_bejerman.service.session._sleep"),
        ):
            with self.assertRaises(UserError) as ctx:
                connector._bejerman_refresh_snapshot()
        self.assertEqual(str(ctx.exception), _REFRESH_FAILED)
        self._assert_no_leak(str(ctx.exception))

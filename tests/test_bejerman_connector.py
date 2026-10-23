# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

"""Kras's query views on the base fake ERP, and the connector tests."""

from odoo.addons.collection_connector_bejerman.tests import common
from odoo.addons.collection_connector_bejerman.tests.common import (
    _EJECUTAR_ERROR,
    _ejecutar_ok,
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

# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo.tests import TransactionCase

from odoo.addons.collection_connector_bejerman.tests.common import (
    _CLIENTES,
    _patch_today,
)

from .test_bejerman_connector import BejermanTestMixin, _debt_row, _patch_httpx

_ACME = "20-12345678-9"  # 000123, customer master seller E
_OTHER = "27987654321"  # 000124, customer master seller G
_EMPTY = {"customer": None, "invoices": []}


def _invoice(nro, codcli, saldo, vendedor):
    return _debt_row(
        "FHK", "00004", "A", nro, codcli, "2026-03-01", saldo, vendedor=vendedor
    )


def _credit_note(nro, codcli, saldo, vendedor):
    return _debt_row(
        "NC", "00004", "A", nro, codcli, "2026-03-01", saldo, vendedor=vendedor
    )


class TestBejermanDebtSellerScope(BejermanTestMixin, TransactionCase):
    """A seller sees the customers and documents whose voucher seller is them.

    The customer master's seller never decides: only the seller code of each
    open document (invoice, quotation or credit note) does.
    """

    def _refresh_debts(self, connector, debts):
        pack = {"clientes": _CLIENTES, "debts": debts}
        self._refresh(connector, payloads_by_url={"*": pack})

    def _debt(self, connector, seller, identification):
        with _patch_today(connector):
            return connector._bejerman_get_customer_debt(seller, identification)

    def _ids(self, debt):
        return [row["id"] for row in debt["invoices"]]

    def test_document_seller_sees_the_customer_not_the_master_seller(self):
        """Debt issued by E to G's customer is E's; G's customer master is ignored."""
        connector = self._make_connector()
        self._refresh_debts(
            connector,
            [
                _invoice("00000001", "000124", 300, "E"),
                _invoice("00000002", "000123", 500, "G"),
            ],
        )
        self.assertEqual(
            [row["id"] for row in connector._bejerman_list_customers("E")], ["124"]
        )
        self.assertEqual(
            [row["id"] for row in connector._bejerman_list_customers("G")], ["123"]
        )
        self.assertEqual(
            self._ids(self._debt(connector, "E", _OTHER)), ["FHK|00004|A|00000001"]
        )
        self.assertEqual(self._debt(connector, "E", _ACME), _EMPTY)
        self.assertEqual(
            self._ids(self._debt(connector, "G", _ACME)), ["FHK|00004|A|00000002"]
        )

    def test_each_seller_reads_only_its_own_documents_and_credits(self):
        """One customer, two sellers: invoices and credit notes split by seller."""
        connector = self._make_connector()
        self._refresh_debts(
            connector,
            [
                _invoice("00000001", "000123", 1000, "E"),
                _invoice("00000002", "000123", 400, "DN"),
                _credit_note("00000003", "000123", -100, "E"),
                _credit_note("00000004", "000123", -30, "DN"),
            ],
        )
        mine = self._debt(connector, "E", _ACME)
        theirs = self._debt(connector, "DN", _ACME)
        self.assertEqual(self._ids(mine), ["FHK|00004|A|00000001"])
        self.assertEqual(mine["credit_total"], 100.0)
        self.assertEqual(self._ids(theirs), ["FHK|00004|A|00000002"])
        self.assertEqual(theirs["credit_total"], 30.0)
        summary = connector._format_customer_debt(mine)["summary"]
        self.assertEqual(
            (summary["gross_total"], summary["credit_total"], summary["net_total"]),
            (1000.0, 100.0, 900.0),
        )

    def test_customer_without_the_sellers_open_documents_is_hidden(self):
        """No open document carries E: E's own master customer is not shown."""
        connector = self._make_connector()
        self._refresh_debts(
            connector,
            [
                _invoice("00000001", "000123", 0, "E"),
                _invoice("00000002", "000124", 300, "G"),
            ],
        )
        self.assertEqual(connector._bejerman_list_customers("E"), [])
        self.assertEqual(self._debt(connector, "E", _ACME), _EMPTY)

    def test_customer_with_only_the_sellers_credit_note_is_shown(self):
        """A credit note is an open document: its seller sees the balance in favour."""
        connector = self._make_connector()
        self._refresh_debts(connector, [_credit_note("00000001", "000124", -300, "E")])
        self.assertEqual(
            [row["id"] for row in connector._bejerman_list_customers("E")], ["124"]
        )
        debt = self._debt(connector, "E", _OTHER)
        self.assertEqual(debt["invoices"], [])
        summary = connector._format_customer_debt(debt)["summary"]
        self.assertEqual(summary["net_total"], -300.0)

    def test_live_path_scopes_documents_by_seller_too(self):
        """Read live: the same rule, whatever the customer master says."""
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        pack = {
            "clientes": _CLIENTES,
            "debts": [
                _invoice("00000001", "000124", 300, "E"),
                _invoice("00000002", "000124", 50, "DN"),
                _credit_note("00000003", "000124", -10, "DN"),
                _credit_note("00000004", "000124", -20, "E"),
                _invoice("00000005", "000123", 500, "G"),
            ],
        }
        with _patch_httpx(payloads_by_url={"*": pack}), _patch_today(connector):
            mine = connector._bejerman_get_customer_debt("E", _OTHER)
            master_only = connector._bejerman_get_customer_debt("E", _ACME)
        self.assertEqual(mine["customer"]["customer_code"], "124")
        self.assertEqual(self._ids(mine), ["FHK|00004|A|00000001"])
        self.assertEqual(mine["credit_total"], 20.0)
        self.assertEqual(master_only, _EMPTY)

    def test_live_path_resolves_a_shared_number_among_the_sellers_customers(self):
        """Two masters' customers share ``0160``: E's documents pick E's one."""
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        norte = {
            "Cliente_Codigo": "NS0160",
            "Cliente_RazonSocial": "Norte SA",
            "Cliente_CUIT": "30765432109",
            "Cliente_Habilitado": True,
            "Cliente_VendedorCodigo": "G",
        }
        sur = dict(
            norte,
            Cliente_Codigo="AB0160",
            Cliente_RazonSocial="Sur SA",
            Cliente_CUIT="30111222339",
            Cliente_VendedorCodigo="E",
        )
        pack = {
            "clientes": [norte, sur],
            "debts": [
                _invoice("00000001", "NS0160", 80, "E"),
                _invoice("00000002", "AB0160", 40, "G"),
            ],
        }
        self._refresh(connector, payloads_by_url={"*": pack})
        with _patch_httpx(payloads_by_url={"*": pack}), _patch_today(connector):
            mine = connector._bejerman_get_customer_debt("E", "0160")
            theirs = connector._bejerman_get_customer_debt("G", "0160")
            nobody = connector._bejerman_get_customer_debt("DN", "0160")
        self.assertEqual(mine["customer"]["customer_code"], "NS0160")
        self.assertEqual(self._ids(mine), ["FHK|00004|A|00000001"])
        self.assertEqual(theirs["customer"]["customer_code"], "AB0160")
        self.assertEqual(self._ids(theirs), ["FHK|00004|A|00000002"])
        self.assertEqual(nobody, _EMPTY)

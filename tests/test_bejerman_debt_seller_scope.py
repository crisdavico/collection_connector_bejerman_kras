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
    """A seller sees the customers whose open documents or master name them.

    Debt is scoped by the voucher seller of each open document (invoice,
    quotation or credit note) only. A customer the master assigns to the
    seller is listed even without documents of theirs, with no debt, so the
    seller can load an advance for it.
    """

    def _refresh_debts(self, connector, debts):
        pack = {"clientes": _CLIENTES, "debts": debts}
        self._refresh(connector, payloads_by_url={"*": pack})

    def _debt(self, connector, seller, identification):
        with _patch_today(connector):
            return connector._bejerman_get_customer_debt(seller, identification)

    def _ids(self, debt):
        return [row["id"] for row in debt["invoices"]]

    def _listed(self, connector, seller):
        return [row["id"] for row in connector._bejerman_list_customers(seller)]

    def _assert_no_debt(self, debt, customer_code):
        """The customer is known, with no document and no credit of the seller."""
        self.assertEqual(debt["customer"]["customer_code"], customer_code)
        self.assertEqual(debt["invoices"], [])
        self.assertEqual(debt["credit_total"], 0.0)

    def test_document_seller_and_master_seller_both_see_the_customer(self):
        """E invoiced G's customer and is Acme's master seller: E sees both.

        Each seller still reads only the documents they issued.
        """
        connector = self._make_connector()
        self._refresh_debts(
            connector,
            [
                _invoice("00000001", "000124", 300, "E"),
                _invoice("00000002", "000123", 500, "G"),
            ],
        )
        self.assertEqual(self._listed(connector, "E"), ["123", "124"])
        self.assertEqual(self._listed(connector, "G"), ["123", "124"])
        self.assertEqual(
            self._ids(self._debt(connector, "E", _OTHER)), ["FHK|00004|A|00000001"]
        )
        self._assert_no_debt(self._debt(connector, "E", _ACME), "123")
        self._assert_no_debt(self._debt(connector, "G", _OTHER), "124")
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

    def test_master_customer_without_open_documents_is_shown_with_no_debt(self):
        """No open document carries E: E's master customer is listed, owing 0.

        That is the customer a seller loads an advance for. The inactive
        master customer stays hidden.
        """
        connector = self._make_connector()
        self._refresh_debts(
            connector,
            [
                _invoice("00000001", "000123", 0, "E"),
                _invoice("00000002", "000124", 300, "G"),
            ],
        )
        self.assertEqual(self._listed(connector, "E"), ["123"])
        self.assertEqual(self._listed(connector, "000E"), ["123"])
        debt = self._debt(connector, "E", _ACME)
        self._assert_no_debt(debt, "123")
        summary = connector._format_customer_debt(debt)["summary"]
        self.assertEqual((summary["gross_total"], summary["net_total"]), (0.0, 0.0))

    def test_customer_of_neither_documents_nor_master_is_hidden(self):
        """DN issued nothing open and is no customer's master seller: unknown."""
        connector = self._make_connector()
        self._refresh_debts(connector, [_invoice("00000001", "000123", 300, "G")])
        self.assertEqual(self._listed(connector, "DN"), [])
        self.assertEqual(self._debt(connector, "DN", _ACME), _EMPTY)
        self.assertEqual(self._debt(connector, "DN", _OTHER), _EMPTY)

    def test_customer_with_only_the_sellers_credit_note_is_shown(self):
        """A credit note is an open document: its seller sees the balance in favour."""
        connector = self._make_connector()
        self._refresh_debts(connector, [_credit_note("00000001", "000124", -300, "E")])
        # 123 is listed too: the customer master assigns it to E.
        self.assertEqual(self._listed(connector, "E"), ["123", "124"])
        debt = self._debt(connector, "E", _OTHER)
        self.assertEqual(debt["invoices"], [])
        summary = connector._format_customer_debt(debt)["summary"]
        self.assertEqual(summary["net_total"], -300.0)

    def test_live_path_scopes_documents_by_seller_too(self):
        """Read live: the same rule, the master seller sees its customer owing 0."""
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
            nobody = connector._bejerman_get_customer_debt("DN", _ACME)
        self.assertEqual(mine["customer"]["customer_code"], "124")
        self.assertEqual(self._ids(mine), ["FHK|00004|A|00000001"])
        self.assertEqual(mine["credit_total"], 20.0)
        self._assert_no_debt(master_only, "123")
        self.assertEqual(nobody, _EMPTY)

    def test_live_path_master_customer_without_any_open_document(self):
        """Read live: a customer with no open row at all is its master's, owing 0."""
        connector = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        pack = {"clientes": _CLIENTES, "debts": []}
        with _patch_httpx(payloads_by_url={"*": pack}), _patch_today(connector):
            master = connector._bejerman_get_customer_debt("E", _ACME)
            other = connector._bejerman_get_customer_debt("G", _ACME)
        self._assert_no_debt(master, "123")
        self.assertEqual(other, _EMPTY)

    def test_shared_number_among_the_sellers_customers_is_ambiguous(self):
        """Two customers share ``0160``, one by E's documents, one by E's master.

        Both are E's, so ``0160`` reads as unknown, live and from the snapshot,
        and E names the customer by its full code. Never a guess.
        """
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
        self.assertEqual(self._listed(connector, "E"), ["AB0160", "NS0160"])
        with _patch_httpx(payloads_by_url={"*": pack}), _patch_today(connector):
            live = {}
            for seller, query in (
                ("E", "0160"),
                ("E", "NS0160"),
                ("E", "AB0160"),
                ("G", "AB0160"),
                ("DN", "0160"),
            ):
                live[f"{seller}:{query}"] = connector._bejerman_get_customer_debt(
                    seller, query
                )
        snapshot = self._make_connector(name="Snapshot")
        self._refresh(snapshot, payloads_by_url={"*": pack})
        self.assertEqual(live["E:0160"], _EMPTY)
        self.assertEqual(self._debt(snapshot, "E", "0160"), _EMPTY)
        self.assertEqual(
            self._debt(snapshot, "E", "NS0160")["customer"]["customer_code"], "NS0160"
        )
        self.assertEqual(live["E:NS0160"]["customer"]["customer_code"], "NS0160")
        self.assertEqual(self._ids(live["E:NS0160"]), ["FHK|00004|A|00000001"])
        self._assert_no_debt(live["E:AB0160"], "AB0160")
        self.assertEqual(self._ids(live["G:AB0160"]), ["FHK|00004|A|00000002"])
        self.assertEqual(live["DN:0160"], _EMPTY)

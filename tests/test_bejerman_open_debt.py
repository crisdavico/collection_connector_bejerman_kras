# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo.fields import Command
from odoo.tests import TransactionCase

from odoo.addons.collection.models.collection_common import privileged
from odoo.addons.collection.tests.common_connector import confirm_sheet
from odoo.addons.collection.tests.test_collection_payment import (
    create_internal_seller,
    dummy_customer_partner,
    ensure_enabled_seller,
)

from .test_bejerman_connector import BejermanTestMixin

_INVOICE = "FHK|00004|A|00001234"


class TestBejermanOpenDebt(BejermanTestMixin, TransactionCase):
    """The snapshot refresh (the 4-hour cron) recreates the Bejerman debt rows.

    A document applied on a closed sheet keeps its real open amount across
    refreshes: the rows are matched by their stable external id and the
    refresh time no longer releases anything.
    """

    def setUp(self):
        super().setUp()
        self.connector = self._make_connector()
        self.env.company.collection_connector_id = self.connector
        self.seller = create_internal_seller(self.env, "bej_open_debt_seller")
        ensure_enabled_seller(self.env, self.seller)

    def _closed_sheet(self, applied=100.0):
        """Apply ``applied`` to the invoice the snapshot shows at 500, then close."""
        partner = dummy_customer_partner(self.env, customer="BEJ-OPEN-DEBT")
        sheet = self.env["collection.sheet"].create({"seller_id": self.seller.id})
        allocation = self.env["collection.allocation"].create(
            {
                "sheet_id": sheet.id,
                "partner_id": partner.id,
                "line_ids": [
                    Command.create(
                        {
                            "external_invoice_id": _INVOICE,
                            "invoice_amount": 1000.0,
                            "invoice_balance": 500.0,
                            "amount_applied": applied,
                        }
                    )
                ],
            }
        )
        voucher = self.env["collection.voucher"].create(
            {
                "document_type": "bank_transfer",
                "partner_id": partner.id,
                "seller_id": self.seller.id,
                "sheet_id": sheet.id,
                "allocation_id": allocation.id,
                "company_id": sheet.company_id.id,
                "amount": applied,
                "document_id": f"bej-open-debt-{sheet.id}",
            }
        )
        with privileged("collection_audited_state"):
            voucher.write({"state": "validated_collections"})
        confirm_sheet(sheet)
        sheet.action_close()
        return sheet

    def _row(self):
        return self.env["collection.connector.bejerman.debt"].search(
            [
                ("connector_id", "=", self.connector.id),
                ("external_invoice_id", "=", _INVOICE),
            ]
        )

    def test_refresh_after_close_keeps_the_applied_amount_out(self):
        self._refresh(self.connector)
        sheet = self._closed_sheet()
        self.assertTrue(sheet.erp_entered_date)
        before = self._row()
        self._refresh(self.connector)
        after = self._row()
        self.assertNotEqual(before.id, after.id)
        self.assertGreaterEqual(
            self.connector.bejerman_snapshot_started_at, sheet.erp_entered_date
        )
        self.assertEqual(after.balance, 500.0)
        open_amounts = self.env["collection.allocation.line"]._open_amounts(
            {_INVOICE: after.balance}
        )
        self.assertEqual(open_amounts[_INVOICE], 400.0)

# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from datetime import timedelta

from odoo import fields
from odoo.fields import Command
from odoo.tests import TransactionCase

from odoo.addons.collection.models.collection_common import privileged
from odoo.addons.collection.tests.common_connector import confirm_sheet
from odoo.addons.collection.tests.test_collection_payment import (
    create_internal_seller,
    dummy_customer_partner,
    ensure_enabled_seller,
)

from .test_bejerman_connector import (
    BejermanTestMixin,
    _patch_httpx,
)

_INVOICE = "FHK|00004|A|00001234"
_SNAPSHOT_SCOPE = "collection_connector_bejerman_snapshot"


class TestBejermanErpEntry(BejermanTestMixin, TransactionCase):
    """A sheet entered in the ERP stops reserving after a refresh started later."""

    def setUp(self):
        super().setUp()
        self.connector = self._make_connector()
        self.env.company.collection_connector_id = self.connector
        self.seller = create_internal_seller(self.env, "bej_erp_entry_seller")
        ensure_enabled_seller(self.env, self.seller)

    def _closed_sheet(self, applied=100.0):
        partner = dummy_customer_partner(self.env, customer="BEJ-ERP-ENTRY")
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
                "document_id": f"bej-erp-entry-{sheet.id}",
            }
        )
        with privileged("collection_audited_state"):
            voucher.write({"state": "validated_collections"})
        confirm_sheet(sheet)
        sheet.action_close()
        return sheet

    def _reserved(self, connector=None):
        return (
            self.env["muk_mcp.mixin"]
            ._collection_reserved_amounts(
                [_INVOICE], connector=connector or self.connector
            )
            .get(_INVOICE, 0.0)
        )

    def test_snapshot_connector_answers_its_last_refresh_start(self):
        self.assertFalse(self.connector._collection_erp_reflected_since())
        self._refresh(self.connector)
        self.assertEqual(
            self.connector._collection_erp_reflected_since(),
            self.connector.bejerman_snapshot_started_at,
        )

    def test_failed_refresh_does_not_move_the_start(self):
        self._refresh(self.connector)
        started = self.connector.bejerman_snapshot_started_at
        with _patch_httpx(fail=("debt_view",)), self.assertLogs(level="WARNING"):
            self.env["collection.connector"]._bejerman_cron_refresh_snapshot()
        self.assertEqual(self.connector._collection_erp_reflected_since(), started)

    def test_live_connector_reads_the_erp_now(self):
        live = self._make_connector(
            name="Live", bejerman_debt_filter_field="CVE_CODCLI"
        )
        before = fields.Datetime.now().replace(microsecond=0)
        self.assertGreaterEqual(live._collection_erp_reflected_since(), before)

    def test_marked_sheet_is_released_by_the_next_refresh_only(self):
        self._refresh(self.connector)
        sheet = self._closed_sheet()
        self.assertTrue(sheet.erp_entered_date)
        self.assertEqual(self._reserved(), 100.0)
        # A refresh that started before the mark may predate the ERP entry.
        with privileged(_SNAPSHOT_SCOPE):
            self.connector.write(
                {
                    "bejerman_snapshot_started_at": sheet.erp_entered_date
                    - timedelta(seconds=1)
                }
            )
        self.assertEqual(self._reserved(), 100.0)
        with privileged(_SNAPSHOT_SCOPE):
            self.connector.write(
                {
                    "bejerman_snapshot_started_at": sheet.erp_entered_date
                    + timedelta(seconds=1)
                }
            )
        self.assertEqual(self._reserved(), 0.0)

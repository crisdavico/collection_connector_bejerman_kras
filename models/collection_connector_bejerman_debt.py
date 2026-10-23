# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import fields, models


class CollectionConnectorBejermanDebt(models.Model):
    _inherit = "collection.connector.bejerman.debt"

    # Untaxed share (NETO + NGEX) of amount and balance, from the sales VAT book
    # view; equal to them for quotations and invoices that view does not list.
    amount_untaxed = fields.Float(digits=(16, 2))
    balance_untaxed = fields.Float(digits=(16, 2))

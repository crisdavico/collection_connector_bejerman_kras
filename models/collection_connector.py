# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

from odoo.addons.collection_connector_bejerman.service import mapping as base_mapping

from ..service import mapping

_logger = logging.getLogger(__name__)

KRAS = "kras"


class CollectionConnector(models.Model):
    _inherit = "collection.connector"

    bejerman_profile = fields.Selection(
        selection_add=[(KRAS, "Kras")],
        ondelete={KRAS: "set default"},
    )
    bejerman_debt_scenario = fields.Char(
        string="Debt Scenario",
        groups="base.group_system,collection.group_collection_manager",
    )
    bejerman_debt_view = fields.Char(
        string="Debt View",
        groups="base.group_system,collection.group_collection_manager",
    )
    bejerman_quotation_kinds = fields.Char(
        default=mapping.DEFAULT_QUOTATION_KINDS,
        groups="base.group_system,collection.group_collection_manager",
    )
    bejerman_debt_filter_field = fields.Char(
        help="Query-view column (e.g. CVE_CODCLI) that filters the debt view by "
        "customer. When set, debt is read live per customer instead of from the "
        "snapshot.",
        groups="base.group_system,collection.group_collection_manager",
    )
    bejerman_untaxed_scenario = fields.Char(
        string="Untaxed Scenario",
        help="Scenario of the sales VAT book query view that gives each invoice's "
        "untaxed amount (NETO + NGEX). Leave it and the view empty to pay "
        "commission on the total.",
        groups="base.group_system,collection.group_collection_manager",
    )
    bejerman_untaxed_view = fields.Char(
        string="Untaxed View",
        help="Sales VAT book query view, as Query/View (e.g. Libro IVA Ventas/IA "
        "CRIS), read for the last 365 days. Only the open invoices it lists, and "
        "the quotations of the same 365 days, are kept as debt.",
        groups="base.group_system,collection.group_collection_manager",
    )

    @api.private
    def _bejerman_is_kras(self):
        # Read with sudo: the profile only picks the code path.
        return self.type == "bejerman" and self.sudo().bejerman_profile == KRAS

    @api.private
    def _bejerman_live_debt(self):
        """Kras connector whose debt view filters by customer: read it live."""
        return self._bejerman_is_kras() and bool(self.sudo().bejerman_debt_filter_field)

    @api.private
    def _bejerman_quotation_kinds(self):
        """Configured quotation voucher types; blank means the default ``PRE``."""
        self.ensure_one()
        return self.bejerman_quotation_kinds or mapping.DEFAULT_QUOTATION_KINDS

    @api.private
    def _bejerman_require_debt_query(self):
        """The debt view cannot be read until scenario and view are set."""
        self.ensure_one()
        missing = [
            self._fields[name].string
            for name in ("bejerman_debt_scenario", "bejerman_debt_view")
            if not (self[name] or "").strip()
        ]
        if missing:
            raise UserError(
                _(
                    "The Bejerman connector is not configured. "
                    "Missing settings: %(settings)s.",
                    settings=", ".join(missing),
                )
            )

    @api.private
    def _bejerman_untaxed_query(self):
        """(scenario, view) of the sales VAT book view, or None when not set.

        Optional: without it every debt keeps its total as the untaxed amount.
        Setting only one of the two is a configuration error.
        """
        self.ensure_one()
        names = ("bejerman_untaxed_scenario", "bejerman_untaxed_view")
        values = [(self[name] or "").strip() for name in names]
        if not any(values):
            return None
        missing = [
            self._fields[name].string
            for name, value in zip(names, values, strict=True)
            if not value
        ]
        if missing:
            raise UserError(
                _(
                    "The Bejerman connector is not configured. "
                    "Missing settings: %(settings)s.",
                    settings=", ".join(missing),
                )
            )
        return tuple(values)

    @api.private
    def _bejerman_check_snapshot_source(self):
        if not self._bejerman_is_kras():
            return super()._bejerman_check_snapshot_source()
        self._bejerman_require_debt_query()
        self._bejerman_untaxed_query()

    @api.private
    def _bejerman_read_snapshot(self, client, today, stats):
        """Customers, then `Saldos al dia`, then the sales VAT book when set.

        The sellers come from the same debt view download (every row, open or
        not), so listing sellers never costs a Bejerman request. When the sales VAT
        book view is set, it is read for the last 365 days: only the invoices it
        lists are kept, with their untaxed amount, and only the quotations issued
        in that window.
        """
        if not self._bejerman_is_kras():
            return super()._bejerman_read_snapshot(client, today, stats)
        untaxed_query = self._bejerman_untaxed_query()
        kinds = self._bejerman_quotation_kinds()
        customers = base_mapping.map_customer_rows(client.tablas.get_customers())
        debt_rows = client.queries.get_json_query_view(
            self.bejerman_debt_scenario.strip(), self.bejerman_debt_view.strip()
        )
        # Debt of disabled or unknown customers is not brought.
        codes = {row["customer_code"] for row in customers}
        debts = mapping.map_open_debt(
            debt_rows, kinds, customer_codes=codes, stats=stats
        )
        credits = mapping.map_customer_credits(debt_rows, customer_codes=codes)
        credit_notes = mapping.map_open_credits(debt_rows, customer_codes=codes)
        customers = [
            dict(row, credit_total=credits.get(row["customer_code"], 0.0))
            for row in customers
        ]
        sellers = mapping.map_debt_sellers(debt_rows)
        # The debt view is large: free it before the next download.
        del debt_rows
        shares = None
        if untaxed_query:
            shares = mapping.map_untaxed_shares(
                client.queries.get_json_query_view(
                    *untaxed_query, mapping.untaxed_date_filter(today)
                )
            )
        debts = mapping.apply_untaxed(
            debts, shares, stats, since=mapping.untaxed_window_start(today)
        )
        # Listed after the VAT book filter, which only concerns invoices.
        ids = {debt["external_invoice_id"] for debt in debts}
        debts += [
            note for note in credit_notes if note["external_invoice_id"] not in ids
        ]
        return customers, debts, sellers

    @api.private
    def _bejerman_log_snapshot_read(self, stats, today):
        result = super()._bejerman_log_snapshot_read(stats, today)
        if self._bejerman_is_kras():
            self._bejerman_log_untaxed_left_out(stats, today)
        return result

    @api.private
    def _bejerman_log_untaxed_left_out(self, stats, today):
        """Log the open debt left out by the sales VAT book view's 365-day window."""
        unmatched = stats.get("untaxed_unmatched", 0)
        if unmatched:
            _logger.warning(
                "Bejerman debt of connector %s: %s open invoices are not in the "
                "sales VAT book view of the last %s days and were left out (e.g. %s)",
                self.id,
                unmatched,
                mapping.UNTAXED_WINDOW_DAYS,
                ", ".join(stats.get("untaxed_unmatched_ids") or ()),
            )
        outdated = stats.get("quotations_outside_window", 0)
        if outdated:
            _logger.warning(
                "Bejerman debt of connector %s: %s open quotations were issued "
                "before %s and were left out",
                self.id,
                outdated,
                mapping.untaxed_window_start(today).isoformat(),
            )
        return unmatched + outdated

    @api.private
    def _bejerman_invoice_payload(self, debt, today):
        payload = super()._bejerman_invoice_payload(debt, today)
        if self._bejerman_is_kras():
            # Rows stored before the untaxed amount existed hold 0: their total.
            payload["amount_untaxed"] = debt.amount_untaxed or debt.amount
        return payload

    @api.private
    def _collection_erp_reflected_since(self):
        """The live filter path reads Bejerman at question time: now."""
        self.ensure_one()
        if self._bejerman_live_debt():
            return fields.Datetime.now()
        return super()._collection_erp_reflected_since()

    @api.private
    def _bejerman_get_customer_debt(self, external_seller_id, customer_identification):
        self.ensure_one()
        if self._bejerman_live_debt():
            return self.sudo()._bejerman_live_customer_debt(
                external_seller_id, customer_identification
            )
        return super()._bejerman_get_customer_debt(
            external_seller_id, customer_identification
        )

    @api.private
    def _bejerman_live_customer_debt(self, external_seller_id, customer_identification):
        """Read one customer's open debt live from the debt view, filtered.

        Used only when ``bejerman_debt_filter_field`` is set, i.e. the ERP view can
        filter server side. Runs on a sudo record: credentials stay admin-only.
        """
        self.ensure_one()
        self._bejerman_require_debt_query()
        field = self.bejerman_debt_filter_field

        def _get_debt(client):
            row = base_mapping.find_customer_row(
                client.tablas.get_customers(),
                external_seller_id,
                customer_identification,
            )
            if row is None:
                return {"customer": None, "invoices": []}
            customer = base_mapping.customer_from_row(row)
            rows = client.queries.get_json_query_view(
                self.bejerman_debt_scenario.strip(),
                self.bejerman_debt_view.strip(),
                mapping.debt_filter(field, base_mapping.erp_customer_code(row)),
            )
            today = self._bejerman_today()
            code = customer["customer_code"]
            stats = {}
            entries = mapping.map_open_debt(
                rows,
                self._bejerman_quotation_kinds(),
                customer_code=code,
                stats=stats,
            )
            self._bejerman_log_skipped_rows(stats)
            credits = mapping.map_customer_credits(rows, customer_code=code)
            return {
                "customer": customer,
                "invoices": [mapping.entry_to_invoice(e, today) for e in entries],
                "credit_total": credits.get(code, 0.0),
                # Read live: the figures are as of now.
                "as_of": fields.Datetime.to_string(fields.Datetime.now()),
            }

        return self._bejerman_call(_get_debt)

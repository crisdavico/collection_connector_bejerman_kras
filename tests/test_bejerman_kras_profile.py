# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo.exceptions import UserError
from odoo.tests import TransactionCase

from odoo.addons.collection_connector_bejerman.tests.common import (
    _NO_DEBT_SOURCE,
    _NOT_CONFIGURED,
)
from odoo.addons.collection_connector_bejerman_kras.hooks import post_init_hook

from .test_bejerman_connector import (
    _UNTAXED_SCENARIO,
    _UNTAXED_VIEW,
    BejermanTestMixin,
    _patch_httpx,
)

_KRAS_FIELDS = (
    "bejerman_debt_scenario",
    "bejerman_debt_view",
    "bejerman_debt_filter_field",
    "bejerman_untaxed_scenario",
    "bejerman_untaxed_view",
    "bejerman_quotation_kinds",
)


class TestBejermanKrasProfile(BejermanTestMixin, TransactionCase):
    """Only `kras` connectors read the Kras views; other customers are untouched."""

    def _standard(self, **overrides):
        return self._make_connector(
            bejerman_profile="standard",
            bejerman_untaxed_scenario=_UNTAXED_SCENARIO,
            bejerman_untaxed_view=_UNTAXED_VIEW,
            **overrides,
        )

    def test_profile_offers_kras(self):
        field = self.env["collection.connector"]._fields["bejerman_profile"]
        keys = {value for value, _label in field._description_selection(self.env)}
        self.assertEqual(keys, {"standard", "kras"})
        self.assertEqual(field.ondelete.get("kras"), "set default")

    def test_standard_connector_never_reads_the_kras_views(self):
        """Kras settings on a standard connector are ignored: no source, no call."""
        connector = self._standard()
        with _patch_httpx() as mock_post, self.assertRaises(UserError) as ctx:
            connector._bejerman_refresh_snapshot()
        self.assertEqual(str(ctx.exception), _NO_DEBT_SOURCE)
        mock_post.assert_not_called()
        debts = self.env["collection.connector.bejerman.debt"].search(
            [("connector_id", "=", connector.id)]
        )
        self.assertFalse(debts)

    def test_standard_connector_never_reads_debt_live(self):
        """The live filter is Kras's: a standard connector reads its snapshot."""
        connector = self._standard(bejerman_debt_filter_field="CVE_CODCLI")
        with _patch_httpx() as mock_post, self.assertRaises(UserError) as ctx:
            connector._bejerman_get_customer_debt("E", "20123456789")
        self.assertEqual(str(ctx.exception), _NOT_CONFIGURED)
        mock_post.assert_not_called()
        self.assertEqual(
            connector._collection_erp_reflected_since(),
            connector.bejerman_snapshot_started_at,
        )

    def test_kras_connector_reads_both_views_and_stores_untaxed_amounts(self):
        connector = self._make_connector(
            bejerman_untaxed_scenario=_UNTAXED_SCENARIO,
            bejerman_untaxed_view=_UNTAXED_VIEW,
        )
        self._refresh(connector)
        debts = self.env["collection.connector.bejerman.debt"].search(
            [
                ("connector_id", "=", connector.id),
                ("document_kind", "!=", "credit_note"),
            ]
        )
        self.assertTrue(debts)
        self.assertTrue(all(debt.amount_untaxed for debt in debts))

    def test_install_marks_every_existing_bejerman_connector_kras(self):
        """Connectors from before the split keep the Kras pipeline."""
        standard = self._standard()
        archived = self._standard(active=False)
        post_init_hook(self.env)
        self.assertEqual(standard.bejerman_profile, "kras")
        self.assertEqual(archived.bejerman_profile, "kras")

    def test_kras_settings_show_only_for_kras_connectors(self):
        arch = self._form_arch_for(self.env.ref("base.user_admin"))
        for name in _KRAS_FIELDS:
            node = arch.xpath(f"//field[@name='{name}']")
            self.assertTrue(node, name)
            self.assertIn("bejerman_profile != 'kras'", node[0].get("invisible"))
        self.assertTrue(arch.xpath("//field[@name='bejerman_profile']"))

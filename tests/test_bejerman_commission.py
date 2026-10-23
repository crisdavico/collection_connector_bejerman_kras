# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo.tests import TransactionCase

from .test_bejerman_connector import (
    BejermanTestMixin,
    _patch_httpx,
)

_BINDING = "collection.connector.seller.binding"
_MANAGER_GROUP = "collection.group_collection_manager"


class TestBejermanSellerCommission(BejermanTestMixin, TransactionCase):
    """The commission of a Bejerman seller binding is maintained in Odoo."""

    def _make_user(self, code, phone, *group_xmlids):
        """An internal user built the way the seller import builds them."""
        user = self.env.company._collection_create_connector_seller_users(
            [{"external_seller_id": code, "name": f"User {code}", "mobile": phone}]
        )
        groups = [self.env.ref(xmlid).id for xmlid in group_xmlids]
        if groups:
            user.write({"group_ids": [(4, group) for group in groups]})
        return user

    def _bind(self, connector, code, user, commission=0.0, enabled=True):
        binding = self.env[_BINDING].create(
            {
                "connector_id": connector.id,
                "external_seller_id": code,
                "user_id": user.id,
                "commission_percent": commission,
            }
        )
        if enabled:
            connector.company_id.write({"enabled_seller_ids": [(4, user.id)]})
        return binding

    def _commission_node(self, user):
        arch = self._form_arch_for(user)
        nodes = arch.xpath(
            "//field[@name='seller_binding_ids']//field[@name='commission_percent']"
        )
        self.assertEqual(len(nodes), 1)
        return nodes[0]

    def test_form_makes_commission_editable_for_bejerman_only(self):
        """The binding list is readonly unless the parent connector is Bejerman."""
        for code, phone, group in (
            ("VM", "5491100000011", _MANAGER_GROUP),
            ("VS", "5491100000012", "base.group_system"),
        ):
            user = self._make_user(code, phone, group)
            node = self._commission_node(user)
            self.assertEqual(node.get("readonly"), "parent.type != 'bejerman'")

    def test_admin_and_manager_can_write_the_commission_of_a_binding(self):
        """Both roles hold write access on the binding; no ACL change is needed."""
        connector = self._make_connector()
        user = self._make_user("DN", "5491155510001", "base.group_user")
        binding = self._bind(connector, "DN", user, 1.0)
        admin = self._make_user("ADM", "5491100000002", "base.group_system")
        manager = self._make_user("MGR", "5491100000001", _MANAGER_GROUP)
        binding.with_user(admin).write({"commission_percent": 4.0})
        self.assertEqual(binding.commission_percent, 4.0)
        binding.with_user(manager).write({"commission_percent": 6.5})
        self.assertEqual(binding.commission_percent, 6.5)

    def test_next_import_keeps_the_edited_commission(self):
        """The edit survives the import and is what list_sellers hands back."""
        connector = self._make_connector()
        company = connector.company_id
        company.collection_connector_id = connector
        with _patch_httpx() as mock_post:
            connector._bejerman_refresh_snapshot()
        refresh_calls = mock_post.call_count
        user = self._make_user("DN", "5491155510001", "base.group_user")
        binding = self._bind(connector, "DN", user, 0.0)
        manager = self._make_user("MGR", "5491100000001", _MANAGER_GROUP)
        binding.with_user(manager).write({"commission_percent": 9.25})
        with _patch_httpx() as mock_post:
            company._sync_enabled_sellers_from_connector()
            sellers = connector.list_sellers()["sellers"]
        mock_post.assert_not_called()
        self.assertGreater(refresh_calls, 0)
        self.assertEqual(binding.commission_percent, 9.25)
        self.assertEqual(company.enabled_seller_ids, user)
        self.assertEqual(
            {row["external_seller_id"]: row["commission_percent"] for row in sellers},
            {"DN": 9.25},
        )

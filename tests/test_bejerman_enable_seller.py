# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import TransactionCase
from odoo.tools.safe_eval import safe_eval

from odoo.addons.collection_connector_bejerman.tests.common import (
    _CLIENTES,
)

from .test_bejerman_connector import (
    _DEBT_ROWS,
    BejermanTestMixin,
    _debt_row,
    _patch_httpx,
)

_WIZARD = "collection.connector.bejerman.seller.enable"
_BINDING = "collection.connector.seller.binding"
_MANAGER_GROUP = "collection.group_collection_manager"


class TestBejermanEnableSeller(BejermanTestMixin, TransactionCase):
    """An administrator onboards a Bejerman seller without any import."""

    def setUp(self):
        super().setUp()
        self.connector = self._make_connector()
        self.company = self.connector.company_id
        self.company.collection_connector_id = self.connector
        pack = {
            "clientes": _CLIENTES,
            "debts": [
                *_DEBT_ROWS,
                _debt_row(
                    "FHK",
                    "00004",
                    "A",
                    "00007777",
                    "000123",
                    "2026-03-01",
                    10,
                    vendedor="000E",
                    vendedor_nombre="ANA PEREZ",
                ),
            ],
        }
        with _patch_httpx(payloads_by_url={"*": pack}) as self.refresh_post:
            self.connector._bejerman_refresh_snapshot()
        self.seller_e = self._seller("E")
        self.seller_dn = self._seller("DN")

    def _seller(self, code):
        return self.env["collection.connector.bejerman.seller"].search(
            [("connector_id", "=", self.connector.id), ("seller_code", "=", code)]
        )

    def _internal_user(self, code, phone=None, *group_xmlids):
        user = self.company._collection_create_connector_seller_users(
            [{"external_seller_id": code, "name": f"User {code}", "mobile": phone}]
        )
        if group_xmlids:
            user.write({"group_ids": [(4, self.env.ref(x).id) for x in group_xmlids]})
        return user

    def _manager(self):
        return self._internal_user("MGR", "5491100000001", _MANAGER_GROUP)

    def _enable(self, seller, as_user=None, **vals):
        vals.setdefault("commission_percent", 5.0)
        vals.setdefault("phone", "5491155510001")
        wizard = (
            self.env[_WIZARD]
            .with_context(default_seller_id=seller.id)
            .create(dict(vals, seller_id=seller.id))
        )
        if as_user:
            wizard = wizard.with_user(as_user)
        return wizard.action_confirm()

    def _binding(self, code):
        return self.env[_BINDING].search(
            [
                ("connector_id", "=", self.connector.id),
                ("external_seller_id", "=", code),
            ]
        )

    def test_enable_with_a_new_user_sets_binding_phone_commission_and_enabled(self):
        """One step: user created from the Bejerman name, bound, enabled, listed."""
        with _patch_httpx() as mock_post:
            self._enable(
                self.seller_e,
                user_mode="new",
                new_user_name="Ana Perez",
                commission_percent=7.5,
            )
        mock_post.assert_not_called()
        binding = self._binding("E")
        self.assertEqual(binding.commission_percent, 7.5)
        user = binding.user_id
        self.assertEqual(user.name, "Ana Perez")
        self.assertEqual(user.login, f"collection.{self.company.id}.E")
        self.assertFalse(user.share)
        self.assertEqual(user.partner_id.phone, "5491155510001")
        self.assertIn(user, self.company.enabled_seller_ids)
        self.assertTrue(self.seller_e.is_enabled)
        self.assertEqual(self.seller_e.user_id, user)
        self.assertEqual(self.seller_e.commission_percent, 7.5)
        self.assertFalse(self.seller_dn.is_enabled)

    def test_enabled_seller_reads_its_customers_from_the_snapshot(self):
        """The generic list_customers path works right after enabling."""
        self._enable(self.seller_e, user_mode="new", new_user_name="Ana Perez")
        user = self._binding("E").user_id
        with _patch_httpx() as mock_post:
            customers = self.connector.list_customers(user.id)["customers"]
        mock_post.assert_not_called()
        self.assertEqual([c["customer_code"] for c in customers], ["123"])

    def test_enable_with_an_existing_user(self):
        """An existing internal user is bound; its phone is rewritten."""
        user = self._internal_user("OLD", "5491100000099")
        self._enable(
            self.seller_e,
            user_mode="existing",
            user_id=user.id,
            phone="5491155510001",
            commission_percent=3.0,
        )
        binding = self._binding("E")
        self.assertEqual(binding.user_id, user)
        self.assertEqual(user.partner_id.phone, "5491155510001")
        self.assertIn(user, self.company.enabled_seller_ids)
        self.assertEqual(binding.commission_percent, 3.0)

    def test_enable_again_updates_the_existing_binding(self):
        """Re-running the wizard changes commission and phone, not the binding."""
        self._enable(self.seller_e, user_mode="new", new_user_name="Ana")
        binding = self._binding("E")
        user = binding.user_id
        self._enable(
            self.seller_e,
            user_mode="existing",
            user_id=user.id,
            phone="5491155510002",
            commission_percent=9.0,
        )
        self.assertEqual(self._binding("E"), binding)
        self.assertEqual(binding.commission_percent, 9.0)
        self.assertEqual(user.partner_id.phone, "5491155510002")

    def test_user_bound_to_another_code_is_rejected(self):
        """A user can serve one seller code per connector."""
        self._enable(self.seller_e, user_mode="new", new_user_name="Ana")
        user = self._binding("E").user_id
        with self.assertRaisesRegex(UserError, "already bound to seller E"):
            self._enable(
                self.seller_dn,
                user_mode="existing",
                user_id=user.id,
                phone="5491155510009",
            )
        self.assertFalse(self._binding("DN"))

    def test_duplicate_phone_of_another_enabled_seller_is_rejected(self):
        """The import refuses duplicate phones, so enabling refuses them too."""
        self._enable(self.seller_e, user_mode="new", new_user_name="Ana")
        enabled_before = self.company.enabled_seller_ids
        with self.assertRaisesRegex(UserError, "phone"):
            self._enable(
                self.seller_dn,
                user_mode="new",
                new_user_name="Dino",
                phone="+54 9 11 5551-0001",
            )
        self.assertFalse(self._binding("DN"))
        self.assertEqual(self.company.enabled_seller_ids, enabled_before)

    def test_blank_phone_and_bad_commission_are_rejected(self):
        for vals in ({"phone": " "}, {"commission_percent": 101.0}):
            with self.subTest(vals=vals), self.assertRaises(UserError):
                self._enable(
                    self.seller_e, user_mode="new", new_user_name="Ana", **vals
                )
        self.assertFalse(self._binding("E"))

    def test_disable_removes_enabled_and_keeps_the_binding(self):
        self._enable(self.seller_e, user_mode="new", new_user_name="Ana")
        binding = self._binding("E")
        self.seller_e.action_bejerman_disable_seller()
        self.assertTrue(binding.exists())
        self.assertNotIn(binding.user_id, self.company.enabled_seller_ids)
        self.assertFalse(self.seller_e.is_enabled)
        with self.assertRaises(UserError):
            self.seller_e.action_bejerman_disable_seller()

    def test_manager_can_enable_and_disable_a_non_manager_cannot(self):
        manager = self._manager()
        plain = self._internal_user("PLAIN", "5491100000077", "base.group_user")
        with self.assertRaises(AccessError):
            self.seller_e.with_user(plain).action_bejerman_enable_seller()
        with self.assertRaises(AccessError):
            self._enable(
                self.seller_e, as_user=plain, user_mode="new", new_user_name="Ana"
            )
        self.assertFalse(self._binding("E"))
        action = self.seller_e.with_user(manager).action_bejerman_enable_seller()
        self.assertEqual(action["res_model"], _WIZARD)
        self.assertEqual(action["context"]["default_seller_id"], self.seller_e.id)
        self._enable(
            self.seller_e, as_user=manager, user_mode="new", new_user_name="Ana"
        )
        self.assertTrue(self._binding("E"))
        with self.assertRaises(AccessError):
            self.seller_e.with_user(plain).action_bejerman_disable_seller()
        self.seller_e.with_user(manager).action_bejerman_disable_seller()
        self.assertFalse(self.seller_e.is_enabled)

    def _outsider(self):
        """An internal user of another company only."""
        other = self._create_company("Enable Outside Co")
        user = self._create_user("bejerman_outsider", "base.group_user")
        user.write({"company_id": other.id, "company_ids": [Command.set(other.ids)]})
        return user

    def test_user_choices_are_limited_to_users_of_the_company(self):
        """The picker offers internal users that already belong to the company."""
        outsider = self._outsider()
        member = self._internal_user("MEMBER", "5491100000055")
        domain = self.env[_WIZARD]._fields["user_id"].domain
        allowed = self.env["res.users"].search(
            safe_eval(domain, {"company_id": self.company.id})
        )
        self.assertIn(member, allowed)
        self.assertNotIn(outsider, allowed)
        self.assertEqual(
            self.env[_WIZARD]
            .with_context(default_seller_id=self.seller_e.id)
            .new({"seller_id": self.seller_e.id})
            .company_id,
            self.company,
        )

    def test_manager_cannot_grant_another_user_access_to_the_company(self):
        """Enabling never adds the company to a user outside it, not even via RPC."""
        manager = self._manager()
        outsider = self._outsider()
        companies_before = outsider.company_ids
        with self.assertRaisesRegex(UserError, "administrator"):
            self._enable(
                self.seller_e,
                as_user=manager,
                user_mode="existing",
                user_id=outsider.id,
            )
        self.assertEqual(outsider.company_ids, companies_before)
        self.assertNotIn(self.company, outsider.company_ids)
        self.assertFalse(self._binding("E"))
        self.assertNotIn(outsider, self.company.enabled_seller_ids)

    def test_manager_cannot_overwrite_another_users_phone(self):
        """Changing a user's contact phone is an administrator's call."""
        manager = self._manager()
        user = self._internal_user("OLD", "5491100000099")
        with self.assertRaisesRegex(UserError, "administrator"):
            self._enable(
                self.seller_e,
                as_user=manager,
                user_mode="existing",
                user_id=user.id,
                phone="5491155510001",
            )
        self.assertEqual(user.partner_id.phone, "5491100000099")
        self.assertFalse(self._binding("E"))
        self.assertNotIn(user, self.company.enabled_seller_ids)

    def test_manager_enables_a_company_user_whose_phone_already_matches(self):
        manager = self._manager()
        user = self._internal_user("OLD", "5491155510001")
        self._enable(
            self.seller_e,
            as_user=manager,
            user_mode="existing",
            user_id=user.id,
            phone="+54 9 11 5551-0001",
            commission_percent=2.5,
        )
        binding = self._binding("E")
        self.assertEqual(binding.user_id, user)
        self.assertEqual(binding.commission_percent, 2.5)
        self.assertEqual(user.partner_id.phone, "5491155510001")
        self.assertIn(user, self.company.enabled_seller_ids)

    def test_only_bejerman_connectors(self):
        other = self.env["collection.connector"].create({"name": "Typeless"})
        seller = self.env["collection.connector.bejerman.seller"].create(
            {"connector_id": other.id, "seller_code": "X", "name": "X"}
        )
        with self.assertRaises(UserError):
            seller.action_bejerman_enable_seller()
        with self.assertRaises(UserError):
            seller.action_bejerman_disable_seller()

    def test_wizard_defaults_come_from_the_snapshot_and_binding(self):
        wizard = (
            self.env[_WIZARD]
            .with_context(default_seller_id=self.seller_e.id)
            .default_get(["user_mode", "new_user_name", "commission_percent"])
        )
        self.assertEqual(wizard["user_mode"], "new")
        self.assertEqual(wizard["new_user_name"], "ANA PEREZ")
        self._enable(
            self.seller_e,
            user_mode="new",
            new_user_name="Ana",
            phone="5491155510001",
            commission_percent=4.0,
        )
        wizard = (
            self.env[_WIZARD]
            .with_context(default_seller_id=self.seller_e.id)
            .default_get(["user_mode", "user_id", "phone", "commission_percent"])
        )
        self.assertEqual(wizard["user_mode"], "existing")
        self.assertEqual(wizard["user_id"], self._binding("E").user_id.id)
        self.assertEqual(wizard["phone"], "5491155510001")
        self.assertEqual(wizard["commission_percent"], 4.0)

    def test_generic_import_keeps_everything_afterwards(self):
        """After enabling, an import lists the seller and changes nothing."""
        self._enable(
            self.seller_e,
            user_mode="new",
            new_user_name="Ana",
            commission_percent=7.5,
        )
        binding = self._binding("E")
        user = binding.user_id
        users_before = self.env["res.users"].search_count([])
        with _patch_httpx() as mock_post:
            self.company._sync_enabled_sellers_from_connector()
        mock_post.assert_not_called()
        self.assertEqual(self._binding("E"), binding)
        self.assertEqual(binding.commission_percent, 7.5)
        self.assertEqual(binding.user_id, user)
        self.assertEqual(user.partner_id.phone, "5491155510001")
        self.assertEqual(self.company.enabled_seller_ids, user)
        self.assertEqual(self.env["res.users"].search_count([]), users_before)
        self.assertEqual(
            [
                row["external_seller_id"]
                for row in self.connector.list_sellers()["sellers"]
            ],
            ["E"],
        )

    def test_sellers_tab_and_buttons_are_in_the_connector_form(self):
        manager = self._manager()
        arch = self._form_arch_for(manager)
        buttons = arch.xpath(
            "//field[@name='bejerman_seller_ids']//button"
            "[@name='action_bejerman_enable_seller']"
        )
        self.assertEqual(len(buttons), 1)
        self.assertTrue(
            arch.xpath(
                "//field[@name='bejerman_seller_ids']//button"
                "[@name='action_bejerman_disable_seller']"
            )
        )

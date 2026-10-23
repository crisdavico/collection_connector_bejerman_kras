# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).


def post_init_hook(env):
    """Every Bejerman connector that exists when this addon arrives is Kras's.

    The Kras debt pipeline used to be the base addon's only behaviour, so the
    connectors created before the split keep it.
    """
    connectors = (
        env["collection.connector"]
        .with_context(active_test=False)
        .search([("type", "=", "bejerman")])
    )
    connectors.write({"bejerman_profile": "kras"})

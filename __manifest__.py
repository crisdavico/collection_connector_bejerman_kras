# Copyright 2026 Seedian
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
{
    "name": "Collections Bejerman Connector for Kras",
    "version": "19.0.1.0.0",
    "category": "Sales/CRM",
    "summary": "Kras debt, sellers and untaxed commission base from Bejerman views",
    "author": "Seedian",
    "website": "https://seedian.ai",
    "depends": ["collection_connector_bejerman"],
    "data": [
        "views/collection_connector_views.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
    "license": "AGPL-3",
}

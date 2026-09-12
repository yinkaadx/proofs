"""The tool registry: the one place a new tool is declared.

Adding a tool to the hub is a single entry here plus its package under
`tools/`. The hub builds its navigation and landing page from this list, so
nothing else needs editing and no redeploy is required: a push is enough.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tool:
    key: str           # URL path segment, stable once published
    title: str         # shown in navigation and on the card
    icon: str          # emoji shown in navigation and on the card
    tagline: str       # one line on the landing card
    audience: str      # who it is for, shown as a pill
    render: Callable[[], None]


def all_tools() -> list[Tool]:
    """Every tool published by the hub, in landing page order."""
    from tools.multi_channel_inventory_sync.page import render as multi_channel_inventory_sync
    from tools.netsuite_hubspot_sync.page import render as netsuite_hubspot_sync
    from tools.wp_form_debugger.page import render as wp_form_debugger

    return [
        Tool(
            key="wp-form-debugger",
            title="WP Form Debugger",
            icon="🛠",
            tagline=(
                "Find out why a WordPress form stopped submitting or stopped "
                "delivering, then copy the exact PHP, JavaScript and "
                "wp-config.php fix."
            ),
            audience="WordPress support",
            render=wp_form_debugger,
        ),
        Tool(
            key="netsuite-hubspot-sync",
            title="NetSuite HubSpot Idempotent Sync Console",
            icon="🔁",
            tagline=(
                "Deal sync simulator, account matching logic, SHA256 idempotency "
                "ledger, and SharePoint audit feed."
            ),
            audience="Revenue operations",
            render=netsuite_hubspot_sync,
        ),
        Tool(
            key="multi-channel-inventory-sync",
            title="Multi Channel Inventory Sync Engine",
            icon="\U0001F4E6",
            tagline=(
                "Cross platform SKU mapping, automated stock deduction, and "
                "negative inventory prevention for Amazon, eBay, and Shopify."
            ),
            audience="Ecommerce operations",
            render=multi_channel_inventory_sync,
        ),
    ]

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
    from tools.pod_automation_router.page import render as pod_automation_router
    from tools.reventure_conversion_engine.page import render as reventure_conversion_engine
    from tools.ten_dlc_compliance_validator.page import render as ten_dlc_compliance_validator
    from tools.web3_smart_escrow_console.page import render as web3_smart_escrow_console
    from tools.retreat_funnel_redundancy_guard.page import render as retreat_funnel_redundancy_guard
    from tools.sharepoint_zero_trust_simulator.page import render as sharepoint_zero_trust_simulator
    from tools.tv_mt5_bridge_diagnostic.page import render as tv_mt5_bridge_diagnostic
    from tools.zero_trust_rmm_console.page import render as zero_trust_rmm_console
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
        Tool(
            key="zero-trust-rmm-console",
            title="Zero Trust Remote Access Console",
            icon="\U0001F510",
            tagline=(
                "Multitenant RBAC simulator, MFA enforcement ledger, and Ad hoc "
                "session code generator."
            ),
            audience="Managed IT services",
            render=zero_trust_rmm_console,
        ),
        Tool(
            key="pod-automation-router",
            title="Print on Demand Automation Router",
            icon="\U0001F5A8",
            tagline=(
                "WooCommerce order payload routing, Printful API fulfillment "
                "simulation, and multi channel tracking synchronization."
            ),
            audience="Ecommerce operations",
            render=pod_automation_router,
        ),
        Tool(
            key="tv-mt5-bridge-diagnostic",
            title="TradingView MT5 Bridge Diagnostic Console",
            icon="\u23F1",
            tagline=(
                "Webhook payload inspector, latency analyzer, and execution "
                "drift monitor for TradingView to MT5 synchronization."
            ),
            audience="Algorithmic trading",
            render=tv_mt5_bridge_diagnostic,
        ),
        Tool(
            key="sharepoint-zero-trust-simulator",
            title="Secure SharePoint Architecture Console",
            icon="\U0001F6E1",
            tagline=(
                "Entra ID authentication simulator, role based access matrix, "
                "and secure deployment checklist."
            ),
            audience="Microsoft 365 security",
            render=sharepoint_zero_trust_simulator,
        ),
        Tool(
            key="retreat-funnel-redundancy-guard",
            title="Retreat Funnel Redundancy Guard",
            icon="\U0001F33F",
            tagline=(
                "FG Funnels webhook simulator, automated Slack alert routing, "
                "and webinar metrics tracking."
            ),
            audience="Coaching and retreat marketing",
            render=retreat_funnel_redundancy_guard,
        ),
        Tool(
            key="web3-smart-escrow-console",
            title="Web3 SmartEscrow Architecture Console",
            icon="\u26D3",
            tagline=(
                "EVM SmartEscrow deployment simulator, Foundry fuzzing ledger, "
                "and event indexing stream."
            ),
            audience="Blockchain engineering",
            render=web3_smart_escrow_console,
        ),
        Tool(
            key="ten-dlc-compliance-validator",
            title="10DLC Campaign Registry Compliance Validator",
            icon="\U0001F4F2",
            tagline=(
                "A2P 10DLC brand and campaign registration simulator ensuring "
                "exact TCR approval standards."
            ),
            audience="SMS compliance",
            render=ten_dlc_compliance_validator,
        ),
        Tool(
            key="reventure-conversion-engine",
            title="Mobile Conversion & Release Engine",
            icon="\U0001F4F1",
            tagline=(
                "Feature flag A/B test controller, App Store rating logic, and "
                "Stripe subscription webhook pipeline."
            ),
            audience="Mobile product engineering",
            render=reventure_conversion_engine,
        ),
    ]

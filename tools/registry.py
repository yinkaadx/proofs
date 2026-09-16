"""The tool registry: the one place a new tool is declared.

Adding a tool to the hub is a single entry here plus its package under
`tools/`. The hub builds its navigation and landing page from this list, so
nothing else needs editing and no redeploy is required: a push is enough.
"""

from __future__ import annotations

import os
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


def _padding() -> int:
    """How many synthetic tools to append, for layout tests only.

    The sidebar once hid each tool's instructions behind a list that grew by a
    row every week. A test that measures sixteen tools would have passed every
    week until the week it did not, so the guard has to prove the layout is
    independent of the count rather than acceptable at today's count. This hook
    is how it grows the list to forty without inventing forty tools.
    """
    raw = os.environ.get("TOOLBENCH_PAD_TOOLS", "")
    return int(raw) if raw.isdigit() and 0 < int(raw) <= 200 else 0


def _placeholder(title: str):
    def render() -> None:
        import streamlit as st

        st.markdown(f"### {title}")
        st.caption("A synthetic tool. It exists only to lengthen the tool list "
                   "while a layout test measures whether that moves anything.")
        with st.sidebar:
            st.subheader("How to use")
            st.markdown("Nothing to use. This tool is scaffolding for a test.")
    return render


def all_tools() -> list[Tool]:
    """Every tool published by the hub, in landing page order."""
    from tools.multi_channel_inventory_sync.page import render as multi_channel_inventory_sync
    from tools.aerial_insights_qa_console.page import render as aerial_insights_qa_console
    from tools.askew_suit_engine.page import render as askew_suit_engine
    from tools.hubspot_b2b_network_architect.page import render as hubspot_b2b_network_architect
    from tools.enterprise_ai_pipeline_console.page import render as enterprise_ai_pipeline_console
    from tools.hipaa_tracking_audit_console.page import render as hipaa_tracking_audit_console
    from tools.mobile_qa_bug_bash_console.page import render as mobile_qa_bug_bash_console
    from tools.creator_membership_architecture_console.page import render as creator_membership_architecture_console
    from tools.cloudflare_r2_transfer_optimizer.page import render as cloudflare_r2_transfer_optimizer
    from tools.erpnext_lab_inventory_console.page import render as erpnext_lab_inventory_console
    from tools.supply_chain_qa_console.page import render as supply_chain_qa_console
    from tools.sports_betting_algo_console.page import render as sports_betting_algo_console
    from tools.megaport_bgp_rtbh_console.page import render as megaport_bgp_rtbh_console
    from tools.brokerage_qa_workflow_console.page import render as brokerage_qa_workflow_console
    from tools.appsec_threat_modeling_console.page import render as appsec_threat_modeling_console
    from tools.saas_multitenant_pg_console.page import render as saas_multitenant_pg_console
    from tools.core_web_vitals_optimizer_console.page import render as core_web_vitals_optimizer_console
    from tools.irish_wine_distribution_console.page import render as irish_wine_distribution_console
    from tools.toh_systems_launch_console.page import render as toh_systems_launch_console
    from tools.sgtm_claude_agent_console.page import render as sgtm_claude_agent_console
    from tools.airtable_whatsapp_automation_guard.page import render as airtable_whatsapp_automation_guard
    from tools.m365_intranet_architecture_console.page import render as m365_intranet_architecture_console
    from tools.pod_automation_router.page import render as pod_automation_router
    from tools.wastetab_dispatch_engine.page import render as wastetab_dispatch_engine
    from tools.reventure_conversion_engine.page import render as reventure_conversion_engine
    from tools.ten_dlc_compliance_validator.page import render as ten_dlc_compliance_validator
    from tools.web3_smart_escrow_console.page import render as web3_smart_escrow_console
    from tools.retreat_funnel_redundancy_guard.page import render as retreat_funnel_redundancy_guard
    from tools.sharepoint_zero_trust_simulator.page import render as sharepoint_zero_trust_simulator
    from tools.tv_mt5_bridge_diagnostic.page import render as tv_mt5_bridge_diagnostic
    from tools.zero_trust_rmm_console.page import render as zero_trust_rmm_console
    from tools.cloud_security_wif_console.page import render as cloud_security_wif_console
    from tools.netsuite_hubspot_sync.page import render as netsuite_hubspot_sync
    from tools.pipedrive_integration_engine.page import render as pipedrive_integration_engine
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
            key="pipedrive-integration-engine",
            title="Pipedrive API & Integration Console",
            icon="\U0001F514",
            tagline=(
                "Direct webhook dispatch without Zapier, Sinch AI SMS "
                "threading, and Power BI incremental sync ledger."
            ),
            audience="Sales operations",
            render=pipedrive_integration_engine,
        ),
        Tool(
            key="cloud-security-wif-console",
            title="EHR Cloud Security & WIF Architecture Console",
            icon="\U0001F511",
            tagline=(
                "Azure to GCP Workload Identity Federation simulator, GCS "
                "Credential Access Boundary evaluator, and Key Vault policy "
                "matrix."
            ),
            audience="Security architecture",
            render=cloud_security_wif_console,
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
        Tool(
            key="airtable-whatsapp-automation-guard",
            title="Airtable WhatsApp Automation Guard",
            icon="\U0001F4AC",
            tagline=(
                "Idempotent Make webhook simulator, Twilio payload router, "
                "and error handling guard."
            ),
            audience="Automation and operations",
            render=airtable_whatsapp_automation_guard,
        ),
        Tool(
            key="m365-intranet-architecture-console",
            title="Microsoft 365 Intranet Architecture Console",
            icon="\U0001F3E2",
            tagline=(
                "SharePoint role simulator, Power Apps PTO and timesheet "
                "logic, and Claude AI project insights."
            ),
            audience="Microsoft 365 consulting",
            render=m365_intranet_architecture_console,
        ),
        Tool(
            key="wastetab-dispatch-engine",
            title="WasteTab Logistics & Financial Engine",
            icon="\U0001F69B",
            tagline=(
                "Regional dispatch routing, GoDaddy gross up fee calculator, "
                "and emergency safety valve simulator."
            ),
            audience="Waste logistics operations",
            render=wastetab_dispatch_engine,
        ),
        Tool(
            key="aerial-insights-qa-console",
            title="Aerial Insights QA & Production Console",
            icon="\U0001F6F0",
            tagline=(
                "Background worker diagnostics, Prisma connection pooling "
                "auditor, and Stripe idempotency ledger."
            ),
            audience="Platform engineering",
            render=aerial_insights_qa_console,
        ),
        Tool(
            key="askew-suit-engine",
            title="ASKEW Bespoke Pricing Engine",
            icon="\U0001F9F5",
            tagline=(
                "Visual base plus upgrade configurator, fifty percent split "
                "deposit logic, and customer measurement database simulator."
            ),
            audience="Bespoke tailoring",
            render=askew_suit_engine,
        ),
        Tool(
            key="hubspot-b2b-network-architect",
            title="HubSpot B2B Network Architect",
            icon="\U0001F578",
            tagline=(
                "Gridwise entity relationship model, network categorization "
                "logic, and LinkedIn deduplication ledger."
            ),
            audience="RevOps and CRM architecture",
            render=hubspot_b2b_network_architect,
        ),
        Tool(
            key="enterprise-ai-pipeline-console",
            title="Enterprise AI Pipeline Console",
            icon="\U0001F9E0",
            tagline=(
                "Intelligent document processing simulator, LLM schema "
                "extractor, background queue worker, and PostgreSQL query "
                "optimizer."
            ),
            audience="AI platform engineering",
            render=enterprise_ai_pipeline_console,
        ),
        Tool(
            key="hipaa-tracking-audit-console",
            title="HIPAA Web & Tracking Audit Console",
            icon="\U0001FA7A",
            tagline=(
                "OCR tracking guidance auditor, Google Tag Manager PHI leak "
                "detector, SimplePractice pre fill risk analyzer, and BAA "
                "matrix."
            ),
            audience="Behavioural health practices",
            render=hipaa_tracking_audit_console,
        ),
        Tool(
            key="mobile-qa-bug-bash-console",
            title="Mobile QA Bug Bash Console",
            icon="\U0001F41E",
            tagline=(
                "Mobile defect reporting console, review triage matrix, "
                "Asana to Slack dispatcher, and Figma gap auditor."
            ),
            audience="Mobile QA and release",
            render=mobile_qa_bug_bash_console,
        ),
        Tool(
            key="creator-membership-architecture-console",
            title="Premium Creator Membership Console",
            icon="\U0001F399",
            tagline=(
                "Editorial paywall preview simulator, audio and podcast "
                "player feed, member tier gating, and Stripe webhook handler."
            ),
            audience="Paid publications and creators",
            render=creator_membership_architecture_console,
        ),
        Tool(
            key="cloudflare-r2-transfer-optimizer",
            title="Cloudflare R2 Transfer Optimization Console",
            icon="\U00002601",
            tagline=(
                "Cloudflare R2 presigned URL transfer simulator, multipart "
                "throughput calculator, and edge latency analyzer."
            ),
            audience="Storage and platform engineering",
            render=cloudflare_r2_transfer_optimizer,
        ),
        Tool(
            key="erpnext-lab-inventory-console",
            title="ERPNext Laboratory & Equipment Console",
            icon="\U00002697",
            tagline=(
                "Cloudflare Tunnel ingress simulator, ERPNext Frappe DocType "
                "schema linker for equipment and chemical SDS, and automated "
                "backup matrix."
            ),
            audience="Laboratory operations and ERP",
            render=erpnext_lab_inventory_console,
        ),
        Tool(
            key="supply-chain-qa-console",
            title="Supply Chain QA & Regression Console",
            icon="\U0001F4CB",
            tagline=(
                "Supply chain planning test case matrix, regression defect "
                "generator, and test execution tracker."
            ),
            audience="QA and supply chain planning",
            render=supply_chain_qa_console,
        ),
        Tool(
            key="sports-betting-algo-console",
            title="Sports Betting Algorithm & Edge Console",
            icon="\U0001F3B2",
            tagline=(
                "Sports betting quantitative edge model, referee bias factor "
                "analysis, ensemble meta predictor, and Kelly backtesting "
                "engine."
            ),
            audience="Quantitative modelling",
            render=sports_betting_algo_console,
        ),
        Tool(
            key="megaport-bgp-rtbh-console",
            title="Megaport BGP RTBH & Blackhole Console",
            icon="\U0001F573",
            tagline=(
                "Cisco BGP route map generator for Megaport RTBH community "
                "tagging, /32 prefix validator, and traffic drop simulator."
            ),
            audience="Network operations",
            render=megaport_bgp_rtbh_console,
        ),
        Tool(
            key="brokerage-qa-workflow-console",
            title="Real Estate Brokerage QA & Workflow Console",
            icon="\U0001F3D8",
            tagline=(
                "US real estate brokerage workflow validator, Marker.io "
                "defect report generator, and transaction compliance auditor."
            ),
            audience="Real estate operations and QA",
            render=brokerage_qa_workflow_console,
        ),
        Tool(
            key="appsec-threat-modeling-console",
            title="AppSec Threat Modeling & Research Console",
            icon="\U0001F3AF",
            tagline=(
                "Application security vulnerability analyzer, threat modeling "
                "evaluator, and product recommendation translator."
            ),
            audience="Application security and product",
            render=appsec_threat_modeling_console,
        ),
        Tool(
            key="saas-multitenant-pg-console",
            title="SaaS Multi Tenant PostgreSQL Console",
            icon="\U0001F5C4",
            tagline=(
                "PostgreSQL Row Level Security simulator, cross tenant query "
                "isolation tester, and multi tenancy architecture matrix."
            ),
            audience="Platform and database engineering",
            render=saas_multitenant_pg_console,
        ),
        Tool(
            key="core-web-vitals-optimizer-console",
            title="Core Web Vitals & Speed Optimizer Console",
            icon="\U0001F6A6",
            tagline=(
                "Core Web Vitals diagnostic engine, INP and LCP latency "
                "analyzer, and before vs after mobile score benchmark."
            ),
            audience="Web performance and SEO",
            render=core_web_vitals_optimizer_console,
        ),
        Tool(
            key="irish-wine-distribution-console",
            title="Irish Wine Wholesale Operating Console",
            icon="\U0001F377",
            tagline=(
                "Bonded stock tracking, Irish excise duty and VAT calculator, "
                "multi tier pricing engine, and 3PL dispatch formatter."
            ),
            audience="Drinks wholesale and distribution",
            render=irish_wine_distribution_console,
        ),
        Tool(
            key="toh-systems-launch-console",
            title="TOH Systems & Commerce Launch Console",
            icon="\U0001F9ED",
            tagline=(
                "System of record architecture map, stale data prevention "
                "dashboard, and supervised customer care agent pilot."
            ),
            audience="Commerce and operations launch",
            render=toh_systems_launch_console,
        ),
        Tool(
            key="sgtm-claude-agent-console",
            title="Server Side Tracking & Claude Agent Console",
            icon="\U0001F4E1",
            tagline=(
                "Stape sGTM event router, Meta CAPI deduplication validator, "
                "Consent Mode v2 tester, and Claude skill manifest generator."
            ),
            audience="Analytics and marketing engineering",
            render=sgtm_claude_agent_console,
        ),
    ] + [
        Tool(
            key=f"synthetic-{index:02d}",
            title=f"Synthetic Tool {index:02d}",
            icon="\u2699",
            tagline="Scaffolding for a layout test, not a real tool.",
            audience="Tests",
            render=_placeholder(f"Synthetic Tool {index:02d}"),
        )
        for index in range(1, _padding() + 1)
    ]

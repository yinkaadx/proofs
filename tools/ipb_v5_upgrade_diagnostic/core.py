"""IPB V5 Upgrade Diagnostic Console: the engine.

Invision Community 5 is not a point release. Three things that worked in 4
stop working, and each one fails in a different way.

1. The plugin system is gone. A plugin is not upgraded, it is rewritten as an
   application, and its code hooks become extension points. A scanner that
   only reports "incompatible" wastes the developer's afternoon; this one
   names the hook it found and the extension point that replaces it.
2. Themes stop carrying hardcoded values. A colour typed into a template in 4
   has to become a custom property in 5, and the exact token names belong to
   the running theme rather than to any document. So every declaration this
   mapper emits carries the original value as the fallback argument, which
   means a token name that turns out to be wrong renders exactly as it did
   before instead of rendering as nothing.
3. Pages records live in per database tables with site specific column names,
   so a recovery query cannot be written from a table name alone. The first
   query discovers the column map and the second one uses it. Anything that
   skips the first step is guessing at column names on a production database.

What this file does not do is invent a fact it cannot check. Version floors
and token names are marked as what this diagnostic checks against, with the
step that confirms them on the site in front of you.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real upgrade runner without a line changing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"

# What this diagnostic checks against. Both are the floors it applies, not a
# quotation from a release note, because the container that built this tool
# has no route to the vendor documentation. Confirm them in the admin panel
# under the support and system information screens before planning a window.
PHP_FLOOR_V5 = (8, 1)
PHP_FLOOR_LABEL = "8.1"
PHP_SUPPORTED_CEILING_LABEL = "8.3"


def parse_php_version(php_version: str) -> tuple[int, int]:
    """Major and minor from a version string, however it was typed."""
    text = str(php_version or "").strip()
    match = re.match(r"^(\d+)(?:\.(\d+))?", text)
    if not match:
        raise ValueError(f"not a PHP version: {php_version!r}")
    return int(match.group(1)), int(match.group(2) or 0)


# ---------------------------------------------------------------------------
# 1. Plugin compatibility
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HookMigration:
    legacy_hook: str
    kind: str
    detail: str
    extension_point: str
    effort: str


# The IPS4 constructs a plugin is built from, and what carries the same job in
# an IC5 application. The mapping is structural: plugins and theme hooks are
# removed in 5, so every row here ends at an application extension or a
# template that the theme overrides rather than patches.
HOOK_LIBRARY: tuple[HookMigration, ...] = (
    HookMigration(
        legacy_hook="hooks/*.php code hook",
        kind="Code hook",
        detail=("An IPS4 plugin patches a core class by declaring a hook that "
                "extends it at runtime. IC5 has no plugin loader to apply "
                "that patch."),
        extension_point=("applications/<app>/extensions/core/<Type>/<Name>.php, "
                         "registered in the application's Application.php"),
        effort="Rewrite"),
    HookMigration(
        legacy_hook="themeHook / template hook",
        kind="Theme hook",
        detail=("Theme hooks injected markup into a core template by CSS "
                "selector. IC5 removes them, so injected markup has nowhere "
                "to attach and silently disappears."),
        extension_point=("A template override in the theme, or an Output "
                         "extension when the markup has to come from code"),
        effort="Rewrite"),
    HookMigration(
        legacy_hook="\\IPS\\Plugin\\Setup",
        kind="Installer",
        detail="The plugin installer and its versioning tables are removed.",
        extension_point=("applications/<app>/setup/upg_<version>/upgrade.php "
                         "plus the application's schema files"),
        effort="Rewrite"),
    HookMigration(
        legacy_hook="\\IPS\\Settings::i()",
        kind="Settings",
        detail=("Plugin settings lived in the plugin record. An application "
                "keeps them in its own settings and ACP menu."),
        extension_point="applications/<app>/data/settings.json and an ACP settings controller",
        effort="Port"),
    HookMigration(
        legacy_hook="\\IPS\\Db::i()->select()",
        kind="Database",
        detail=("The query builder survives, but a plugin's tables were "
                "created by the plugin installer and have to move into the "
                "application schema."),
        extension_point="applications/<app>/data/schema.json",
        effort="Port"),
    HookMigration(
        legacy_hook="\\IPS\\Theme::i()->getTemplate()",
        kind="Templates",
        detail=("Templates that belonged to the plugin move into the "
                "application's dev folder and are compiled with it."),
        extension_point="applications/<app>/dev/html/",
        effort="Port"),
    HookMigration(
        legacy_hook="\\IPS\\Output::i()->output",
        kind="Output",
        detail=("Writing directly to the output buffer from a hook has no "
                "equivalent, because nothing runs at that point any more."),
        extension_point="An Output extension, or a controller of the application's own",
        effort="Rewrite"),
)

HOOK_BY_NAME = {row.legacy_hook: row for row in HOOK_LIBRARY}

# Sample plugins, each written to exercise a different shape of failure. The
# scanner reads the constructs, so a plugin declaring nothing scans clean.
SAMPLE_PLUGINS: dict = {
    "Video Embed Plus": ("hooks/*.php code hook", "themeHook / template hook",
                         "\\IPS\\Plugin\\Setup", "\\IPS\\Db::i()->select()"),
    "Member Badge Ribbons": ("themeHook / template hook",
                             "\\IPS\\Theme::i()->getTemplate()"),
    "Topic Auto Tagger": ("hooks/*.php code hook", "\\IPS\\Settings::i()",
                          "\\IPS\\Db::i()->select()"),
    "Sidebar Advert Rotator": ("\\IPS\\Output::i()->output",
                               "themeHook / template hook"),
    "Simple Copyright Line": (),
}

VERDICT_CLEAN = "NO BLOCKERS FOUND"
VERDICT_PORT = "PORTABLE TO AN APPLICATION"
VERDICT_REWRITE = "REWRITE REQUIRED"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


@dataclass(frozen=True)
class PluginScan:
    plugin_name: str
    php_version: str
    php_major_minor: tuple[int, int]
    php_meets_floor: bool
    deprecated_hooks: tuple[HookMigration, ...]
    extension_points: tuple[str, ...]
    rewrite_count: int
    port_count: int
    verdict: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
            if any(f.severity == level for f in self.findings):
                return level
        return SEVERITY_OK

    @property
    def total_constructs(self) -> int:
        """Every construct found, which must equal rewrite plus port."""
        return len(self.deprecated_hooks)


def scan_plugin_compatibility(plugin_name: str, php_version: str) -> PluginScan:
    """What in this plugin stops working, and what replaces each piece.

    The plugin can be one of the samples or any name at all: an unknown name
    scans as a plugin whose constructs are not yet known, which is reported as
    exactly that rather than as a clean bill of health. A scanner that returns
    "no problems" for something it never read is worse than no scanner.
    """
    name = str(plugin_name or "").strip()
    if not name:
        raise ValueError("a scan needs a plugin name")
    major_minor = parse_php_version(php_version)
    meets_floor = major_minor >= PHP_FLOOR_V5

    findings: list[Finding] = []
    known = name in SAMPLE_PLUGINS
    hook_names = SAMPLE_PLUGINS.get(name, ())
    hooks = tuple(HOOK_BY_NAME[hook] for hook in hook_names)

    if not meets_floor:
        findings.append(Finding(
            code="IPB5-PHP", severity=SEVERITY_CRITICAL,
            title=f"PHP {php_version} is below the {PHP_FLOOR_LABEL} floor this diagnostic applies",
            detail=("The upgrader stops before it touches the database when "
                    "the runtime is too old, so this is a blocker for the "
                    "whole site rather than for one plugin."),
            fix=(f"Move the host to PHP {PHP_FLOOR_LABEL} or newer, tested up "
                 f"to {PHP_SUPPORTED_CEILING_LABEL}, and confirm the floor in "
                 f"the admin panel system information screen before booking "
                 f"the window.")))
    else:
        findings.append(Finding(
            code="IPB5-PHP-OK", severity=SEVERITY_OK,
            title=f"PHP {php_version} clears the {PHP_FLOOR_LABEL} floor",
            detail="The runtime is not what will stop this upgrade.",
            fix=("Still run the upgrade against a copy first. Clearing the "
                 "floor says the code loads, not that the plugin works.")))

    if not known:
        findings.append(Finding(
            code="IPB5-UNKNOWN", severity=SEVERITY_WARN,
            title=f"{name} is not in this diagnostic's library",
            detail=("Nothing was read, so nothing was found, and those are "
                    "not the same thing as nothing being wrong."),
            fix=("Unzip the plugin and grep its XML for hooks, then match "
                 "what you find against the library on this page. A plugin "
                 "reported clean without being read is the reason upgrades "
                 "fail on the night.")))

    rewrite = sum(1 for hook in hooks if hook.effort == "Rewrite")
    port = sum(1 for hook in hooks if hook.effort == "Port")

    if not known:
        verdict = "NOT SCANNED"
    elif rewrite:
        verdict = VERDICT_REWRITE
    elif hooks:
        verdict = VERDICT_PORT
    else:
        verdict = VERDICT_CLEAN

    for hook in hooks:
        findings.append(Finding(
            code=f"IPB5-{hook.kind.upper().replace(' ', '-')}",
            severity=(SEVERITY_CRITICAL if hook.effort == "Rewrite"
                      else SEVERITY_WARN),
            title=f"{hook.legacy_hook} has no direct equivalent in 5"
                  if hook.effort == "Rewrite"
                  else f"{hook.legacy_hook} moves rather than disappears",
            detail=hook.detail,
            fix=f"Replace it with {hook.extension_point}."))

    if known and not hooks:
        findings.append(Finding(
            code="IPB5-NO-HOOKS", severity=SEVERITY_OK,
            title=f"{name} declares no hook this diagnostic flags",
            detail=("It still has to become an application, because the "
                    "plugin system itself is gone, but nothing inside it "
                    "needs redesigning first."),
            fix=("Convert it with the developer tools and keep the code as "
                 "it stands.")))

    if verdict == "NOT SCANNED":
        headline = (f"{name} was not read, so this is a list of what to look "
                    f"for rather than a result")
    elif rewrite:
        headline = (f"{name} uses {len(hooks)} construct(s) that 5 changes, "
                    f"{rewrite} of which have no direct equivalent and need "
                    f"redesigning rather than porting")
    elif hooks:
        headline = (f"{name} uses {len(hooks)} construct(s) that move to new "
                    f"homes in an application, with nothing needing a redesign")
    else:
        headline = (f"{name} declares no flagged construct, so converting it "
                    f"to an application is the whole job")

    return PluginScan(
        plugin_name=name, php_version=str(php_version).strip(),
        php_major_minor=major_minor, php_meets_floor=meets_floor,
        deprecated_hooks=hooks,
        extension_points=tuple(hook.extension_point for hook in hooks),
        rewrite_count=rewrite, port_count=port, verdict=verdict,
        headline=headline, findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 2. Theme variable mapping
# ---------------------------------------------------------------------------

KIND_COLOR = "colour"
KIND_SPACING = "spacing"
KIND_RADIUS = "radius"
KIND_FONT = "font"
KIND_SHADOW = "shadow"
KIND_OTHER = "other"

# The token naming convention this mapper follows. It is a convention, not a
# quotation: the authoritative list of custom properties is the :root block of
# the theme actually installed, and the verification step below reads it. The
# fallback argument is what makes that distinction safe rather than academic.
TOKEN_PREFIX = "--i"

LEGACY_LIBRARY: dict = {
    "#ipsLayout_header background": (KIND_COLOR, "#3b5998", "color--header-background"),
    ".ipsButton_primary background": (KIND_COLOR, "#1c5eb4", "color--primary"),
    ".ipsButton_primary color": (KIND_COLOR, "#ffffff", "color--primary-contrast"),
    ".ipsBox background": (KIND_COLOR, "#ffffff", "color--surface"),
    ".ipsBox border-radius": (KIND_RADIUS, "3px", "radius--box"),
    ".ipsBox box-shadow": (KIND_SHADOW, "0 1px 2px rgba(0,0,0,0.08)", "shadow--box"),
    ".ipsType_sectionTitle font-size": (KIND_FONT, "17px", "font-size--large"),
    ".ipsType_normal color": (KIND_COLOR, "#303236", "color--text"),
    ".ipsAreaBackground padding": (KIND_SPACING, "15px", "spacing--medium"),
    ".ipsComment_content padding": (KIND_SPACING, "12px", "spacing--small"),
    ".ipsDataItem border-bottom": (KIND_COLOR, "#e3e3e3", "color--border"),
    "#ipsLayout_footer color": (KIND_COLOR, "#8b8b8b", "color--text-muted"),
}

READ_REAL_TOKENS = (
    "Open the site, inspect the html element, and read the :root block in "
    "the compiled theme CSS. Those names are the authoritative ones for the "
    "theme you actually have."
)


@dataclass(frozen=True)
class ThemeMapping:
    legacy_element: str
    known: bool
    kind: str
    legacy_value: str
    token: str
    declaration: str
    property_name: str
    has_fallback: bool
    verification_step: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)


def _css_property_for(element: str, kind: str) -> str:
    tail = element.strip().rsplit(" ", 1)[-1]
    if tail and not tail.startswith((".", "#")):
        return tail
    return {KIND_COLOR: "color", KIND_SPACING: "padding",
            KIND_RADIUS: "border-radius", KIND_FONT: "font-size",
            KIND_SHADOW: "box-shadow"}.get(kind, "color")


def map_theme_variables(legacy_css_element: str) -> ThemeMapping:
    """Translate one hardcoded IPS4 declaration into an IC5 custom property.

    The declaration that comes back always carries the original value as the
    fallback argument of var(). That is the whole point rather than a nicety:
    the token names belong to the theme installed on the site, this tool
    cannot read that theme, and a var() with no fallback whose name is wrong
    renders as nothing at all. With the fallback, a wrong name renders exactly
    what the site rendered before, and the upgrade degrades to a no change
    instead of to a blank page.
    """
    element = str(legacy_css_element or "").strip()
    if not element:
        raise ValueError("a mapping needs a legacy element to map")

    findings: list[Finding] = []
    entry = LEGACY_LIBRARY.get(element)
    known = entry is not None

    if known:
        kind, value, token_tail = entry
    else:
        kind = _guess_kind(element)
        value = "inherit"
        token_tail = _suggest_token_tail(element, kind)
        findings.append(Finding(
            code="THEME-UNKNOWN", severity=SEVERITY_WARN,
            title=f"{element} is not in this mapper's library",
            detail=("The token below follows the naming convention this "
                    "mapper uses, and the fallback is inherit because the "
                    "original value is not known here."),
            fix=("Put the value from your own stylesheet into the fallback "
                 "argument before shipping it, or the declaration changes "
                 "the rendering rather than preserving it.")))

    token = f"{TOKEN_PREFIX}-{token_tail}"
    prop = _css_property_for(element, kind)
    declaration = f"{prop}: var({token}, {value});"

    findings.append(Finding(
        code="THEME-FALLBACK", severity=SEVERITY_OK,
        title="The original value is carried as the fallback",
        detail=(f"If {token} does not exist in the installed theme, the "
                f"browser uses {value} and the element renders as it did in "
                f"4. Without the fallback a wrong token name renders nothing."),
        fix=READ_REAL_TOKENS))

    findings.append(Finding(
        code="THEME-CONVENTION", severity=SEVERITY_WARN,
        title="This token name follows a convention, it is not a quotation",
        detail=("The authoritative custom property names live in the :root "
                "block of the theme installed on the site. This container "
                "has no route to the vendor documentation and did not "
                "pretend otherwise."),
        fix=READ_REAL_TOKENS))

    if kind == KIND_COLOR:
        findings.append(Finding(
            code="THEME-DARK", severity=SEVERITY_WARN,
            title="A hardcoded colour has no dark mode",
            detail=("This is the reason to move it to a token at all. A "
                    "literal value is the same in both schemes, so a light "
                    "surface colour stays light on a dark background."),
            fix=("Once the token is in place, set its dark value in the "
                 "theme rather than adding a second rule here.")))

    return ThemeMapping(
        legacy_element=element, known=known, kind=kind, legacy_value=value,
        token=token, declaration=declaration, property_name=prop,
        has_fallback=True, verification_step=READ_REAL_TOKENS,
        findings=tuple(findings),
    )


def _guess_kind(element: str) -> str:
    lowered = element.lower()
    if "radius" in lowered:
        return KIND_RADIUS
    if "shadow" in lowered:
        return KIND_SHADOW
    if "font" in lowered or "type" in lowered:
        return KIND_FONT
    if any(word in lowered for word in ("padding", "margin", "gap", "spacing")):
        return KIND_SPACING
    if any(word in lowered for word in ("color", "colour", "background",
                                        "border")):
        return KIND_COLOR
    return KIND_OTHER


def _suggest_token_tail(element: str, kind: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", element.lower()).strip("-")
    return f"{kind}--{slug}" if kind != KIND_OTHER else f"custom--{slug}"


# ---------------------------------------------------------------------------
# 3. Pages record recovery
# ---------------------------------------------------------------------------

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

RECOVERY_BLOCKED = "BLOCKED"
RECOVERY_READY = "READY"

SAMPLE_TABLES: tuple[str, ...] = (
    "ips_cms_custom_database_4",
    "ips_cms_custom_database_7",
    "ips_cms_databases",
)


@dataclass(frozen=True)
class RecoveryPlan:
    table_name: str
    status: str
    database_id: str
    backup_command: str
    discovery_query: str
    dry_run_query: str
    extraction_query: str
    load_query: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def queries(self) -> tuple[tuple[str, str], ...]:
        """Every step in order, so the page cannot print them out of order."""
        if self.status == RECOVERY_BLOCKED:
            return ()
        return (
            ("1. Back up before reading, because the next person to touch "
             "this table may not be as careful", self.backup_command),
            ("2. Discover the column map, because field_N numbers are site "
             "specific and cannot be guessed", self.discovery_query),
            ("3. Count first, so you know what you are about to move",
             self.dry_run_query),
            ("4. Extract the records with their real column names",
             self.extraction_query),
            ("5. Load into a new table, never over the original",
             self.load_query),
        )


def simulate_database_recovery(table_name: str) -> RecoveryPlan:
    """The MySQL to lift isolated Pages records out and into the 5 schema.

    Two things make this more than one query. Pages stores each database in
    its own table with columns named field_1, field_2 and so on, and those
    numbers are assigned per site, so the column that holds a video URL on one
    forum holds a release date on another. The map has to be read before the
    extract is written.

    And the extract must never write over the table it read. Every query here
    is a SELECT except the last, which creates a new table, so a mistake costs
    a dropped copy rather than the records.
    """
    table = str(table_name or "").strip()
    findings: list[Finding] = []

    if not IDENTIFIER.match(table):
        findings.append(Finding(
            code="DB-IDENT", severity=SEVERITY_CRITICAL,
            title=f"{table or 'The table name'} is not a valid MySQL identifier",
            detail=("A table name cannot be quoted safely if it is not one, "
                    "and a generated query carrying it would be a query "
                    "nobody should paste into a production shell."),
            fix=("Give the real table name, which for Pages looks like "
                 "ips_cms_custom_database_4. Read the list from "
                 "ips_cms_databases if you do not have it.")))
        return RecoveryPlan(
            table_name=table, status=RECOVERY_BLOCKED, database_id="",
            backup_command="", discovery_query="", dry_run_query="",
            extraction_query="", load_query="", findings=tuple(findings))

    match = re.search(r"database_(\d+)$", table)
    database_id = match.group(1) if match else ""

    if not database_id:
        findings.append(Finding(
            code="DB-NOT-PAGES", severity=SEVERITY_WARN,
            title=f"{table} is not a per database Pages records table",
            detail=("Pages record tables end in the numeric database id. "
                    "This looks like a different table, so the column map "
                    "query below will return nothing."),
            fix=("Run SELECT database_id, database_key, database_name FROM "
                 "ips_cms_databases to find the records table you want.")))

    backup = (f"mysqldump --single-transaction --quick "
              f"--result-file={table}_preupgrade.sql <schema> {table}")

    discovery = "\n".join([
        "-- Which field_N column holds what. Run this first and read it.",
        "SELECT",
        "    f.field_id,",
        "    CONCAT('field_', f.field_id) AS column_name,",
        "    f.field_key,",
        "    f.field_type,",
        "    f.field_required",
        "FROM ips_cms_database_fields AS f",
        f"WHERE f.field_database_id = {database_id or '<database_id>'}",
        "ORDER BY f.field_position;",
    ])

    dry_run = "\n".join([
        "-- Count before you move anything. A recovery that starts by",
        "-- knowing the number is a recovery that can be checked afterwards.",
        "SELECT",
        "    COUNT(*)                          AS total_records,",
        "    SUM(record_approved = 1)          AS approved,",
        "    SUM(record_approved <> 1)         AS hidden_or_pending,",
        "    MIN(record_publish_date)          AS earliest,",
        "    MAX(record_publish_date)          AS latest",
        f"FROM {table};",
    ])

    extraction = "\n".join([
        "-- Replace each field_N below with the column names the discovery",
        "-- query returned. Do not assume field_1 is the title: that number",
        "-- is assigned per site and this tool cannot see yours.",
        "SELECT",
        "    r.primary_id_field                AS legacy_record_id,",
        "    r.member_id                       AS author_member_id,",
        "    r.category_id                     AS legacy_category_id,",
        "    FROM_UNIXTIME(r.record_publish_date) AS published_at,",
        "    FROM_UNIXTIME(r.record_updated)   AS updated_at,",
        "    r.record_approved                 AS approved,",
        "    r.record_static_furl              AS slug,",
        "    r.field_1                         AS record_title,",
        "    r.field_2                         AS video_url,",
        "    r.field_3                         AS video_description",
        f"FROM {table} AS r",
        "WHERE r.record_approved IS NOT NULL",
        "ORDER BY r.primary_id_field;",
    ])

    load = "\n".join([
        "-- A new table, always. The original stays untouched, so a mistake",
        "-- here costs a DROP of the copy rather than the records.",
        f"CREATE TABLE {table}_recovered LIKE {table};",
        f"INSERT INTO {table}_recovered",
        f"SELECT * FROM {table}",
        "WHERE record_approved IS NOT NULL;",
        "",
        "-- Prove the move before you trust it. These two must match.",
        f"SELECT COUNT(*) AS source FROM {table} WHERE record_approved IS NOT NULL;",
        f"SELECT COUNT(*) AS recovered FROM {table}_recovered;",
    ])

    findings.append(Finding(
        code="DB-FIELD-MAP", severity=SEVERITY_CRITICAL,
        title="field_N column numbers are site specific and must be read first",
        detail=("The column holding a video URL on one forum holds a release "
                "date on another, because Pages assigns field ids in the "
                "order the fields were created. An extract written from a "
                "table name alone is a guess."),
        fix=("Run the discovery query, then edit the aliases in the extract "
             "so each one names the column the discovery query returned.")))

    findings.append(Finding(
        code="DB-READ-ONLY", severity=SEVERITY_OK,
        title="Every query here reads, except the load which creates a copy",
        detail=("Nothing updates or deletes a row in the source table, so "
                "the worst outcome of running the whole sequence is a table "
                "you drop afterwards."),
        fix=("Keep it that way. Any UPDATE against the source belongs in a "
             "separate, reviewed step after the counts have matched.")))

    findings.append(Finding(
        code="DB-BACKUP", severity=SEVERITY_WARN,
        title="Take the dump even though nothing here writes",
        detail=("The dump is not protection against these queries. It is "
                "protection against the next thing that runs on that "
                "database while the recovery is in progress."),
        fix=f"Run the mysqldump line first and check the file is not empty."))

    return RecoveryPlan(
        table_name=table, status=RECOVERY_READY, database_id=database_id,
        backup_command=backup, discovery_query=discovery,
        dry_run_query=dry_run, extraction_query=extraction, load_query=load,
        findings=tuple(findings),
    )

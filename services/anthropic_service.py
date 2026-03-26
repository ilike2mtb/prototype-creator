import anthropic, asyncio, base64, json, logging, re
import httpx as _httpx
from collections import Counter
from config import settings
from services.integrations import (
    get_frames, get_nodes, export_images,
    get_variables, get_components, get_styles,
    get_file_summary, search_file,
    get_architecture_plan, get_architecture_template,
)

log = logging.getLogger("anthropic_service")

client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

MODEL            = "claude-sonnet-4-5"
MAX_TOKENS       = 3500   # default per-call limit — stays under the 4 000 token/min rate cap
HTML_MAX_TOKENS  = 16000  # HTML phase: enough for data-layer + full CSS system + all pages
MAX_TOOL_CHARS   = 4000
PHASE_DELAY      = 65     # seconds between phases — lets the 4 000/min token bucket reset
HTML_PHASE_DELAY = 160    # longer wait before HTML phase to refill token bucket fully
RATE_LIMIT_WAIT  = 65     # seconds to wait on a 429 before retrying


def _resolve_figma_params(user_params: dict) -> dict:
    """Merge user-supplied Figma params with server defaults.

    Uses FIGMA_FILE_KEY_2 / FIGMA_NODE_IDS / FIGMA_DEPTH (existing Render env vars)
    so no additional environment variables are needed on the server.
    Returns an empty dict only when no defaults are configured either.
    """
    defaults = {}
    default_fk = settings.figma_file_key_2 or settings.figma_file_key
    if default_fk:
        defaults["file_key"] = default_fk
    if settings.figma_node_ids:
        defaults["ids"] = settings.figma_node_ids
    defaults["depth"] = settings.figma_depth

    merged = {**defaults, **(user_params or {})}
    # Only return params if we have at minimum a file_key
    return merged if merged.get("file_key") else {}


# ── Tools ─────────────────────────────────────────────────────────────────────

ALL_TOOLS = [
    {
        "name": "get_figma_summary",
        "description": (
            "Get a lightweight overview of the Figma file: file name, page names, "
            "top-level frame names, and counts of components, styles, and variables. "
            "Call this FIRST before any other Figma tool to orient yourself — it shows "
            "what pages and frames exist so you can make targeted follow-up calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key": {"type": "string", "description": "Figma file key (optional — uses default if omitted)"}
            }
        }
    },
    {
        "name": "get_figma_frames",
        "description": "Get all frames in the Figma file to analyse the design.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key": {"type": "string", "description": "Figma file key"},
                "ids":      {"type": "string", "description": "Node IDs, comma-separated"},
                "depth":    {"type": "integer","description": "Tree depth 1-10"}
            }
        }
    },
    {
        "name": "get_figma_nodes",
        "description": "Get Figma node structure and component details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key": {"type": "string"},
                "ids":      {"type": "string"},
                "depth":    {"type": "integer"}
            }
        }
    },
    {
        "name": "export_frame_images",
        "description": "Export rendered images of Figma frames.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key": {"type": "string"},
                "ids":      {"type": "string"},
                "format":   {"type": "string", "description": "png | jpg | svg"},
                "scale":    {"type": "number",  "description": "1-4"}
            }
        }
    },
    {
        "name": "get_figma_variables",
        "description": (
            "Get design tokens (colors, spacing, typography scales) defined as local variables "
            "in the Figma file. Use this to extract the exact color palette and spacing system "
            "so generated code uses the real design tokens instead of guessed values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key": {"type": "string", "description": "Figma file key (optional — uses default if omitted)"}
            }
        }
    },
    {
        "name": "get_figma_components",
        "description": (
            "Get the component library from the Figma file — names, descriptions, and node IDs "
            "of all reusable components. Use this to understand the design system's component "
            "vocabulary before generating framework code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key": {"type": "string", "description": "Figma file key (optional)"}
            }
        }
    },
    {
        "name": "get_figma_styles",
        "description": (
            "Get published styles (color fills, text styles, effects, grids) from the Figma file. "
            "Use this to extract the typographic scale, shadow definitions, and grid system."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key": {"type": "string", "description": "Figma file key (optional)"}
            }
        }
    },
    {
        "name": "search_figma_nodes",
        "description": (
            "Search the full Figma file tree by node name, text content, or node ID. "
            "Returns matching nodes with their paths, bounds, and parent IDs. "
            "Use this to locate specific components, text layers, or frames by keyword "
            "rather than traversing the full tree with get_figma_frames/get_figma_nodes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":     {"type": "string",  "description": "Case-insensitive search term matched against node names, text content, and IDs."},
                "file_key":  {"type": "string",  "description": "Figma file key (optional — uses default if omitted)"},
                "node_type": {"type": "string",  "description": "Optional exact Figma node type filter, e.g. FRAME, TEXT, COMPONENT, INSTANCE"},
                "page_name": {"type": "string",  "description": "Optional exact page name to restrict the search scope"},
                "limit":     {"type": "integer", "description": "Maximum number of matches to return (default 25, max 100)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_dci_architecture_plan",
        "description": "Fetch the DCI architecture plan from SharePoint.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_architecture_plan_template",
        "description": "Fetch the architecture plan template from SharePoint.",
        "input_schema": {"type": "object", "properties": {}}
    },
]

TOOL_MAP = {
    "get_figma_summary":              lambda i: get_file_summary(**i),
    "get_figma_frames":               lambda i: get_frames(**i),
    "get_figma_nodes":                lambda i: get_nodes(**i),
    "export_frame_images":            lambda i: export_images(**i),
    "get_figma_variables":            lambda i: get_variables(**i),
    "get_figma_components":           lambda i: get_components(**i),
    "get_figma_styles":               lambda i: get_styles(**i),
    "search_figma_nodes":             lambda i: search_file(**i),
    "get_dci_architecture_plan":      lambda _: get_architecture_plan(),
    "get_architecture_plan_template": lambda _: get_architecture_template(),
}


def truncate(result) -> str:
    s = json.dumps(result) if not isinstance(result, str) else result
    if len(s) > MAX_TOOL_CHARS:
        s = s[:MAX_TOOL_CHARS] + "... [truncated]"
    return s


def _select_tools(mode: str, framework: str, output_type: str) -> list:
    """Return the subset of tools relevant for this session."""
    has_figma = mode in ("figma", "both")
    # Architecture only for Drupal/Claude-chooses, and only when generating framework output
    has_arch = (
        mode in ("arch", "both") and
        framework in ("drupal10", "drupal11", "claude") and
        output_type in ("framework", "both")
    )
    FIGMA_TOOLS = {
        "get_figma_summary", "get_figma_frames", "get_figma_nodes", "export_frame_images",
        "get_figma_variables", "get_figma_components", "get_figma_styles", "search_figma_nodes",
    }
    ARCH_TOOLS = {"get_dci_architecture_plan", "get_architecture_plan_template"}
    return [t for t in ALL_TOOLS if
        (has_figma or t["name"] not in FIGMA_TOOLS) and
        (has_arch  or t["name"] not in ARCH_TOOLS)
    ]


# ── Core LLM helper ───────────────────────────────────────────────────────────

async def _call(system: str, messages: list, tools: list = None, retries: int = 2,
                max_tokens: int = None) -> tuple:
    """Run a single LLM request with optional tool-use loop and rate-limit retry.
    Returns (text: str, stop_reason: str).
    """
    kwargs = {
        "model":      MODEL,
        "max_tokens": max_tokens or MAX_TOKENS,
        "system":     system,
    }
    if tools:
        kwargs["tools"] = tools

    msgs = list(messages)

    for attempt in range(retries + 1):
        try:
            response = await client.messages.create(**kwargs, messages=msgs)

            while tools and response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result = await TOOL_MAP[block.name](block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     truncate(result),
                    })
                msgs = msgs + [
                    {"role": "assistant", "content": response.content},
                    {"role": "user",      "content": tool_results},
                ]
                response = await client.messages.create(**kwargs, messages=msgs)

            text = "".join(b.text for b in response.content if hasattr(b, "text"))
            return text, response.stop_reason

        except anthropic.RateLimitError:
            if attempt < retries:
                await asyncio.sleep(RATE_LIMIT_WAIT)
            else:
                raise


def _parse_files(text: str) -> list:
    """Extract <FILE path="...">content</FILE> blocks from LLM output.
    Also recovers the last file if the closing tag was cut off by a token limit.
    """
    pattern = r'<FILE path="([^"]+)">(.*?)</FILE>'
    matches = re.findall(pattern, text, re.DOTALL)
    files = [{"path": p.strip(), "content": c.strip()} for p, c in matches]

    # Truncation recovery: capture final unclosed FILE block if present
    last_open = list(re.finditer(r'<FILE path="([^"]+)">', text))
    if last_open:
        last = last_open[-1]
        last_path = last.group(1).strip()
        # Only recover if this file wasn't already captured by a closed block
        if not any(f["path"] == last_path for f in files):
            content = text[last.end():]
            files.append({"path": last_path, "content": content.strip() + "\n<!-- truncated -->"})

    return files


# ── System prompts ────────────────────────────────────────────────────────────

def _plan_system(framework: str, mode: str, drupal_version: str, figma_params: dict) -> str:
    has_figma = mode in ("figma", "both")
    has_arch  = mode in ("arch",  "both")
    param_str = f"\nFigma params to use: {json.dumps(figma_params)}" if figma_params else ""

    if framework == "claude":
        fw_guidance = (
            "Choose the best framework (drupal10, drupal11, or laravel) based on the "
            "user's requirements. Set 'framework' accordingly in your plan, and set "
            "'frameworkRationale' to a concise 1-2 sentence explanation of why you chose it."
        )
    elif framework in ("drupal10", "drupal11"):
        ver = "10" if framework == "drupal10" else "11"
        fw_guidance = f'Use Drupal {ver}. Set "framework": "{framework}" in your plan.'
    else:
        fw_guidance = 'Use Laravel. Set "framework": "laravel" in your plan.'

    figma_instruction = (
        f"Call get_figma_summary FIRST to see the file's page names, frame names, and "
        f"component/style/variable counts. Then call get_figma_variables to extract the "
        f"design token palette (colors, spacing, typography). Use get_figma_frames or "
        f"get_figma_nodes only for specific frames identified in the summary. Call "
        f"get_figma_components or get_figma_styles if their counts are >0. "
        f"Apply all extracted tokens to the 'designTokens' block — including backgroundColor "
        f"and textColor when present.{param_str}"
        if has_figma else "No Figma design available."
    )
    arch_instruction = (
        "Use get_dci_architecture_plan to understand the existing data model before planning."
        if has_arch else ""
    )

    rationale_field = (
        '\n  "frameworkRationale": "1-2 sentence explanation of why this framework was chosen",'
        if framework == "claude" else ""
    )

    return f"""You are an expert web architect. Analyse the user's requirements and produce a structured project plan.

{fw_guidance}

{figma_instruction}
{arch_instruction}

IMPORTANT: Limit "pages" to a maximum of 5. Consolidate related sections into single pages (e.g. combine listing + detail into one page, or group similar views).

Respond with ONLY this JSON block and no other text:
<PLAN>
{{
  "projectName": "kebab-case-name",
  "displayName": "Human Readable Name",
  "summary": "2-3 sentence description of what this prototype does",
  "framework": "drupal11 | drupal10 | laravel",{rationale_field}
  "drupalVersion": "11 | 10 | null",
  "contentTypes": [
    {{"name": "article", "fields": [{{"name": "title", "type": "string"}}, {{"name": "body", "type": "text"}}]}}
  ],
  "taxonomies": [{{"name": "tags", "terms": []}}],
  "pages": [
    {{"name": "Home", "path": "/", "description": "Landing page"}},
    {{"name": "Listing", "path": "/items", "description": "Content listing"}}
  ],
  "_note": "Maximum 5 pages. Consolidate related views into one page rather than making separate pages.",
  "designTokens": {{
    "primaryColor": "#6366f1",
    "secondaryColor": "#8b5cf6",
    "backgroundColor": "#111827",
    "textColor": "#f9fafb",
    "fontFamily": "system-ui, sans-serif",
    "borderRadius": "8px"
  }}
}}
</PLAN>"""


def _drupal_backend_system(plan: dict, drupal_version: str) -> str:
    ver = drupal_version or "11"
    content_types = plan.get("contentTypes", [])
    ct_examples = " | ".join(ct["name"].lower().replace(" ", "_") for ct in content_types[:3]) or "article"
    return f"""You are an expert Drupal {ver} developer. Generate backend module files for this project.

Project plan:
{json.dumps(plan, indent=2)}

Generate ALL of these files — every one is required:
1. MODULE_NAME.info.yml
2. MODULE_NAME.module — ALWAYS generate this. Include hook_help(), hook_theme() registrations,
   and hook_preprocess_node() to pass useful variables to templates.
3. config/install/node.type.CONTENT_TYPE.yml — one per content type ({ct_examples})
4. config/install/field.storage.node.FIELD_NAME.yml — one per custom field
5. config/install/field.field.node.CONTENT_TYPE.FIELD_NAME.yml — one per field instance
   (field.field differs from field.storage: it is the per-bundle attachment config)
6. config/install/views.view.CONTENT_TYPE_list.yml — one listing view (keep concise)

Format each file EXACTLY like this — no explanatory text, only FILE blocks:
<FILE path="web/modules/custom/MODULE_NAME/MODULE_NAME.info.yml">
file content here
</FILE>

Keep Drupal {ver} config schema. Omit boilerplate comments. Use realistic machine names."""


def _drupal_theme_system(plan: dict, drupal_version: str) -> str:
    ver = drupal_version or "11"
    tokens = plan.get("designTokens", {})
    return f"""You are an expert Drupal {ver} theme developer. Generate theme static asset files for this project.

Project plan:
{json.dumps(plan, indent=2)}

Design tokens: {json.dumps(tokens)}

Generate ONLY these three files (Twig templates will be generated separately):
1. THEME_NAME.info.yml — base theme: false, list all regions (header, primary_menu, breadcrumb,
   highlighted, help, content, sidebar_first, sidebar_second, footer), attach libraries
2. THEME_NAME.libraries.yml — define global-styling and interactive-components libraries
3. css/style.css — comprehensive stylesheet using CSS custom properties from design tokens:
   :root {{
     --color-primary: {tokens.get('primaryColor','#6366f1')};
     --color-secondary: {tokens.get('secondaryColor','#8b5cf6')};
     --color-bg: {tokens.get('backgroundColor','#111827')};
     --color-text: {tokens.get('textColor','#f9fafb')};
     --radius: {tokens.get('borderRadius','8px')};
     --font: {tokens.get('fontFamily','system-ui, sans-serif')};
   }}
   body {{ background: var(--color-bg); color: var(--color-text); font-family: var(--font); }}
   Include styles for: nav, cards, forms, buttons, layout containers, and responsive breakpoints.

Format each file EXACTLY like this — no explanatory text, only FILE blocks:
<FILE path="web/themes/custom/THEME_NAME/THEME_NAME.info.yml">
file content here
</FILE>

Use realistic names derived from the project plan. Make the CSS thorough and professional."""


def _drupal_twig_system(plan: dict, drupal_version: str) -> str:
    ver = drupal_version or "11"
    tokens = plan.get("designTokens", {})
    content_types = plan.get("contentTypes", [])
    primary  = tokens.get("primaryColor",   "#6366f1")
    bg       = tokens.get("backgroundColor","#111827")
    fg       = tokens.get("textColor",      "#f9fafb")
    project  = plan.get("displayName", "Site")

    ct_twig_list = "\n".join(
        f"- templates/node/node--{ct['name'].lower().replace(' ', '_')}.html.twig"
        for ct in content_types
    )

    fields_hint = ""
    for ct in content_types[:2]:   # hint on first 2 types to keep prompt tight
        field_names = [f["name"] for f in ct.get("fields", [])[:4]]
        if field_names:
            fields_hint += f"\n  {ct['name']} fields: {', '.join(field_names)}"

    return f"""You are an expert Drupal {ver} Twig developer. Generate complete, working Twig templates.

Project plan:
{json.dumps(plan, indent=2)}

Generate ALL of these template files:
1. templates/layout/page.html.twig — full page layout
2. templates/layout/page--front.html.twig — homepage variant (hero section + intro)
{ct_twig_list}

Design tokens for inline styles / CSS class hints:
  Primary: {primary} | Background: {bg} | Text: {fg}
{fields_hint}

Rules for page.html.twig:
- Include {{ page.header }}, {{ page.primary_menu }}, {{ page.content }}, {{ page.footer }} regions
- Wrap in <div class="layout-container"> with a <main role="main"> content area
- Add a sticky <header> with site name "{project}" and nav region

Rules for node--TYPE.html.twig:
- Use {{{{ attributes }}}} on <article>
- Render title with {{{{ title_prefix }}}}, <h2>{{{{ label }}}}</h2>, {{{{ title_suffix }}}}
- Render body/fields via {{{{ content }}}} but ALSO explicitly render key fields:
  e.g. {{% if content.field_featured_image %}}{{{{ content.field_featured_image }}}}...
- Add teaser vs full-page conditional: {{% if view_mode == 'teaser' %}}
- Use BEM class names matching the content type machine name

Format each file EXACTLY like this — no explanatory text, only FILE blocks:
<FILE path="web/themes/custom/THEME_NAME/templates/layout/page.html.twig">
{{#
/**
 * @file
 * Theme override for the page template.
 */
#}}
file content here
</FILE>

Replace THEME_NAME with the actual theme machine name from the project plan.
Write complete, real Twig — not stubs. Every template must be fully functional."""


def _build_drupal_readme(plan: dict, drupal_ver: str) -> str:
    """Generate a README.md deterministically from the plan — no LLM call needed."""
    project_name   = plan.get("projectName",  "custom-project")
    display_name   = plan.get("displayName",  "Custom Project")
    summary        = plan.get("summary",      "A Drupal prototype.")
    module_name    = project_name.replace("-", "_")
    theme_name     = module_name   # convention: theme shares the module machine name
    content_types  = plan.get("contentTypes", [])
    taxonomies     = plan.get("taxonomies",   [])
    pages          = plan.get("pages",        [])
    tokens         = plan.get("designTokens", {})

    # ── Content type table ────────────────────────────────────────────────────
    ct_rows = ""
    for ct in content_types:
        fields = ", ".join(f["name"] for f in ct.get("fields", []))
        ct_rows += f"| `{ct['name'].lower().replace(' ','_')}` | {ct['name']} | {fields or '—'} |\n"

    # ── Taxonomy table ────────────────────────────────────────────────────────
    tax_rows = ""
    for t in taxonomies:
        tax_rows += f"| `{t['name'].lower().replace(' ','_')}` | {t['name']} |\n"

    # ── Pages table ───────────────────────────────────────────────────────────
    page_rows = ""
    for p in pages:
        page_rows += f"| {p['name']} | `{p.get('path','/')}` | {p.get('description','')} |\n"

    # ── Required contrib modules (inferred from info.yml dependencies) ────────
    required_modules = [
        "node", "field", "text", "image", "views", "taxonomy", "path", "options",
        "entity_reference_revisions", "paragraphs",
    ]
    module_list = "\n".join(f"- `{m}`" for m in required_modules)

    # ── Twig template table ───────────────────────────────────────────────────
    twig_rows = "| `templates/layout/page.html.twig` | Main page layout (header, nav, content, footer) |\n"
    twig_rows += "| `templates/layout/page--front.html.twig` | Homepage with hero section |\n"
    for ct in content_types:
        machine = ct['name'].lower().replace(' ', '_')
        twig_rows += f"| `templates/node/node--{machine}.html.twig` | {ct['name']} node display |\n"

    # Pre-compute fallback strings to avoid backslash-in-f-string-expression errors (Python < 3.12)
    ct_section   = ct_rows   or "| *(none defined)* | | |\n"
    tax_section  = tax_rows  or "| *(none defined)* | |\n"
    page_section = page_rows or "| *(none defined)* | | |\n"

    return f"""# {display_name}

{summary}

**Framework:** Drupal {drupal_ver}
**Module:** `{module_name}`
**Theme:** `{theme_name}`

---

## Prerequisites

- Drupal {drupal_ver} installation (or DDEV local environment — see below)
- [Drush](https://www.drush.org/) 12+
- The following Drupal core/contrib modules enabled:

{module_list}

---

## Installation

### Option A — Using the `practical` DDEV project template (recommended)

The `practical` repo is HMP Global's standard Drupal starter. Clone it, then
drop these generated files into the project root before running `ddev start`.

#### 0. Prerequisites (first time only)

```bash
# Install DDEV: https://ddev.readthedocs.io/en/stable/users/install/ddev-installation/
# Then authenticate with HMP Global's private Packagist mirror:
ddev exec composer config http-basic.repo.packagist.com token <YOUR_ORG_TOKEN>
# Token: https://packagist.com/orgs/hmp-global/settings/auth
```

#### 1. Clone `practical` and copy generated files

```bash
git clone git@github.com:HMP-Global/practical.git my-new-project
cd my-new-project

# Unzip the prototype-creator output into the project root
unzip /path/to/prototype-output.zip -d .
```

#### 2. Start DDEV

```bash
ddev start
# DDEV runs composer install automatically on first start.
# If it fails with a 401, complete the Packagist auth step above first.
```

#### 3. Install Drupal (fresh database only — skip if importing a DB dump)

```bash
ddev drush site:install --account-name=admin --account-pass=admin -y
```

#### 4. Enable the module (auto-imports all config/install YAMLs)

```bash
ddev drush en {module_name} -y
ddev drush cr
```

#### 5. Enable and set the theme

```bash
ddev drush theme:enable {theme_name} -y
ddev drush config:set system.theme default {theme_name} -y
ddev drush cr
```

#### 6. Verify

```bash
ddev drush status
ddev drush cim --preview   # preview any pending config
ddev drush cr              # final cache rebuild
```

---

### Option B — Vanilla Drupal (no DDEV)

#### Step 1 — Copy files into your Drupal project

```bash
# Copy the custom module
cp -r web/modules/custom/{module_name} /path/to/drupal/web/modules/custom/

# Copy the custom theme
cp -r web/themes/custom/{theme_name} /path/to/drupal/web/themes/custom/
```

#### Step 2 — Enable the module (imports all config automatically)

```bash
cd /path/to/drupal
drush en {module_name} -y
```

Enabling the module automatically imports all YAML configuration files from
`config/install/` — content types, fields, taxonomies, and views are created
without any manual configuration in the Drupal UI.

#### Step 3 — Enable and set the theme

```bash
drush theme:enable {theme_name} -y
drush config:set system.theme default {theme_name} -y
drush cr
```

#### Step 4 — Verify

```bash
drush status
drush cim --preview   # preview any pending config
drush cr              # final cache rebuild
```

---

## Content Types

| Machine Name | Label | Fields |
|---|---|---|
{ct_section}
## Taxonomies

| Machine Name | Label |
|---|---|
{tax_section}
## Pages / Views

| Page | Path | Description |
|---|---|---|
{page_section}

---

## File Structure

```
web/
├── modules/custom/{module_name}/
│   ├── {module_name}.info.yml          # Module definition & dependencies
│   ├── {module_name}.module            # PHP hooks (preprocess, theme, help)
│   └── config/install/
│       ├── node.type.*.yml             # Content type definitions
│       ├── field.storage.node.*.yml    # Field storage configs
│       ├── field.field.node.*.yml      # Field instance (per-bundle) configs
│       └── views.view.*.yml            # Listing view configs
│
└── themes/custom/{theme_name}/
    ├── {theme_name}.info.yml           # Theme definition & regions
    ├── {theme_name}.libraries.yml      # CSS/JS library definitions
    ├── css/style.css                   # Stylesheet (CSS custom properties)
    └── templates/
        ├── layout/
        │   ├── page.html.twig          # Main page layout
        │   └── page--front.html.twig   # Homepage layout
        └── node/
            └── node--*.html.twig       # Per content-type templates
```

## Twig Templates Reference

| Template | Purpose |
|---|---|
{twig_rows}
---

## Design Tokens

These CSS custom properties are defined in `css/style.css` and control the
visual appearance of the theme:

| Token | Value |
|---|---|
| `--color-primary` | `{tokens.get('primaryColor', '#6366f1')}` |
| `--color-secondary` | `{tokens.get('secondaryColor', '#8b5cf6')}` |
| `--color-bg` | `{tokens.get('backgroundColor', '#111827')}` |
| `--color-text` | `{tokens.get('textColor', '#f9fafb')}` |
| `--radius` | `{tokens.get('borderRadius', '8px')}` |
| `--font` | `{tokens.get('fontFamily', 'system-ui, sans-serif')}` |

---

## Uninstalling

```bash
drush pmu {module_name} -y
drush theme:uninstall {theme_name} -y
drush cr
```

> ⚠️ Uninstalling the module will remove all associated configuration.
> Export your content first if needed: `drush dcer --skip-dependencies node`

---

*Generated by [Prototype Creator](https://prototype-creator-ui.onrender.com)*
"""


def _laravel_backend_system(plan: dict) -> str:
    return f"""You are an expert Laravel developer. Generate the backend application files for this project.

Project plan:
{json.dumps(plan, indent=2)}

Generate these files:
- database/migrations/TIMESTAMP_create_TABLE_table.php for each content type
- app/Models/ModelName.php for each content type
- app/Http/Controllers/ResourceController.php for each content type
- routes/web.php with all resource routes
- app/Http/Requests/StoreRequest.php for validation (combine into one file if small)

Format each file EXACTLY like this — no explanatory text, only FILE blocks:
<FILE path="app/Models/Article.php">
<?php
file content here
</FILE>

Use Laravel 11 syntax. Keep code functional and concise."""


def _laravel_theme_system(plan: dict) -> str:
    tokens = plan.get("designTokens", {})
    pages  = plan.get("pages", [])
    return f"""You are an expert Laravel/Blade developer. Generate frontend views for this project.

Project plan:
{json.dumps(plan, indent=2)}

Design tokens: {json.dumps(tokens)}

Generate these files:
- resources/views/layouts/app.blade.php (main layout with navigation)
- resources/views/welcome.blade.php (home page)
{chr(10).join(f"- resources/views/{p['name'].lower().replace(' ', '-')}.blade.php" for p in pages)}
- public/css/style.css — define CSS custom properties then use them:
  :root {{ --color-primary: {tokens.get('primaryColor','#6366f1')}; --color-secondary: {tokens.get('secondaryColor','#8b5cf6')}; --color-bg: {tokens.get('backgroundColor','#111827')}; --color-text: {tokens.get('textColor','#f9fafb')}; }}
  body {{ background: var(--color-bg); color: var(--color-text); }}

Format each file EXACTLY like this — no explanatory text, only FILE blocks:
<FILE path="resources/views/layouts/app.blade.php">
file content here
</FILE>

Make views clean and professional. Use CSS variables from design tokens."""


def _extract_frame_ids(figma_data: dict, max_frames: int = 5) -> list:
    """Walk the Figma node tree and return the first N FRAME node IDs.

    Handles both cases:
    - Node IDs that ARE frames → collected immediately.
    - Node IDs that are pages (CANVAS) → walks one level of children to find
      the top-level frames on that page.
    Children of those frames are NOT recursed into (we want page-level frames,
    not every nested sub-frame).
    """
    ids: list = []
    seen: set = set()

    def collect(node_id: str):
        if node_id and node_id not in seen and len(ids) < max_frames:
            seen.add(node_id)
            ids.append(node_id)

    def walk(node, depth: int = 0):
        if not isinstance(node, dict) or len(ids) >= max_frames:
            return
        ntype = node.get("type")
        nid   = node.get("id")
        if ntype == "FRAME":
            collect(nid)
            return   # don't recurse into sub-frames — we want page-level only
        # For CANVAS (page) nodes or the root document, walk one level of children
        if depth < 2:
            for child in node.get("children") or []:
                walk(child, depth + 1)

    for node_info in (figma_data.get("nodes") or {}).values():
        if isinstance(node_info, dict):
            walk(node_info)

    return ids


# ── Figma structure analysis ───────────────────────────────────────────────────

def _find_container_id(frames: list, provided_ids: str) -> str:
    """Return the most common parentId in the frames list (the container frame).

    Falls back to the first provided ID if no clear container is found.
    """
    if not frames:
        return (provided_ids or "").split(",")[0].strip()
    parent_counts: Counter = Counter(
        f.get("parentId") for f in frames if f.get("parentId")
    )
    if not parent_counts:
        return (provided_ids or "").split(",")[0].strip()
    best, count = parent_counts.most_common(1)[0]
    # Only treat as container if it owns at least 3 direct children
    return best if count >= 3 else (provided_ids or "").split(",")[0].strip()


def _group_screens_by_pattern(screens: list) -> list:
    """Bucket frames into splash_menu, hub, and case_series groups.

    Naming convention detected:
      - "Homepage", "Home", "Splash", etc.  → splash_menu
      - "X N" where N is an integer          → case_series keyed on base name X
      - Everything else                       → hub (individual category page)
    """
    _SPLASH_KWS = {"homepage", "home", "splash", "intro", "start", "welcome", "landing"}
    _NUMBERED   = re.compile(r"^(.+?)\s+(\d+)$")

    numbered: dict  = {}   # base_name → [screen_dict, ...]
    unnumbered: list = []

    for s in screens:
        name = s.get("name", "")
        m = _NUMBERED.match(name)
        if m:
            base = m.group(1).strip()
            numbered.setdefault(base, []).append({**s, "_slide_num": int(m.group(2))})
        else:
            unnumbered.append(s)

    groups: list = []

    splash = [s for s in unnumbered
              if any(kw in s.get("name", "").lower() for kw in _SPLASH_KWS)]
    hubs   = [s for s in unnumbered if s not in splash]

    if splash:
        groups.append({"type": "splash_menu", "name": "Entry", "screens": splash})

    for s in hubs:
        groups.append({"type": "hub", "name": s.get("name", ""), "screens": [s]})

    for base, series in sorted(numbered.items()):
        series.sort(key=lambda x: x["_slide_num"])
        groups.append({"type": "case_series", "name": base, "screens": series})

    return groups


def _extract_all_texts(node: dict) -> list:
    """Recursively collect every TEXT node's characters from a Figma node tree."""
    results: list = []
    if not isinstance(node, dict):
        return results
    if node.get("type") == "TEXT":
        chars = (node.get("characters") or "").strip()
        if len(chars) > 2:
            results.append(chars)
    for child in (node.get("children") or []):
        results.extend(_extract_all_texts(child))
    return results


def _select_representative_frames(groups: list) -> list:
    """Pick 5–8 frame IDs that cover the full visual language of the design.

    Strategy:
      • Splash/menu: up to 2 (prefer the richer/last screen)
      • Hub: 1 representative
      • Case series: slides 1, 2, 3 from the first series (challenge / plan / win)
      • Optionally: slide 1 from a second series
    """
    ids: list = []

    sm = [g for g in groups if g["type"] == "splash_menu"]
    if sm:
        screens = sm[0]["screens"]
        for s in screens[-2:]:          # last 2 (or 1) — the menu screen is last
            ids.append(s["id"])

    hubs = [g for g in groups if g["type"] == "hub"]
    if hubs:
        ids.append(hubs[0]["screens"][0]["id"])

    series = [g for g in groups if g["type"] == "case_series"]
    if series:
        for s in series[0]["screens"][:3]:   # challenge / plan / win
            ids.append(s["id"])
        if len(series) > 1 and len(ids) < 7:
            ids.append(series[1]["screens"][0]["id"])

    return ids[:8]


def _build_nav_graph(groups: list) -> dict:
    """Return {screen_name: [child_screen_names]} for the navigation commentary."""
    graph: dict = {}

    splash_names = [s.get("name", "")
                    for g in groups if g["type"] == "splash_menu"
                    for s in g["screens"]]
    hub_names    = [g["name"] for g in groups if g["type"] == "hub"]

    for n in splash_names:
        graph[n] = hub_names

    series_by_name = {g["name"]: [s.get("name", "") for s in g["screens"]]
                      for g in groups if g["type"] == "case_series"}

    for g in groups:
        if g["type"] == "hub":
            hub_n  = g["name"]
            related = [
                series_screens[0]
                for series_name, series_screens in series_by_name.items()
                if (series_name.lower() in hub_n.lower() or
                    hub_n.lower() in series_name.lower())
                if series_screens
            ]
            if related:
                graph[hub_n] = related

    for g in groups:
        if g["type"] == "case_series":
            screens = g["screens"]
            for i, s in enumerate(screens):
                nxt = screens[i + 1].get("name", "") if i < len(screens) - 1 else "contact"
                graph[s.get("name", "")] = [nxt]

    return graph


def _format_structure_for_prompt(analysis: dict) -> str:
    """Serialise the Figma structure analysis into a compact string for the HTML prompt.

    Designed to give the model:
      1. A clear page-flow overview
      2. Every hub screen with its sub-options
      3. Every case series with per-slide text extracts
      4. The navigation graph
    While staying within ~3 000 chars.
    """
    if not analysis:
        return ""

    groups    = analysis.get("screen_groups", [])
    nav_graph = analysis.get("nav_graph", {})
    lines: list = ["━━━ FIGMA CONTENT STRUCTURE ━━━", ""]

    # Overview
    sm_count  = sum(1 for g in groups if g["type"] == "splash_menu")
    hub_count = sum(1 for g in groups if g["type"] == "hub")
    cs_count  = sum(1 for g in groups if g["type"] == "case_series")
    lines.append(f"OVERVIEW: {sm_count} entry screen(s) · {hub_count} hub(s) · {cs_count} case series")
    lines.append("")

    # Entry screens
    for g in groups:
        if g["type"] != "splash_menu":
            continue
        lines.append("ENTRY SCREENS:")
        for s in g["screens"]:
            texts = s.get("texts", [])[:6]
            label = " | ".join(t[:80] for t in texts)
            lines.append(f'  "{s["name"]}": {label}')
        lines.append("")

    # Hub screens
    hub_lines = [g for g in groups if g["type"] == "hub"]
    if hub_lines:
        lines.append("HUB SCREENS (category pages — each leads to case studies):")
        for g in hub_lines:
            s = g["screens"][0]
            texts = s.get("texts", [])[:6]
            label = " | ".join(t[:60] for t in texts)
            lines.append(f'  "{g["name"]}": {label}')
        lines.append("")

    # Case series
    case_lines = [g for g in groups if g["type"] == "case_series"]
    if case_lines:
        lines.append("CASE SERIES (3-tab structure: tab 1 = Challenge, tab 2 = Game Plan, tab 3 = Win):")
        for g in case_lines:
            lines.append(f'  "{g["name"]}" ({len(g["screens"])} slides):')
            for s in g["screens"]:
                texts = s.get("texts", [])[:8]
                excerpt = " | ".join(t[:70] for t in texts)
                lines.append(f'    Slide {s.get("_slide_num","?")} "{s["name"]}": {excerpt}')
        lines.append("")

    # Nav graph (compact)
    if nav_graph:
        lines.append("NAVIGATION FLOW:")
        for src, dsts in list(nav_graph.items())[:20]:
            lines.append(f'  {src!r:40s} → {", ".join(str(d) for d in dsts[:4])}')
        lines.append("")

    lines.append(
        "DATA LAYER REQUIREMENT:\n"
        "Build const DATA = { ... } containing every case study. Each entry must have:\n"
        "  { title, parentHub, slides: [{tab, sectionLabel, body, bullets, stats, quote}] }\n"
        "Populate from the slide texts above — use the real content, not placeholders."
    )

    return "\n".join(lines)


async def _analyze_figma_structure(figma_params: dict) -> dict:
    """Orchestrate the full pre-HTML Figma analysis.

    1. Fetch frames for the provided node IDs to detect the parent container.
    2. Re-fetch all direct children of that container (= top-level screens).
    3. Group screens by naming pattern (splash/hub/case_series).
    4. Batch-fetch text content (depth=4) for all screens.
    5. Select representative frame IDs for image export.
    6. Build navigation graph.

    Returns a structured dict, or {} on any failure (caller degrades gracefully).
    """
    file_key     = figma_params.get("file_key")
    provided_ids = figma_params.get("ids")
    if not file_key:
        return {}

    try:
        # ── Step 1: fetch frames around the provided node(s) ──────────────────
        log.info("Figma analysis: fetching frames for ids=%s", provided_ids)
        frames_data = await get_frames(file_key=file_key, ids=provided_ids, depth=2)
        frames      = frames_data.get("frames", [])

        # ── Step 2: detect parent container ───────────────────────────────────
        container_id = _find_container_id(frames, provided_ids)
        log.info("Figma analysis: container_id=%s", container_id)

        # ── Step 3: re-fetch from container when it differs from provided IDs ─
        if container_id and container_id != (provided_ids or "").split(",")[0].strip():
            frames_data = await get_frames(file_key=file_key, ids=container_id, depth=2)
            frames      = frames_data.get("frames", [])

        # ── Step 4: keep only direct children of the container ────────────────
        top_screens = [f for f in frames if f.get("parentId") == container_id]
        if not top_screens:
            top_screens = [f for f in frames if f.get("type") == "FRAME"][:30]
        log.info("Figma analysis: %d top-level screens", len(top_screens))

        # ── Step 5: group by naming pattern ───────────────────────────────────
        groups = _group_screens_by_pattern(top_screens)

        # ── Step 6: batch-fetch text content (capped at 25 screens) ──────────
        screen_ids  = [s["id"] for s in top_screens[:25]]
        text_content: dict = {}
        if screen_ids:
            try:
                nodes_data = await get_nodes(
                    file_key=file_key,
                    ids=",".join(screen_ids),
                    depth=4,
                )
                for nid, wrap in (nodes_data.get("nodes") or {}).items():
                    doc   = wrap.get("document", wrap)
                    texts = _extract_all_texts(doc)
                    if texts:
                        text_content[nid] = texts
                log.info("Figma analysis: text content for %d screens", len(text_content))
            except Exception as exc:
                log.warning("Figma analysis: text fetch failed — %s", exc)

        for group in groups:
            for s in group.get("screens", []):
                s["texts"] = text_content.get(s["id"], [])

        # ── Step 7: representative images + nav graph ─────────────────────────
        image_ids = _select_representative_frames(groups)
        nav_graph = _build_nav_graph(groups)
        log.info("Figma analysis complete — %d image frames: %s", len(image_ids), image_ids)

        return {
            "screen_groups":  groups,
            "image_frame_ids": image_ids,
            "nav_graph":       nav_graph,
            "container_id":    container_id,
            "total_screens":   len(top_screens),
        }

    except Exception as exc:
        log.warning("Figma structure analysis failed — %s", exc)
        return {}


async def _fetch_figma_images(figma_params: dict, figma_data: dict,
                               explicit_ids: list = None) -> tuple:
    """Export Figma frames as PNG for vision input.

    When explicit_ids is provided (from _analyze_figma_structure), those
    representative frames are used directly.  Otherwise falls back to the
    legacy tree-walk via _extract_frame_ids.

    Returns (b64_images, url_map) where:
      b64_images  — list of base64 PNG strings for Anthropic vision API
      url_map     — {frame_id: cdn_url}

    Returns ([], {}) on any error so callers can degrade gracefully.
    """
    try:
        # Prefer analysis-derived IDs; fall back to legacy extractor
        frame_ids = explicit_ids or _extract_frame_ids(figma_data, max_frames=5)
        if not frame_ids:
            return [], {}

        exported = await export_images(
            file_key=figma_params.get("file_key"),
            ids=",".join(frame_ids),
            format="png",
            scale=1,          # 1× keeps file size reasonable
        )
        image_map = exported.get("images") or {}
        if not image_map:
            return [], {}

        b64_results = []
        url_map: dict = {}
        async with _httpx.AsyncClient(timeout=30) as client:
            for fid in frame_ids:
                url = image_map.get(fid)
                if not url:
                    continue
                url_map[fid] = url           # keep CDN URL for <img> embedding
                resp = await client.get(url)
                if resp.status_code == 200:
                    b64 = base64.b64encode(resp.content).decode()
                    b64_results.append(b64)
        return b64_results, url_map

    except Exception as exc:
        log.warning("Phase 4: frame image export/download failed — %s", exc)
        return [], {}


def _html_system(plan: dict, has_figma_content: bool = False,
                 has_figma_images: bool = False,
                 figma_structure: dict = None) -> str:
    """System prompt for the HTML phase.

    Modes (in descending priority):
      figma_structure — full structural analysis: navigation graph + all text content.
                        Unlocks <style> blocks, inline SVG, and data-layer injection.
      has_figma_images — screenshots present: suppress generic layout guide, derive from images.
      has_figma_content — raw Figma JSON in user message: extract real labels.
      none            — fallback: prescriptive layout guide + Tailwind constraints.
    """
    tokens        = plan.get("designTokens", {})
    pages         = plan.get("pages", [])[:8]   # raised cap — more pages now feasible
    content_types = plan.get("contentTypes", [])
    page_list     = "\n".join(
        f"  {i}. id=\"{p['name'].lower().replace(' ', '-')}\" — {p['name']}: {p.get('description', '')}"
        for i, p in enumerate(pages)
    )
    ct_list = ", ".join(ct["name"] for ct in content_types)

    brand_hex  = tokens.get("primaryColor",    "#6366f1")
    accent_hex = tokens.get("secondaryColor",  "#8b5cf6")
    bg_hex     = tokens.get("backgroundColor", "#ffffff")
    fg_hex     = tokens.get("textColor",       "#1f2937")
    nav_bg     = bg_hex if bg_hex != "#ffffff" else "#f8f8f8"

    has_structure = bool(figma_structure)

    # ── Design section ───────────────────────────────────────────────────────
    if has_structure or has_figma_images:
        design_section = (
            "━━━ FIGMA SCREENSHOTS = THE DESIGN SPEC ━━━\n"
            "Screenshots of the actual Figma screens are in the user message.\n"
            "Extract from them:\n"
            "  1. EXACT colors (background, card fill, accent, text) — express as CSS hex vars.\n"
            "  2. EXACT layout per screen type (splash, hub card grid, 3-tab case study, etc.).\n"
            "  3. EXACT typography scale (display, heading, body, label sizes & weights).\n"
            "  4. Component patterns (card radius, button shape, bottom nav bar, tab underline).\n"
            "  5. DO NOT embed <img> of the screenshots — reproduce as HTML/CSS."
        )
    else:
        design_section = (
            f"COLORS — use these exact hex values:\n"
            f"  Primary: {brand_hex}   Accent: {accent_hex}   BG: {bg_hex}   Text: {fg_hex}\n"
            "\n"
            "LAYOUT GUIDE per page type:\n"
            "- Home: large centred heading + touch-target card grid\n"
            "- Listing: full-width rows OR 2-col card grid with coloured header bands\n"
            "- Detail: numbered timeline story OR 3-col feature cards\n"
            "- Results: large bold stat numbers + CTA\n"
        )

    # ── CSS rules ────────────────────────────────────────────────────────────
    if has_structure or has_figma_images:
        css_rules = (
            "CSS RULES (Figma mode — full CSS unlocked):\n"
            "- Write a single <style> block immediately after <body> opens.\n"
            "  Define CSS custom properties (--color-primary, --color-accent, etc.) and\n"
            "  all structural rules (.page, .page.active, .kiosk, nav-bar, tab-bar, etc.).\n"
            "- Use Tailwind arbitrary values (bg-[#hex]) only for one-off utility overrides.\n"
            "- Inline SVG icons are allowed and preferred over emoji for visual accuracy.\n"
            "- Keep all transition/hover/active states as CSS, not inline JS."
        )
    else:
        css_rules = (
            "CSS RULES (no-Figma mode — Tailwind only):\n"
            "- NO <style> blocks — Tailwind arbitrary values only (bg-[#hex], text-[#hex]).\n"
            "- NO SVG — use 1–2 char emoji (🏠 📄 👥 🔍 ✅ 🚀 📊 ⭐ 🎯 📈).\n"
            "- Keep prose concise — 1 sentence descriptions, 5–10 word labels."
        )

    # ── Data layer instruction ────────────────────────────────────────────────
    if has_structure:
        data_instruction = (
            "DATA LAYER REQUIREMENT (mandatory when Figma structure is provided):\n"
            "1. Declare const DATA = { ... } at the top of <script>.\n"
            "2. Populate it with EVERY case study / content item using the REAL text from the\n"
            "   Figma structure block in the user message — do NOT invent placeholder copy.\n"
            "3. Each case study entry: { title, parentHub, slides: [{tab, sectionLabel, body,\n"
            "   bullets:[], stats:[], quote:''}] }\n"
            "4. Every page rendered at runtime must pull from DATA — never hardcode repeated HTML."
        )
    else:
        data_instruction = (
            "JS DATA PATTERN — use when cards/list items need individual content:\n"
            "  const ITEMS = [{id:0, title:'...', body:'...'}];\n"
            "  function renderDetail(id) { const item = ITEMS[id]; ... showPage('detail'); }"
        )

    return f"""You are completing an HTML prototype. The <head> with Tailwind CDN is already written.
Continue from inside the open <body> tag.

{design_section}

{css_rules}

{data_instruction}

INTERACTIVITY RULES — mandatory, no exceptions:
- Every clickable element (card, button, link, row) MUST have a working onclick handler.
- NEVER leave onclick stubs ("// TODO" or no-op functions).
- Multi-step flows: use a currentStep / currentTab state variable with real prev/next logic.
- Detail views driven by card clicks: use DATA + a render function — never N copies of HTML.
- All navigation links must work; Back and Home buttons must navigate correctly.
- Mentally walk every click path — every button must produce a visible, meaningful result.

PAGE STRUCTURE:
1. <style> block (Figma mode) OR omit (no-Figma mode).
2. One <div id="page-NAME" class="page"> per screen — first gets class="page active".
   In <style>: .page{{display:none}} .page.active{{display:flex;flex-direction:column}}
3. </body>
4. <script>
     const DATA = {{ ... }};   // populated with real Figma content
     function showPage(name){{document.querySelectorAll('.page').forEach(e=>e.classList.remove('active'));document.getElementById('page-'+name).classList.add('active');window.scrollTo(0,0);}}
     // ... all functions with REAL implementations
   </script>
5. </html>
6. </FILE>

Project: {plan.get("displayName", "Prototype")}
Pages ({len(pages)}): {page_list}
Content types: {ct_list}

End your output with </FILE>."""


def _html_prefill(plan: dict, figma_structure: dict = None) -> str:
    """Pre-written assistant prefill for the HTML phase.

    When figma_structure is provided (Figma mode), the prefill ends just after
    <body> — giving the model room to open its own <style> block immediately.

    When no structure is present (no-Figma mode), the prefill ends inside the
    already-open <body> tag so the model cannot backtrack and inject a <style>.
    """
    tokens    = plan.get("designTokens", {})
    primary   = tokens.get("primaryColor",   "#6366f1")
    secondary = tokens.get("secondaryColor", "#8b5cf6")
    bg        = tokens.get("backgroundColor", "")
    fg        = tokens.get("textColor",       "")
    font      = tokens.get("fontFamily",      "system-ui, sans-serif")
    title     = plan.get("displayName", "Prototype")

    bg_token   = bg or "#111827"
    fg_token   = fg or "#f9fafb"
    first_font = font.split(",")[0].strip().strip("'\"")

    _system_fonts = {"system-ui", "ui-sans-serif", "sans-serif", "serif",
                     "monospace", "ui-serif", "ui-monospace", "cursive", "fantasy"}
    is_web_font = first_font.lower() not in _system_fonts

    font_sans = f"['{first_font}','system-ui','sans-serif']" if is_web_font else "['system-ui','sans-serif']"

    tw_cfg = (
        'tailwind.config={'
        'theme:{extend:{'
        'colors:{'
        f'primary:"{primary}",'
        f'secondary:"{secondary}",'
        f'background:"{bg_token}",'
        f'foreground:"{fg_token}"'
        '},'
        f'fontFamily:{{sans:{font_sans}}}'
        '}}}'
    )

    font_link = ""
    if is_web_font:
        gf_param  = first_font.replace(" ", "+")
        font_link = (
            f'  <link rel="preconnect" href="https://fonts.googleapis.com">\n'
            f'  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
            f'  <link href="https://fonts.googleapis.com/css2?family={gf_param}'
            f':wght@300;400;500;600;700&display=swap" rel="stylesheet">\n'
        )

    head = (
        f'<FILE path="prototype.html">\n'
        f'<!DOCTYPE html>\n'
        f'<html lang="en">\n'
        f'<head>\n'
        f'  <meta charset="UTF-8">\n'
        f'  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'  <title>{title}</title>\n'
        f'{font_link}'
        f'  <script src="https://cdn.tailwindcss.com"></script>\n'
        f'  <script>{tw_cfg}</script>\n'
        f'</head>\n'
    )

    if figma_structure:
        # Figma mode: end prefill at opening <body> tag.
        # The model will write a <style> block first, then HTML pages, then <script>.
        return head + '<body>'
    else:
        # No-Figma mode: force the model into <body> with classes applied.
        # It cannot backtrack to write a <style> block before this point.
        body_classes = "min-h-screen font-sans"
        if bg:
            body_classes += f" bg-[{bg}]"
        if fg:
            body_classes += f" text-[{fg}]"
        return head + f'<body class="{body_classes}">'


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_chat(messages, framework: str, output_type: str, mode: str,
                   drupal_version: str, figma_params: dict):
    """
    Multi-phase prototype generation pipeline.

    framework:   'drupal10' | 'drupal11' | 'laravel' | 'claude'
    output_type: 'framework' | 'html' | 'both'
    mode:        'arch' | 'figma' | 'both' | 'none'
    """
    # Resolve Figma params: merge user-supplied values with server defaults so
    # Figma mode always has a valid file even when the user skipped the config step.
    if mode in ("figma", "both"):
        figma_params = _resolve_figma_params(figma_params)
        log.info("Phase 0: Resolved Figma params — %s", figma_params)

    tools = _select_tools(mode, framework, output_type)
    msgs  = [{"role": m.role, "content": m.content} for m in messages]

    # ── Phase 1: Plan ──────────────────────────────────────────────────────────
    log.info("Phase 1: Planning (%s / %s / %s)", framework, output_type, mode)
    plan_text, _ = await _call(
        system=_plan_system(framework, mode, drupal_version, figma_params),
        messages=msgs,
        tools=tools or None,
    )

    plan_match = re.search(r"<PLAN>(.*?)</PLAN>", plan_text, re.DOTALL)
    try:
        plan = json.loads(plan_match.group(1).strip()) if plan_match else {}
    except (json.JSONDecodeError, AttributeError):
        plan = {}

    # Deterministic design token extraction from Figma variables.
    # Fills backgroundColor/textColor/primaryColor/secondaryColor if Claude left defaults
    # or the plan was empty — does not overwrite values Claude already extracted.
    if mode in ("figma", "both") and figma_params:
        try:
            vars_data = await get_variables(file_key=figma_params.get("file_key"))
            tokens = plan.setdefault("designTokens", {})
            all_color_vars = [
                v for v in (vars_data.get("variables") or [])
                if v.get("hex")
            ]

            for v in all_color_vars:
                n = (v.get("name") or "").lower().replace("/", " ").replace("-", " ").replace("_", " ")
                h = v.get("hex")
                if any(k in n for k in ("background", "bg ", "surface", "canvas", "base", "page")):
                    tokens.setdefault("backgroundColor", h)
                elif any(k in n for k in ("text", "foreground", "on ", "body", "content", "label")):
                    tokens.setdefault("textColor", h)
                elif any(k in n for k in ("primary", "brand", "accent", "action", "blue", "key")):
                    tokens.setdefault("primaryColor", h)
                elif any(k in n for k in ("secondary", "support", "purple", "violet")):
                    tokens.setdefault("secondaryColor", h)

            # Fallback: if primaryColor still unset, pick the first non-white/black color
            if "primaryColor" not in tokens and all_color_vars:
                neutral_hex = {"#ffffff", "#000000", "#fff", "#000"}
                for v in all_color_vars:
                    h = v.get("hex", "").lower()
                    if h not in neutral_hex and h not in (tokens.get("backgroundColor",""), tokens.get("textColor","")):
                        tokens.setdefault("primaryColor", v["hex"])
                        break

            # Extract font family from variables if present
            for v in (vars_data.get("variables") or []):
                if v.get("type") == "STRING":
                    n = (v.get("name") or "").lower()
                    val = v.get("value", "")
                    if val and any(k in n for k in ("font", "typeface", "typography")):
                        tokens.setdefault("fontFamily", val)

            log.info("Design tokens after extraction: %s", tokens)
        except Exception as exc:
            log.warning("Deterministic token extraction failed — %s", exc)

    # Resolve framework & Drupal version from plan (handles 'claude' mode)
    resolved_framework  = plan.get("framework", framework if framework != "claude" else "drupal11")
    resolved_drupal_ver = plan.get("drupalVersion") or drupal_version or "11"
    # Normalise: plan may return "11" or "Drupal 11"
    resolved_drupal_ver = resolved_drupal_ver.replace("Drupal ", "").strip() or "11"
    is_drupal = resolved_framework in ("drupal10", "drupal11")
    log.info("Plan complete — project: %s, framework: %s", plan.get("projectName"), resolved_framework)

    all_files: list = []
    generation_msg = [{"role": "user", "content": f"Generate files for: {plan.get('displayName', 'prototype')}"}]

    # ── Phase 2 & 3: Framework-specific files ─────────────────────────────────
    if output_type in ("framework", "both"):
        log.info("Phase 2: Backend files (waiting %ds for rate-limit reset)…", PHASE_DELAY)
        await asyncio.sleep(PHASE_DELAY)
        if is_drupal:
            # Phase 2: Drupal module (backend) — needs full budget for field.storage + field.field YAMLs
            backend_text, _ = await _call(
                system=_drupal_backend_system(plan, resolved_drupal_ver),
                messages=generation_msg,
                max_tokens=HTML_MAX_TOKENS,
            )
            all_files.extend(_parse_files(backend_text))
            log.info("Phase 2 done — %d backend files", len(all_files))

            # Phase 3: Drupal theme static assets (info, libraries, CSS)
            log.info("Phase 3: Drupal theme static assets (waiting %ds)…", PHASE_DELAY)
            await asyncio.sleep(PHASE_DELAY)
            theme_text, _ = await _call(
                system=_drupal_theme_system(plan, resolved_drupal_ver),
                messages=generation_msg,
            )
            all_files.extend(_parse_files(theme_text))
            log.info("Phase 3 done — %d total files so far", len(all_files))

            # Phase 3b: Drupal Twig templates (separate call — ensures token budget)
            log.info("Phase 3b: Drupal Twig templates (waiting %ds)…", PHASE_DELAY)
            await asyncio.sleep(PHASE_DELAY)
            twig_text, _ = await _call(
                system=_drupal_twig_system(plan, resolved_drupal_ver),
                messages=generation_msg,
                max_tokens=HTML_MAX_TOKENS,
            )
            twig_files = _parse_files(twig_text)
            all_files.extend(twig_files)
            log.info("Phase 3b done — %d Twig file(s), %d total", len(twig_files), len(all_files))

            # README — generated deterministically from the plan (no LLM call)
            all_files.append({
                "path":    "README.md",
                "content": _build_drupal_readme(plan, resolved_drupal_ver),
            })
            log.info("README.md appended")

        else:
            # Phase 2: Laravel backend
            backend_text, _ = await _call(
                system=_laravel_backend_system(plan),
                messages=generation_msg,
            )
            all_files.extend(_parse_files(backend_text))
            log.info("Phase 2 done — %d backend files", len(all_files))

            # Phase 3: Laravel views
            log.info("Phase 3: Laravel views (waiting %ds)…", PHASE_DELAY)
            await asyncio.sleep(PHASE_DELAY)
            theme_text, _ = await _call(
                system=_laravel_theme_system(plan),
                messages=generation_msg,
            )
            all_files.extend(_parse_files(theme_text))
            log.info("Phase 3 done — %d total files so far", len(all_files))

    # ── Phase 4: HTML prototype ────────────────────────────────────────────────
    if output_type in ("html", "both"):
        log.info("Phase 4: HTML prototype (waiting %ds for extended token budget)…", HTML_PHASE_DELAY)
        await asyncio.sleep(HTML_PHASE_DELAY)

        figma_content    = ""
        figma_images     = []
        figma_image_urls = {}
        figma_analysis   = {}   # result of _analyze_figma_structure

        if mode in ("figma", "both") and figma_params:

            # ── Phase 4a: structural analysis (replaces ad-hoc node fetch) ───
            log.info("Phase 4a: Figma structural analysis…")
            try:
                figma_analysis = await _analyze_figma_structure(figma_params)
                log.info(
                    "Phase 4a: analysis complete — %d screen groups, %d image frames, "
                    "%d total screens",
                    len(figma_analysis.get("screen_groups", [])),
                    len(figma_analysis.get("image_frame_ids", [])),
                    figma_analysis.get("total_screens", 0),
                )
            except Exception as exc:
                log.warning("Phase 4a: structural analysis failed — %s", exc)

            # ── Phase 4b: export representative frame images ─────────────────
            try:
                log.info("Phase 4b: Exporting representative Figma frame images…")
                explicit_ids = figma_analysis.get("image_frame_ids") or None
                figma_images, figma_image_urls = await _fetch_figma_images(
                    figma_params, {}, explicit_ids=explicit_ids
                )
                log.info(
                    "Phase 4b: %d frame image(s) exported",
                    len(figma_images),
                )
            except Exception as exc:
                log.warning("Phase 4b: Figma image export failed — %s", exc)

        # ── Build user message ────────────────────────────────────────────────
        display_name = plan.get("displayName", "prototype")
        base_text    = f"Generate HTML prototype for: {display_name}"

        # Inject the structured content analysis when available
        if figma_analysis:
            structure_str = _format_structure_for_prompt(figma_analysis)
            if structure_str:
                base_text += "\n\n" + structure_str
        elif figma_content:
            # Legacy fallback: raw Figma JSON snippet
            base_text += (
                "\n\nFigma design content (extract real text labels, titles, descriptions):\n"
                + figma_content
            )

        if figma_images:
            # Multimodal: screenshots first (visual spec), then text content
            user_content: list = [
                {
                    "type": "text",
                    "text": (
                        "The following screenshots are the EXACT Figma screens for this project.\n"
                        "For each screenshot, extract:\n"
                        "  • Exact background color, card fill, accent color, text color\n"
                        "  • Exact layout (kiosk frame, bottom nav bar, tab underlines, card grid)\n"
                        "  • Typography scale (display size, heading weight, body size)\n"
                        "  • Component patterns (pill buttons, card radius, icon style)\n"
                        "Reproduce these faithfully in your <style> block and HTML. "
                        "Do NOT embed these screenshots as <img> tags — recreate them in code."
                    ),
                }
            ]
            for b64 in figma_images:
                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                })
            user_content.append({"type": "text", "text": base_text})
        else:
            user_content = base_text

        # ── Prefill + call ────────────────────────────────────────────────────
        prefill = _html_prefill(plan, figma_structure=figma_analysis or None)

        html_messages = [
            {"role": "user",      "content": user_content},
            {"role": "assistant", "content": prefill},
        ]

        html_continuation, html_stop = await _call(
            system=_html_system(
                plan,
                has_figma_content=bool(figma_content or figma_analysis),
                has_figma_images=bool(figma_images),
                figma_structure=figma_analysis or None,
            ),
            messages=html_messages,
            max_tokens=HTML_MAX_TOKENS,
        )

        full_html  = prefill + html_continuation
        html_files = _parse_files(full_html)
        all_files.extend(html_files)
        log.info(
            "Phase 4 done — stop_reason=%s, continuation_chars=%d, files=%d, total=%d",
            html_stop, len(html_continuation), len(html_files), len(all_files),
        )

    # ── Build response ─────────────────────────────────────────────────────────
    project_name = plan.get("projectName", "prototype")
    display_name = plan.get("displayName", "Prototype")
    summary      = plan.get("summary", "Prototype generated successfully.")

    artifacts = {
        "projectName":   project_name,
        "displayName":   display_name,
        "framework":     resolved_framework,
        "drupalVersion": resolved_drupal_ver if is_drupal else None,
        "outputType":    output_type,
        "files":         all_files,
        "zipName":       project_name,
    } if all_files else None

    file_count = len(all_files)
    if all_files:
        # Build a breakdown by file type for a more informative message
        twig_count   = sum(1 for f in all_files if f["path"].endswith(".twig"))
        php_count    = sum(1 for f in all_files if f["path"].endswith(".php") or f["path"].endswith(".module"))
        yml_count    = sum(1 for f in all_files if f["path"].endswith(".yml"))
        css_count    = sum(1 for f in all_files if f["path"].endswith(".css"))
        html_count   = sum(1 for f in all_files if f["path"].endswith(".html"))
        has_readme   = any(f["path"] == "README.md" for f in all_files)
        breakdown    = ", ".join(filter(None, [
            f"{yml_count} YML config{'s' if yml_count != 1 else ''}" if yml_count else None,
            f"{twig_count} Twig template{'s' if twig_count != 1 else ''}" if twig_count else None,
            f"{php_count} PHP file{'s' if php_count != 1 else ''}"        if php_count else None,
            f"{css_count} CSS file{'s' if css_count != 1 else ''}"        if css_count else None,
            f"{html_count} HTML prototype{'s' if html_count != 1 else ''}" if html_count else None,
            "README.md"                                                     if has_readme else None,
        ]))
        detail = f" ({breakdown})" if breakdown else ""
        display = f"{summary}\n\n✅ Generated {file_count} file{'s' if file_count != 1 else ''}{detail}."
    else:
        display = summary

    # When user chose "Claude Chooses", prepend what framework was selected and why.
    if framework == "claude" and resolved_framework:
        fw_names = {"drupal10": "Drupal 10", "drupal11": "Drupal 11", "laravel": "Laravel"}
        fw_name  = fw_names.get(resolved_framework, resolved_framework)
        rationale = plan.get("frameworkRationale", "")
        fw_note   = f"Framework selected: {fw_name}."
        if rationale:
            fw_note += f" {rationale}"
        display = f"{fw_note}\n\n{display}"

    return display, artifacts

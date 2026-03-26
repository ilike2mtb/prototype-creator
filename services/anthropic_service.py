import anthropic, asyncio, base64, json, logging, re
import httpx as _httpx
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
HTML_MAX_TOKENS  = 8000   # HTML phase gets full budget; with prefill no CSS is wasted
MAX_TOOL_CHARS   = 4000
PHASE_DELAY      = 65     # seconds between phases — lets the 4 000/min token bucket reset
HTML_PHASE_DELAY = 130    # longer wait before HTML phase to refill token bucket fully
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


async def _fetch_figma_images(figma_params: dict, figma_data: dict) -> tuple:
    """Export the first few Figma frames as PNG.

    Returns (b64_images, url_map) where:
      b64_images  — list of base64 PNG strings for Anthropic vision API
      url_map     — {frame_id: cdn_url} so the prototype can embed <img> tags

    Returns ([], {}) on any error so callers can degrade gracefully.
    """
    try:
        frame_ids = _extract_frame_ids(figma_data, max_frames=5)
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
                 has_figma_images: bool = False) -> str:
    """System prompt for the HTML phase.
    Used together with an assistant-prefill message (see run_chat Phase 4),
    so the model always continues from inside an already-open <body> — making
    it structurally impossible to write a <style> block first.
    When has_figma_content=True, Figma node data is present in the user message.
    When has_figma_images=True, Figma screenshots are in the user message — the
    generic layout guide is suppressed entirely; screenshots ARE the layout spec.
    """
    tokens        = plan.get("designTokens", {})
    pages         = plan.get("pages", [])[:5]   # hard cap — token budget supports max 5 pages
    content_types = plan.get("contentTypes", [])
    page_list     = "\n".join(
        f"  {i}. id=\"p{i}\" — {p['name']}: {p.get('description', '')}"
        for i, p in enumerate(pages)
    )
    ct_list = ", ".join(ct["name"] for ct in content_types)

    # Determine brand colors (from plan tokens, or safe fallbacks)
    brand_hex  = tokens.get("primaryColor",    "#6366f1")
    accent_hex = tokens.get("secondaryColor",  "#8b5cf6")
    bg_hex     = tokens.get("backgroundColor", "#ffffff")
    fg_hex     = tokens.get("textColor",       "#1f2937")
    nav_bg     = bg_hex if bg_hex != "#ffffff" else "#f8f8f8"

    figma_content_note = (
        "\n⭐ FIGMA TEXT DATA: Real design content is in the user message. "
        "Extract actual labels, titles, and descriptions from it — do NOT invent placeholder text."
    ) if has_figma_content else ""

    if has_figma_images:
        # Screenshots present — they ARE the layout specification.
        # Suppress generic layout guide entirely; Claude must derive layout from images.
        layout_section = (
            "━━━ FIGMA SCREENSHOTS ARE THE LAYOUT SPEC ━━━\n"
            "You have been given actual Figma design screenshots. Your ONLY job is to faithfully\n"
            "reproduce what you see in those screenshots as HTML/Tailwind. Rules:\n"
            "\n"
            "1. LAYOUT — replicate it exactly:\n"
            "   - Study each screenshot's structure: hero, grid, list, timeline, stat blocks, etc.\n"
            "   - NEVER default to a generic '2×2 card grid' if the screenshot shows something else.\n"
            "   - If a page shows a full-width hero with large type + CTA button, build that.\n"
            "   - If a page shows a numbered timeline or step-by-step story, build that.\n"
            "   - If a page shows large stat numbers, build those.\n"
            "   - Each page MUST have the layout shown in its corresponding screenshot.\n"
            "\n"
            "2. COLORS — pick exact values from the screenshots:\n"
            "   - Identify every distinct color visible: nav background, card headers, body bg, text, accents.\n"
            "   - Use ONLY Tailwind arbitrary-value hex: bg-[#rrggbb], text-[#rrggbb], border-[#rrggbb].\n"
            "   - NEVER use named Tailwind colors (blue-600, violet-500, gray-800, etc.).\n"
            "   - Fallback tokens from the design system: brand={brand_hex}, accent={accent_hex},\n"
            f"     bg={bg_hex}, text={fg_hex} — use these when a color in a screenshot is ambiguous.\n"
            "\n"
            "3. TYPOGRAPHY — match weight, size, and spacing:\n"
            "   - Large display headings → text-4xl font-light or font-bold as shown.\n"
            "   - Card titles → font-semibold text-lg or text-xl.\n"
            "   - Body copy → text-sm or text-base text-[#hex]/70.\n"
            "\n"
            "4. NAV — match the exact header shown: brand name position, nav link style, background color.\n"
            "\n"
            "5. DO NOT embed <img> tags of the screenshots — build the HTML/CSS equivalent."
        )
    else:
        # No screenshots — use prescriptive layout guide as fallback
        layout_section = (
            f"COLORS — use these exact values (do not substitute):\n"
            f"  Brand/primary: {brand_hex}    Accent/secondary: {accent_hex}\n"
            f"  Background: {bg_hex}          Text: {fg_hex}\n"
            f"  Use bg-[{brand_hex}] not bg-primary (Tailwind JIT may not resolve custom tokens).\n"
            "\n"
            "LAYOUT GUIDE per page type:\n"
            "- Home/Landing: Large centred heading + 2×2 grid of large touch-target cards (h-40) with brand-colored tops\n"
            "- Listing/Challenges: Full-width clickable rows OR 2-col card grid with colored header bands\n"
            "- Detail/Case study: Vertical story steps (numbered timeline) OR 3-col feature cards\n"
            "- Results/Metrics: Large bold stat numbers in coloured circles + CTA button\n"
            "- Directory/All: Filter pills row + 2-col card grid\n"
            "\n"
            f"Card template:\n"
            f'<div class="rounded-2xl overflow-hidden shadow-lg border border-[{fg_hex}]/10">\n'
            f'  <div class="h-28 bg-[{brand_hex}] flex items-center px-6">\n'
            f'    <span class="text-3xl mr-3">📄</span>\n'
            f'    <h3 class="font-semibold text-white text-lg leading-tight">Card Title</h3>\n'
            f'  </div>\n'
            f'  <div class="p-5 bg-white text-[{fg_hex}]/70 text-sm">Brief description.</div>\n'
            f'</div>'
        )

    return f"""You are completing an HTML prototype. The <head> with Tailwind CDN is pre-written. \
Continue from inside the open <body> tag.
{figma_content_note}
{layout_section}

INTERACTIVITY RULES — these are mandatory, not optional:
- Every clickable element (card, button, link, row) MUST have a working onclick handler.
- NEVER leave onclick stubs like "// TODO" or functions that do nothing.
- Multi-step flows (journeys, wizards, slideshows): implement real step state in JS.
  Use a currentStep variable; prev/next buttons must actually advance/retreat through steps.
- If cards lead to a detail view, the detail content must differ per card — use a JS data
  array and a render function, NOT N copies of hardcoded HTML.
- All navigation links in the nav must work (showPage or equivalent).
- Test your logic mentally: clicking every button should produce a visible, meaningful result.

JS DATA PATTERN — use this when cards or list items need individual content:
  const ITEMS = [ {{id:0, title:'...', body:'...'}}, ... ];
  function renderDetail(id) {{
    const item = ITEMS[id];
    document.getElementById('detail-title').textContent = item.title;
    // ... populate other fields
    showPage('detail');
  }}

TOKEN BUDGET RULES:
- NO <style> blocks — Tailwind arbitrary values only (bg-[#hex], text-[#hex])
- NO SVG — use 1-2 char emoji (🏠 📄 👥 🔍 ✅ 🚀 📊 ⭐ 🎯 📈)
- Keep prose concise — 1 sentence descriptions, 5-10 word labels
- MUST complete ALL {len(pages)} pages with working interactions

Project: {plan.get("displayName", "Prototype")}
Pages ({len(pages)}):
{page_list}
Content types: {ct_list}

Write in this order:
1. <nav class="bg-[{nav_bg}] border-b px-6 py-4 flex items-center justify-between sticky top-0 z-50">
     Brand name left | navigation buttons right (each calls showPage or a named function)
   </nav>
2. One <div id="page-NAME" class="page"> per page — first gets class="page active", rest "page".
   Use CSS: .page{{display:none}} .page.active{{display:flex;flex-direction:column}}
3. </body>
4. <script>
     // State variables for multi-step flows
     function showPage(name){{document.querySelectorAll('.page').forEach(e=>e.classList.remove('active'));document.getElementById('page-'+name).classList.add('active');window.scrollTo(0,0);}}
     // ... all other functions with REAL implementations (no stubs)
     document.addEventListener('DOMContentLoaded',()=>showPage('{pages[0]["name"].lower().replace(" ","-") if pages else "home"}'));
   </script>
5. </html>
6. </FILE>

End your output with </FILE>."""


def _html_prefill(plan: dict) -> str:
    """Pre-written assistant content for the HTML phase.
    Sending this as the last assistant message forces the model to continue
    from inside <body> — it cannot backtrack and write CSS.
    """
    tokens    = plan.get("designTokens", {})
    primary   = tokens.get("primaryColor",   "#6366f1")
    secondary = tokens.get("secondaryColor", "#8b5cf6")
    bg        = tokens.get("backgroundColor", "")
    fg        = tokens.get("textColor",       "")
    font      = tokens.get("fontFamily",      "system-ui, sans-serif")
    title     = plan.get("displayName", "Prototype")

    # Build a valid JS object literal (no f-string brace collision)
    bg_token   = bg or "#111827"
    fg_token   = fg or "#f9fafb"
    first_font = font.split(",")[0].strip().strip("'\"")

    # Google Fonts: load the font if it's a named web font (not a system font)
    _system_fonts = {"system-ui", "ui-sans-serif", "sans-serif", "serif",
                     "monospace", "ui-serif", "ui-monospace", "cursive", "fantasy"}
    is_web_font = first_font.lower() not in _system_fonts

    # Build Tailwind fontFamily — only include the named font if it's a real web font
    if is_web_font:
        font_sans = f"['{first_font}','system-ui','sans-serif']"
    else:
        font_sans = "['system-ui','sans-serif']"

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

    # Apply backgroundColor/textColor directly to <body> so the model cannot
    # accidentally override them. Falls back to Tailwind defaults if not set.
    body_classes = "min-h-screen font-sans"
    if bg:
        body_classes += f" bg-[{bg}]"
    if fg:
        body_classes += f" text-[{fg}]"

    return (
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
        f'<body class="{body_classes}">'
    )


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

        # Fetch Figma node content (text labels) + frame images (visual style + embed).
        figma_content    = ""
        figma_images     = []   # base64 PNG strings for Anthropic vision
        figma_image_urls = {}   # {frame_id: cdn_url} for <img> embedding
        figma_raw_data   = {}

        if mode in ("figma", "both") and figma_params:
            try:
                log.info("Phase 4: Fetching Figma node content for HTML guidance…")
                figma_raw_data = await get_nodes(
                    file_key=figma_params.get("file_key"),
                    ids=figma_params.get("ids"),
                    depth=figma_params.get("depth", 2),
                )
                figma_content = truncate(figma_raw_data)
                log.info("Phase 4: Figma content fetched (%d chars)", len(figma_content))
            except Exception as exc:
                log.warning("Phase 4: Figma node fetch failed — %s", exc)

            try:
                log.info("Phase 4: Exporting Figma frame images for style matching + embedding…")
                figma_images, figma_image_urls = await _fetch_figma_images(
                    figma_params, figma_raw_data
                )
                log.info(
                    "Phase 4: %d Figma frame image(s) downloaded, %d URL(s) for embedding",
                    len(figma_images), len(figma_image_urls),
                )
            except Exception as exc:
                log.warning("Phase 4: Figma image export failed — %s", exc)

        # Prefill: pre-write the <head> with Tailwind CDN so the model is forced
        # to continue from inside <body> — it cannot write a <style> block first.
        prefill = _html_prefill(plan)

        # Build the user message as a multimodal content list when images exist,
        # or a plain string otherwise.
        display_name = plan.get("displayName", "prototype")
        base_text = f"Generate HTML prototype for: {display_name}"

        if figma_content:
            base_text += (
                "\n\nFigma design content (extract real text labels, titles, descriptions):\n"
                + figma_content
            )

        if figma_images:
            # Multimodal: images first so the model can reference them, text last
            user_content: list = []
            user_content.append({
                "type": "text",
                "text": (
                    "The following screenshots are the actual Figma design frames for this project. "
                    "Your primary job is to REPLICATE these layouts in HTML/Tailwind — not to invent "
                    "generic layouts. For each screenshot:\n"
                    "• Identify the exact layout pattern (hero, grid, timeline, stat blocks, list, etc.)\n"
                    "• Extract the exact colors visible (nav background, card fill, body bg, text, accents)\n"
                    "• Note the typography scale (display, heading, body sizes)\n"
                    "• Note component patterns (card shape, button style, icon treatment)\n"
                    "Each page in the HTML must visually match its corresponding screenshot. "
                    "Do NOT default to a generic card grid if the screenshot shows something different."
                ),
            })
            for b64 in figma_images:
                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                })
            user_content.append({"type": "text", "text": base_text})
        else:
            user_content = base_text

        html_messages = [
            {"role": "user",      "content": user_content},
            {"role": "assistant", "content": prefill},
        ]

        html_continuation, html_stop = await _call(
            system=_html_system(
                plan,
                has_figma_content=bool(figma_content),
                has_figma_images=bool(figma_images),
            ),
            messages=html_messages,
            max_tokens=HTML_MAX_TOKENS,
        )

        # The full file = prefill + model continuation; parse together
        full_html = prefill + html_continuation
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

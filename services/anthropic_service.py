import anthropic, asyncio, base64, json, logging, re
import httpx as _httpx
from config import settings
from services.integrations import (
    get_frames, get_nodes, export_images,
    get_variables, get_components, get_styles,
    get_file_summary,
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
        "get_figma_variables", "get_figma_components", "get_figma_styles",
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
    return f"""You are an expert Drupal {ver} developer. Generate backend module files for this project.

Project plan:
{json.dumps(plan, indent=2)}

Generate these files:
- MODULE_NAME.info.yml
- config/install/node.type.CONTENT_TYPE.yml for each content type
- config/install/field.storage.node.*.yml for custom fields
- config/install/views.view.*.yml for listing views (keep views config concise)
- MODULE_NAME.module (only if custom hooks are genuinely needed)

Format each file EXACTLY like this — no explanatory text, only FILE blocks:
<FILE path="web/modules/custom/MODULE_NAME/MODULE_NAME.info.yml">
file content here
</FILE>

Keep Drupal {ver} config schema. Omit boilerplate comments. Use realistic machine names."""


def _drupal_theme_system(plan: dict, drupal_version: str) -> str:
    ver = drupal_version or "11"
    tokens = plan.get("designTokens", {})
    return f"""You are an expert Drupal {ver} theme developer. Generate theme files for this project.

Project plan:
{json.dumps(plan, indent=2)}

Design tokens: {json.dumps(tokens)}

Generate these files:
- THEME_NAME.info.yml (base theme: false, list all regions, attach libraries)
- THEME_NAME.libraries.yml
- css/style.css — define CSS custom properties from design tokens, then use them throughout:
  :root {{ --color-primary: {tokens.get('primaryColor','#6366f1')}; --color-secondary: {tokens.get('secondaryColor','#8b5cf6')}; --color-bg: {tokens.get('backgroundColor','#111827')}; --color-text: {tokens.get('textColor','#f9fafb')}; }}
  body {{ background: var(--color-bg); color: var(--color-text); }}
- templates/page.html.twig (main layout with {{ page.header }}, {{ page.content }}, {{ page.footer }})
- templates/node--CONTENT_TYPE.html.twig for each content type

Format each file EXACTLY like this — no explanatory text, only FILE blocks:
<FILE path="web/themes/custom/THEME_NAME/THEME_NAME.info.yml">
file content here
</FILE>

Use realistic names derived from the project plan. Make the CSS professional and responsive."""


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


def _extract_frame_ids(figma_data: dict, max_frames: int = 3) -> list:
    """Walk the Figma node tree and return the first N FRAME node IDs."""
    ids: list = []

    def walk(node):
        if len(ids) >= max_frames:
            return
        if isinstance(node, dict):
            if node.get("type") == "FRAME":
                ids.append(node["id"])
            for child in node.get("children") or []:
                walk(child)

    for node_info in (figma_data.get("nodes") or {}).values():
        walk(node_info)

    return ids


async def _fetch_figma_images(figma_params: dict, figma_data: dict) -> list:
    """Export the first few Figma frames as PNG and return a list of
    base64-encoded images suitable for the Anthropic vision API.
    Returns [] on any error so callers can degrade gracefully.
    """
    try:
        frame_ids = _extract_frame_ids(figma_data, max_frames=2)
        if not frame_ids:
            return []

        exported = await export_images(
            file_key=figma_params.get("file_key"),
            ids=",".join(frame_ids),
            format="png",
            scale=1,          # 1× keeps file size reasonable for an iPad frame
        )
        image_map = exported.get("images") or {}
        if not image_map:
            return []

        results = []
        async with _httpx.AsyncClient(timeout=30) as client:
            for fid in frame_ids:
                url = image_map.get(fid)
                if not url:
                    continue
                resp = await client.get(url)
                if resp.status_code == 200:
                    b64 = base64.b64encode(resp.content).decode()
                    results.append(b64)
        return results

    except Exception as exc:
        log.warning("Phase 4: frame image export/download failed — %s", exc)
        return []


def _html_system(plan: dict, has_figma_content: bool = False,
                 has_figma_images: bool = False) -> str:
    """System prompt for the HTML phase.
    Used together with an assistant-prefill message (see run_chat Phase 4),
    so the model always continues from inside an already-open <body> — making
    it structurally impossible to write a <style> block first.
    When has_figma_content=True, Figma node data is present in the user message
    and the model should use that real content instead of inventing placeholders.
    """
    tokens        = plan.get("designTokens", {})
    pages         = plan.get("pages", [])[:5]   # hard cap — token budget supports max 5 pages
    content_types = plan.get("contentTypes", [])
    page_list     = "\n".join(
        f"  {i}. id=\"p{i}\" — {p['name']}: {p.get('description', '')}"
        for i, p in enumerate(pages)
    )
    ct_list = ", ".join(ct["name"] for ct in content_types)

    figma_content_note = (
        "\n⭐ FIGMA DATA: Real design content is in the user message. "
        "Extract actual text labels, component names, titles, and descriptions from it. "
        "Use that real content to populate cards and headings — do NOT invent placeholder text when Figma provides it."
    ) if has_figma_content else ""

    figma_style_note = (
        "\n🎨 FIGMA IMAGES: Figma design screenshots are included in the user message. "
        "Analyse the images and replicate the visual style:\n"
        "- Extract the exact background color and apply it to <body> using Tailwind arbitrary values: class=\"bg-[#hex] ...\"\n"
        "- Extract text colors and apply them: text-[#hex]\n"
        "- Match the card/panel style (light cards on dark bg, or dark cards on light bg, etc.)\n"
        "- Match the nav bar background and text color\n"
        "- Use Tailwind arbitrary values freely: bg-[#1a1f2e], text-[#e2e8f0], border-[#2d3748], etc.\n"
        "- Do NOT default to dark bg-gray-950 — use the actual Figma background color."
    ) if has_figma_images else ""

    return f"""You are completing an HTML prototype. The <head> with Tailwind CDN is pre-written. \
Continue from inside the open <body> tag.
{figma_content_note}{figma_style_note}
EFFICIENCY RULES (token budget is limited — follow strictly):
- NO <style> blocks or custom CSS — Tailwind only
- NO SVG icons — use 1-2 char emoji instead (🏠 📄 👥 🔍 etc.)
- NO long lorem ipsum — use short (5-10 word) placeholder text
- Cards must be brief: just a coloured header + title + 1 line description
- Max 4 cards per section — do not add more
- MUST generate ALL {len(pages)} pages — finishing all pages matters more than detail in any one

Project: {plan.get("displayName", "Prototype")}
Pages ({len(pages)}):
{page_list}
Content types: {ct_list}

Write in this order — no deviations:
1. <nav> — dark bar: project name left, one <button onclick="showPage(N)"> per page right
2. {len(pages)} sections:
   <section id="p0" class="page p-8 max-w-6xl mx-auto">heading + 4 simple cards</section>
   <section id="p1" class="page hidden p-8 max-w-6xl mx-auto">heading + 4 simple cards</section>
   ... repeat for all {len(pages)} pages ...
3. </body>
4. <script>
     function showPage(n){{document.querySelectorAll('.page').forEach(e=>e.classList.add('hidden'));document.getElementById('p'+n).classList.remove('hidden');}}
     document.addEventListener('DOMContentLoaded',()=>showPage(0));
   </script>
5. </html>
6. </FILE>

Simple card template (copy this pattern — do not elaborate):
<div class="bg-white/10 rounded-xl overflow-hidden shadow">
  <div class="h-24 bg-gradient-to-r from-primary to-secondary flex items-center px-4">
    <span class="text-2xl">📄</span>
    <h3 class="ml-3 font-bold text-white">Card Title</h3>
  </div>
  <div class="p-4 text-foreground/70 text-sm">Brief description here.</div>
</div>

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
    title     = plan.get("displayName", "Prototype")

    # Build a valid JS object literal (no f-string brace collision)
    bg_token = bg or "#111827"
    fg_token = fg or "#f9fafb"
    tw_cfg = (
        'tailwind.config={'
        'theme:{extend:{colors:{'
        f'primary:"{primary}",'
        f'secondary:"{secondary}",'
        f'background:"{bg_token}",'
        f'foreground:"{fg_token}"'
        '}}}}'
    )

    # Apply backgroundColor/textColor directly to <body> so the model cannot
    # accidentally override them. Falls back to Tailwind defaults if not set.
    body_classes = "min-h-screen"
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
            for v in (vars_data.get("variables") or []):
                n = (v.get("name") or "").lower()
                h = v.get("hex")
                if not h:
                    continue
                if any(k in n for k in ("background", "bg", "surface", "canvas")):
                    tokens.setdefault("backgroundColor", h)
                elif any(k in n for k in ("text", "foreground", "on-", "content")):
                    tokens.setdefault("textColor", h)
                elif "primary" in n:
                    tokens.setdefault("primaryColor", h)
                elif "secondary" in n:
                    tokens.setdefault("secondaryColor", h)
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
            # Phase 2: Drupal module (backend)
            backend_text, _ = await _call(
                system=_drupal_backend_system(plan, resolved_drupal_ver),
                messages=generation_msg,
            )
            all_files.extend(_parse_files(backend_text))
            log.info("Phase 2 done — %d backend files", len(all_files))

            # Phase 3: Drupal theme
            log.info("Phase 3: Drupal theme (waiting %ds)…", PHASE_DELAY)
            await asyncio.sleep(PHASE_DELAY)
            theme_text, _ = await _call(
                system=_drupal_theme_system(plan, resolved_drupal_ver),
                messages=generation_msg,
            )
            all_files.extend(_parse_files(theme_text))
            log.info("Phase 3 done — %d total files so far", len(all_files))

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

        # Fetch Figma node content (text labels) + frame images (visual style).
        figma_content  = ""
        figma_images   = []   # list of base64-encoded PNG strings
        figma_raw_data = {}

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
                log.info("Phase 4: Exporting Figma frame images for style matching…")
                figma_images = await _fetch_figma_images(figma_params, figma_raw_data)
                log.info("Phase 4: %d Figma frame image(s) downloaded", len(figma_images))
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
                    "The following screenshots are the actual Figma design frames. "
                    "Analyse them carefully and match their visual style "
                    "(background color, text color, card style, nav style, typography) "
                    "in the HTML you generate."
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
    display = f"{summary}\n\n✅ Generated {file_count} file{'s' if file_count != 1 else ''}." if all_files else summary

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

"""Core Figma API and SharePoint MS Graph functions.

Called by:
  - routers/figma.py and routers/sharepoint.py  → serves the Custom GPT HTTP endpoints
  - services/anthropic_service.py               → used by the LLM prototype pipeline

All Figma/SharePoint calls go directly to the upstream APIs (no HTTP self-calls).
"""

import io
from typing import Optional
from urllib.parse import quote

import httpx
from config import settings

FIGMA_API_BASE = "https://api.figma.com/v1"
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.request_timeout_seconds, connect=30.0)


# ── Node / response trimming helpers ─────────────────────────────────────────


def _slim_figma_node(node: dict) -> dict:
    children = node.get("children")
    slim_children = []
    if isinstance(children, list):
        slim_children = [
            _slim_figma_node(child) for child in children if isinstance(child, dict)
        ]
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "type": node.get("type"),
        "layoutMode": node.get("layoutMode"),
        "itemSpacing": node.get("itemSpacing"),
        "paddingTop": node.get("paddingTop"),
        "paddingBottom": node.get("paddingBottom"),
        "paddingLeft": node.get("paddingLeft"),
        "paddingRight": node.get("paddingRight"),
        "children": slim_children,
    }


def _trim_figma_file_response(data: dict) -> dict:
    document = data.get("document")
    return {
        "name": data.get("name"),
        "lastModified": data.get("lastModified"),
        "version": data.get("version"),
        "document": _slim_figma_node(document) if isinstance(document, dict) else None,
    }


def _trim_figma_nodes_response(data: dict) -> dict:
    nodes = data.get("nodes")
    trimmed_nodes: dict = {}
    if isinstance(nodes, dict):
        for node_id, node_data in nodes.items():
            if isinstance(node_data, dict) and isinstance(node_data.get("document"), dict):
                trimmed_nodes[node_id] = _slim_figma_node(node_data["document"])
            else:
                trimmed_nodes[node_id] = node_data
    return {"name": data.get("name"), "nodes": trimmed_nodes}


def _trim_figma_images_response(data: dict) -> dict:
    trimmed = {"images": data.get("images", {})}
    if "err" in data:
        trimmed["err"] = data["err"]
    return trimmed


# ── Frame metadata helpers ────────────────────────────────────────────────────


def _parse_figma_ids(ids: Optional[str]) -> list[str]:
    if not ids:
        return []
    parsed: list[str] = []
    for raw_part in ids.split(","):
        part = raw_part.strip()
        if part:
            parsed.append(part.replace("-", ":"))
    return parsed


def _frame_metadata_from_node(node: dict, path: list[str], parent_id: Optional[str]) -> dict:
    bounds = node.get("absoluteBoundingBox") if isinstance(node, dict) else None
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "type": node.get("type"),
        "path": " / ".join(path),
        "parentId": parent_id,
        "childCount": (
            len(node.get("children", [])) if isinstance(node.get("children"), list) else 0
        ),
        "layoutMode": node.get("layoutMode"),
        "itemSpacing": node.get("itemSpacing"),
        "paddingTop": node.get("paddingTop"),
        "paddingBottom": node.get("paddingBottom"),
        "paddingLeft": node.get("paddingLeft"),
        "paddingRight": node.get("paddingRight"),
        "x": bounds.get("x") if isinstance(bounds, dict) else None,
        "y": bounds.get("y") if isinstance(bounds, dict) else None,
        "width": bounds.get("width") if isinstance(bounds, dict) else None,
        "height": bounds.get("height") if isinstance(bounds, dict) else None,
    }


def _collect_frame_metadata(
    node: dict,
    path: Optional[list[str]] = None,
    parent_id: Optional[str] = None,
) -> list[dict]:
    if not isinstance(node, dict):
        return []
    current_path = (path or []) + [node.get("name") or node.get("id") or "Unnamed"]
    frames: list[dict] = []
    if node.get("type") == "FRAME":
        frames.append(_frame_metadata_from_node(node, current_path, parent_id))
    children = node.get("children")
    if isinstance(children, list):
        node_id = node.get("id") if isinstance(node.get("id"), str) else parent_id
        for child in children:
            if isinstance(child, dict):
                frames.extend(_collect_frame_metadata(child, current_path, node_id))
    return frames


def _extract_frame_ids_from_frames(frames: list[dict]) -> list[str]:
    frame_ids: list[str] = []
    seen: set[str] = set()
    for frame in frames:
        frame_id = frame.get("id")
        if isinstance(frame_id, str) and frame_id and frame_id not in seen:
            seen.add(frame_id)
            frame_ids.append(frame_id)
    return frame_ids


def _chunk_list(values: list[str], chunk_size: int) -> list[list[str]]:
    if chunk_size < 1:
        chunk_size = 1
    return [values[i: i + chunk_size] for i in range(0, len(values), chunk_size)]


# ── Default resolution helpers ────────────────────────────────────────────────


def _resolve_file_key(file_key: Optional[str] = None) -> Optional[str]:
    """Return the first non-empty file key: query param → FIGMA_FILE_KEY_2 → FIGMA_FILE_KEY."""
    return (file_key or "").strip() or settings.figma_file_key_2 or settings.figma_file_key or None


def _resolve_node_ids(ids: Optional[str] = None) -> Optional[str]:
    """Return node IDs, normalising dash→colon format."""
    raw = (ids or "").strip() or settings.figma_node_ids
    if not raw:
        return None
    return ",".join(part.strip().replace("-", ":") for part in raw.split(",") if part.strip())


def _resolve_image_ids(ids: Optional[str] = None) -> Optional[str]:
    raw = (ids or "").strip() or settings.figma_image_ids
    if not raw:
        return None
    return ",".join(part.strip().replace("-", ":") for part in raw.split(",") if part.strip())


def _resolve_depth(depth: Optional[int] = None) -> int:
    return depth if depth is not None else settings.figma_depth


def _resolve_format(format_val: Optional[str] = None) -> str:
    fmt = (format_val or settings.figma_image_format or "png").strip()
    if fmt not in {"jpg", "png", "svg", "pdf"}:
        fmt = "png"
    return fmt


def _resolve_scale(scale_val: Optional[float] = None) -> float:
    return scale_val if scale_val is not None else settings.figma_image_scale


# ── Figma API HTTP helper ─────────────────────────────────────────────────────


async def _figma_get(path: str, params: Optional[dict] = None) -> dict:
    if not settings.figma_token:
        return {"error": "FIGMA_TOKEN is not configured"}
    headers = {"X-Figma-Token": settings.figma_token}
    if settings.debug:
        print("FIGMA GET:", path, params)
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.get(
            f"{FIGMA_API_BASE}{path}", headers=headers, params=params or {}
        )
        if response.status_code >= 400:
            detail = {"figma_status": response.status_code, "figma_body": response.text}
            if settings.debug:
                print("FIGMA ERROR:", detail)
            return detail
        return response.json()


# ── Public Figma functions ────────────────────────────────────────────────────


async def get_file(file_key: Optional[str] = None) -> dict:
    fk = _resolve_file_key(file_key)
    data = await _figma_get(f"/files/{fk}")
    return _trim_figma_file_response(data)


async def get_nodes(
    file_key: Optional[str] = None,
    ids: Optional[str] = None,
    depth: Optional[int] = None,
) -> dict:
    fk = _resolve_file_key(file_key)
    nids = _resolve_node_ids(ids)
    d = _resolve_depth(depth)
    data = await _figma_get(f"/files/{fk}/nodes", {"ids": nids, "depth": d})
    return _trim_figma_nodes_response(data)


async def get_frames(
    file_key: Optional[str] = None,
    ids: Optional[str] = None,
    depth: Optional[int] = None,
) -> dict:
    fk = _resolve_file_key(file_key)
    nids = _resolve_node_ids(ids)
    d = _resolve_depth(depth)
    data = await _figma_get(f"/files/{fk}/nodes", {"ids": nids, "depth": d})

    frames: list[dict] = []
    nodes = data.get("nodes")
    if isinstance(nodes, dict):
        for node_data in nodes.values():
            if isinstance(node_data, dict) and isinstance(node_data.get("document"), dict):
                frames.extend(_collect_frame_metadata(node_data["document"]))

    return {
        "name": data.get("name"),
        "requestedIds": nids,
        "depth": d,
        "totalFrames": len(frames),
        "frames": frames,
    }


async def get_file_images(
    file_key: Optional[str] = None,
    ids: Optional[str] = None,
    format: Optional[str] = None,
    scale: Optional[float] = None,
) -> dict:
    """Get images for specific node IDs. Used by /figma/file/images endpoint."""
    fk = _resolve_file_key(file_key)
    nids = _resolve_image_ids(ids)
    fmt = _resolve_format(format)
    scl = _resolve_scale(scale)
    data = await _figma_get(f"/images/{fk}", {"ids": nids, "format": fmt, "scale": scl})
    return _trim_figma_images_response(data)


async def export_images(
    file_key: Optional[str] = None,
    ids: Optional[str] = None,
    format: Optional[str] = None,
    scale: Optional[float] = None,
) -> dict:
    """Export rendered images for frame IDs.

    If ids is None/empty, discovers all frames in the file first.
    Used by both the LLM pipeline (with explicit ids) and the /figma/file/frames/images endpoint.
    """
    fk = _resolve_file_key(file_key)
    fmt = _resolve_format(format)
    scl = _resolve_scale(scale)
    frame_ids = _parse_figma_ids(ids) if ids else []

    if not frame_ids:
        # Discover all frames in the file
        file_data = await _figma_get(f"/files/{fk}")
        document = file_data.get("document")
        if isinstance(document, dict):
            frames = _collect_frame_metadata(document)
            frame_ids = _extract_frame_ids_from_frames(frames)

    if not frame_ids:
        return {"totalFrames": 0, "frames": [], "images": {}}

    batch_size = settings.figma_image_batch_size
    batches = _chunk_list(frame_ids, batch_size)

    merged_images: dict[str, str] = {}
    merged_errors: list[str] = []
    for batch in batches:
        images_data = await _figma_get(
            f"/images/{fk}",
            {"ids": ",".join(batch), "format": fmt, "scale": scl},
        )
        trimmed = _trim_figma_images_response(images_data)
        batch_images = trimmed.get("images", {})
        if isinstance(batch_images, dict):
            merged_images.update(batch_images)
        if trimmed.get("err"):
            merged_errors.append(str(trimmed["err"]))

    result: dict = {
        "totalFrames": len(frame_ids),
        "frameIds": frame_ids,
        "images": merged_images,
        "batchSize": batch_size,
        "batchCount": len(batches),
    }
    if merged_errors:
        result["err"] = " | ".join(merged_errors)
    return result


# ── SharePoint / MS Graph functions ──────────────────────────────────────────


async def _graph_access_token(client: httpx.AsyncClient) -> str:
    token_url = (
        f"https://login.microsoftonline.com/{settings.ms_tenant_id}/oauth2/v2.0/token"
    )
    payload = {
        "client_id": settings.ms_client_id,
        "client_secret": settings.ms_client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    response = await client.post(token_url, data=payload)
    if response.status_code >= 400:
        raise Exception(
            f"Graph auth failed: {response.status_code} {response.text[:200]}"
        )
    token = response.json().get("access_token")
    if not token:
        raise Exception("No Graph access token returned")
    return token


async def _graph_download_binary(
    client: httpx.AsyncClient, token: str, path: str
) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get(
        f"{GRAPH_API_BASE}{path}", headers=headers, follow_redirects=True
    )
    if response.status_code >= 400:
        raise Exception(
            f"Graph download failed: {response.status_code} {response.text[:200]}"
        )
    return response.content


def _normalize_cell(value: object) -> str:
    return "" if value is None else str(value)


def _values_to_rows(
    values: list[list[object]],
) -> tuple[list[str], list[dict[str, str]]]:
    if not values:
        return [], []
    raw_headers = values[0]
    headers: list[str] = []
    header_counts: dict[str, int] = {}
    for index, raw_header in enumerate(raw_headers):
        header = _normalize_cell(raw_header).strip() or f"column_{index + 1}"
        if header in header_counts:
            header_counts[header] += 1
            header = f"{header}_{header_counts[header]}"
        else:
            header_counts[header] = 1
        headers.append(header)
    rows: list[dict[str, str]] = []
    for raw_row in values[1:]:
        row_values = [_normalize_cell(item) for item in raw_row]
        if not any(cell.strip() for cell in row_values):
            continue
        row: dict[str, str] = {}
        for idx, header in enumerate(headers):
            row[header] = row_values[idx] if idx < len(row_values) else ""
        rows.append(row)
    return headers, rows


async def _fetch_sharepoint_sheet(file_path_raw: str) -> dict:
    from openpyxl import load_workbook

    path = quote(file_path_raw.lstrip("/"), safe="/")
    file_path = f"/drives/{settings.sp_drive_id}/root:/{path}:/content"

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        token = await _graph_access_token(client)
        file_bytes = await _graph_download_binary(client, token, file_path)

    workbook = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    if settings.sp_worksheet_name:
        if settings.sp_worksheet_name not in workbook.sheetnames:
            raise Exception(f"Worksheet '{settings.sp_worksheet_name}' not found")
        worksheet = workbook[settings.sp_worksheet_name]
    else:
        worksheet = workbook.active

    values = list(worksheet.values)
    if not values:
        raise Exception("Spreadsheet contains no data")

    headers, rows = _values_to_rows(values)
    return {
        "file_name": file_path_raw.split("/")[-1],
        "file_web_url": None,
        "worksheet": worksheet.title,
        "headers": headers,
        "total_rows": len(rows),
        "rows": rows,
    }


async def get_architecture_plan() -> dict:
    """Fetch the DCI architecture plan from SharePoint (example file)."""
    return await _fetch_sharepoint_sheet(settings.sp_file_path_example)


async def get_architecture_template() -> dict:
    """Fetch the architecture plan template from SharePoint."""
    return await _fetch_sharepoint_sheet(settings.sp_file_path)

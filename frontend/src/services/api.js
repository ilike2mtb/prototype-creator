const BASE = import.meta.env.VITE_API_URL || "";

export async function sendMessage({ messages, framework, outputType, mode, drupalVersion, figmaParams }) {
  const res = await fetch(`${BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messages,
      framework,
      output_type:    outputType,
      mode,
      drupal_version: drupalVersion ?? "",
      figma_params:   figmaParams  ?? {},
    }),
  });
  if (!res.ok) {
    let detail = `Server error (${res.status})`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

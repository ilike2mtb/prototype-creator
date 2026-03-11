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
  if (!res.ok) throw new Error(`API error ${res.status}`);
  return res.json();
}

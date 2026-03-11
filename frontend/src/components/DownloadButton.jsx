import { useState } from "react";
import { downloadZip } from "../utils/zip";

export default function DownloadButton({ artifacts }) {
  const [busy, setBusy] = useState(false);
  const ready = !!artifacts;

  async function handle() {
    if (!ready || busy) return;
    setBusy(true);
    try {
      await downloadZip(artifacts);
    } catch (e) {
      console.error("Download failed:", e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <button onClick={handle} disabled={!ready || busy} style={{
      padding: "6px 14px", borderRadius: 6, border: "none", fontSize: 12, fontWeight: 600, cursor: ready && !busy ? "pointer" : "default",
      background: ready && !busy ? "linear-gradient(135deg,#6366f1,#8b5cf6)" : "#2d3748",
      color: ready && !busy ? "#fff" : "#4a5568", opacity: ready ? 1 : 0.5
    }}>
      {busy ? "Generating…" : "⬇ Download Zip"}
    </button>
  );
}

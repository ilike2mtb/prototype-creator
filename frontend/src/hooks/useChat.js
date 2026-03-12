import { useState } from "react";
import { sendMessage } from "../services/api";

export function useChat({ framework, outputType, mode, drupalVersion, figmaParams }) {
  const [messages,  setMessages]  = useState([]);
  const [artifacts, setArtifacts] = useState(null);
  const [loading,   setLoading]   = useState(false);

  async function send(text) {
    const userMsg = { role: "user", content: text };
    const next    = [...messages, userMsg];
    setMessages(next);
    setLoading(true);
    try {
      const { message, artifacts: a } = await sendMessage({
        messages: next, framework, outputType, mode, drupalVersion, figmaParams,
      });
      setMessages(prev => [...prev, { role: "assistant", content: message }]);
      if (a?.length) setArtifacts(a[0]);
    } catch (e) {
      const msg = e.message === "Failed to fetch"
        ? "Could not reach the server. The backend may be waking up on Render's free tier — please wait 30 seconds and try again."
        : `Error: ${e.message}`;
      setMessages(prev => [...prev, { role: "assistant", content: msg }]);
    }
    setLoading(false);
  }

  function reset() { setMessages([]); setArtifacts(null); }

  return { messages, artifacts, loading, send, reset };
}

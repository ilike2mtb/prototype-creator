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
      if (a) setArtifacts(a);
    } catch (e) {
      setMessages(prev => [...prev, { role: "assistant", content: `Error: ${e.message}` }]);
    }
    setLoading(false);
  }

  function reset() { setMessages([]); setArtifacts(null); }

  return { messages, artifacts, loading, send, reset };
}

import { useRef, useEffect, useState } from "react";
import Message from "./Message";
import DownloadButton from "./DownloadButton";
import { useChat } from "../hooks/useChat";

// Human-readable labels for header badges
const FW_LABEL = {
  drupal10: "Drupal 10",
  drupal11: "Drupal 11",
  laravel:  "Laravel",
  claude:   "Claude Chooses",
};
const OUT_LABEL = {
  framework: "Framework",
  html:      "HTML",
  both:      "Both",
};

// Phase descriptions shown while loading (advances every ~12 s)
const PHASES = [
  "Analysing requirements…",
  "Planning project structure…",
  "Generating backend files…",
  "Building theme & frontend…",
  "Finalising prototype…",
];

export default function Chat({ framework, outputType, mode, drupalVersion, figmaParams, onReset }) {
  const { messages, artifacts, loading, send } = useChat({
    framework, outputType, mode, drupalVersion, figmaParams,
  });
  const [input,    setInput]    = useState("");
  const [phaseIdx, setPhaseIdx] = useState(0);
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // Cycle through phase descriptions while loading
  useEffect(() => {
    if (!loading) { setPhaseIdx(0); return; }
    const timer = setInterval(
      () => setPhaseIdx(i => Math.min(i + 1, PHASES.length - 1)),
      12000
    );
    return () => clearInterval(timer);
  }, [loading]);

  function handleKey(e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }
  function handleSend() { if (!input.trim() || loading) return; send(input.trim()); setInput(""); }

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100vh", background:"#0f1117", color:"#e2e8f0", fontFamily:"system-ui,sans-serif" }}>

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div style={{ padding:"14px 20px", borderBottom:"1px solid #2d3748", background:"#1a1f2e", display:"flex", alignItems:"center", gap:12 }}>
        <div style={{ width:32,height:32,borderRadius:8,background:"linear-gradient(135deg,#6366f1,#8b5cf6)",display:"flex",alignItems:"center",justifyContent:"center",fontSize:16 }}>✦</div>
        <div>
          <div style={{ fontWeight:700,fontSize:15 }}>Prototype Creator</div>
          <div style={{ fontSize:11,color:"#718096" }}>prototype-creator.onrender.com</div>
        </div>
        <div style={{ marginLeft:"auto",display:"flex",gap:8,alignItems:"center" }}>
          <span style={{ fontSize:11,color:"#68d391" }}>● Connected</span>
          {/* Framework badge */}
          <span style={{ fontSize:11,color:"#a5b4fc",background:"#2d3748",padding:"3px 8px",borderRadius:4 }}>
            {FW_LABEL[framework] ?? framework}
          </span>
          {/* Output-type badge */}
          <span style={{ fontSize:11,color:"#68d391",background:"#1a2e22",padding:"3px 8px",borderRadius:4 }}>
            {OUT_LABEL[outputType] ?? outputType}
          </span>
          <DownloadButton artifacts={artifacts} />
          <button onClick={onReset} style={{ padding:"4px 10px",borderRadius:6,background:"#2d3748",color:"#a0aec0",border:"1px solid #4a5568",cursor:"pointer",fontSize:11 }}>Reset</button>
        </div>
      </div>

      {/* ── Messages ───────────────────────────────────────────────────────── */}
      <div style={{ flex:1,overflowY:"auto",padding:20 }}>
        {messages.length === 0 && (
          <div style={{ textAlign:"center",color:"#4a5568",marginTop:60 }}>
            <div style={{ fontSize:36,marginBottom:8 }}>✦</div>
            <div style={{ fontSize:13,color:"#718096" }}>Describe what you'd like to build</div>
          </div>
        )}
        {messages.map((m,i) => <Message key={i} msg={m} />)}

        {/* Loading: dots + cycling phase label */}
        {loading && (
          <div style={{ display:"flex",justifyContent:"flex-start" }}>
            <div style={{ background:"#1e2433",border:"1px solid #2d3748",borderRadius:"16px 16px 16px 4px",padding:"12px 16px",display:"flex",alignItems:"center",gap:10 }}>
              <div style={{ display:"flex",gap:4 }}>
                {[0,1,2].map(i=>(
                  <div key={i} style={{ width:6,height:6,borderRadius:"50%",background:"#6366f1",animation:"pulse 1.2s ease-in-out infinite",animationDelay:`${i*0.2}s` }}/>
                ))}
              </div>
              <span style={{ fontSize:12,color:"#718096" }}>{PHASES[phaseIdx]}</span>
            </div>
          </div>
        )}
        <div ref={bottomRef}/>
      </div>

      {/* ── Input ──────────────────────────────────────────────────────────── */}
      <div style={{ padding:"14px 20px",borderTop:"1px solid #2d3748",background:"#1a1f2e" }}>
        <div style={{ display:"flex",gap:10,alignItems:"flex-end" }}>
          <textarea value={input} onChange={e=>setInput(e.target.value)} onKeyDown={handleKey}
            placeholder="Describe what you'd like to build…" rows={12}
            style={{ flex:1,padding:"10px 14px",borderRadius:10,border:"1px solid #2d3748",background:"#2d3748",color:"#e2e8f0",fontSize:14,resize:"vertical",outline:"none",lineHeight:1.5 }}/>
          <button onClick={handleSend} disabled={loading||!input.trim()} style={{
            padding:"10px 18px",borderRadius:10,border:"none",fontSize:14,fontWeight:600,
            background:loading||!input.trim()?"#2d3748":"linear-gradient(135deg,#6366f1,#8b5cf6)",
            color:loading||!input.trim()?"#4a5568":"#fff",cursor:loading||!input.trim()?"default":"pointer"
          }}>Send</button>
        </div>
        <div style={{ fontSize:11,color:"#4a5568",marginTop:6 }}>
          Enter to send · Shift+Enter for new line
          {loading && <span style={{ marginLeft:12,color:"#6366f1" }}>⏳ Multi-phase generation — may take 60–120 s</span>}
        </div>
      </div>

      <style>{`@keyframes pulse{0%,100%{opacity:.3}50%{opacity:1}}`}</style>
    </div>
  );
}

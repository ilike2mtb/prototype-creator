import { useState } from "react";

// ── Shared button style helper ─────────────────────────────────────────────
const pill = (label, onClick) => (
  <button
    key={label}
    onClick={() => onClick(label)}
    style={{
      padding: "8px 18px", borderRadius: 20, background: "transparent",
      border: "1px solid #6366f1", color: "#a5b4fc",
      cursor: "pointer", fontSize: 13, margin: 4,
    }}
    onMouseEnter={e => { e.target.style.background = "#6366f1"; e.target.style.color = "#fff"; }}
    onMouseLeave={e => { e.target.style.background = "transparent"; e.target.style.color = "#a5b4fc"; }}
  >
    {label}
  </button>
);

const FW_LABEL = { drupal10:"Drupal 10", drupal11:"Drupal 11", laravel:"Laravel", claude:"Claude Chooses" };
const FW_KEY   = { "Drupal 10":"drupal10", "Drupal 11":"drupal11", "Laravel":"laravel", "Claude Chooses":"claude" };
const FW_DRUPAL_VER = { drupal10:"10", drupal11:"11", laravel:"", claude:"" };

// ── Component ─────────────────────────────────────────────────────────────────
export default function Onboarding({ onComplete }) {
  const [step,         setStep]    = useState("framework");
  const [framework,    setFW]      = useState(null);
  const [outputType,   setOutput]  = useState(null);
  const [mode,         setMode]    = useState(null);
  const [figmaParams,  setFigma]   = useState({});
  const [inputVal,     setInput]   = useState("");
  const [awaitingField,setAwait]   = useState(null);
  const [history,      setHistory] = useState([]);  // stack of state snapshots

  // ── History helpers ───────────────────────────────────────────────────────
  function pushHistory() {
    setHistory(h => [...h, { step, framework, outputType, mode, figmaParams, awaitingField }]);
  }

  function goBack() {
    if (history.length === 0) return;
    const prev = history[history.length - 1];
    setHistory(h => h.slice(0, -1));
    setStep(prev.step);
    setFW(prev.framework);
    setOutput(prev.outputType);
    setMode(prev.mode);
    setFigma(prev.figmaParams);
    setAwait(prev.awaitingField);
    setInput("");
  }

  // ── Step 1: Framework ────────────────────────────────────────────────────
  function pickFramework(label) {
    pushHistory();
    setFW(FW_KEY[label]);
    setStep("outputType");
  }

  // ── Step 2: Output type ──────────────────────────────────────────────────
  function pickOutputType(label) {
    pushHistory();
    setOutput(label === "HTML Prototype" ? "html" : label === "Both" ? "both" : "framework");
    setStep("dataSource");
  }

  function frameworkOutputLabel() {
    if (framework === "drupal10") return "Drupal 10 Prototype";
    if (framework === "drupal11") return "Drupal 11 Prototype";
    if (framework === "laravel")  return "Laravel Prototype";
    return "Framework Prototype";
  }

  function showArchOption() {
    return ["drupal10", "drupal11", "claude"].includes(framework) && outputType !== "html";
  }

  // ── Step 3: Data source ──────────────────────────────────────────────────
  function pickDataSource(label) {
    const m = label === "Architecture Plan" ? "arch"
            : label === "Figma" || label === "Yes, use Figma" ? "figma"
            : label === "Both"  ? "both"
            : "none";

    if (m === "figma" || m === "both") {
      pushHistory();
      setMode(m);
      setStep("figmaDefaults");
    } else {
      onComplete({ framework, outputType, mode: m, drupalVersion: FW_DRUPAL_VER[framework], figmaParams: {} });
    }
  }

  // ── Step 4: Figma defaults ───────────────────────────────────────────────
  function pickFigmaDefaults(label) {
    if (label === "Yes, use defaults") {
      onComplete({ framework, outputType, mode, drupalVersion: FW_DRUPAL_VER[framework], figmaParams: {} });
    } else {
      pushHistory();
      setStep("figmaConfig");
      setAwait("file_key");
    }
  }

  // ── Step 5: Figma config ─────────────────────────────────────────────────
  const fieldLabels = { file_key: "Figma File Key", ids: "Node ID(s) — comma-separated", depth: "Depth (1–10)" };

  function submitField() {
    const val  = inputVal.trim();
    const skip = val.toLowerCase() === "skip";

    if (awaitingField === "file_key") {
      pushHistory();
      if (!skip) setFigma(p => ({ ...p, file_key: val }));
      setAwait("ids");
      setInput("");
    } else if (awaitingField === "ids") {
      pushHistory();
      if (!skip) setFigma(p => ({ ...p, ids: val }));
      setAwait("depth");
      setInput("");
    } else if (awaitingField === "depth") {
      const finalParams = (!skip && !isNaN(parseInt(val)))
        ? { ...figmaParams, depth: parseInt(val) }
        : figmaParams;
      onComplete({ framework, outputType, mode, drupalVersion: FW_DRUPAL_VER[framework], figmaParams: finalParams });
    }
  }

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center",
      justifyContent: "center", flex: 1, gap: 20, color: "#e2e8f0", padding: 24,
    }}>

      <div style={{ fontSize: 36 }}>✦</div>

      {/* Back button — inline, just above the star's gap */}
      {history.length > 0 && (
        <button
          onClick={goBack}
          style={{
            marginTop: -8,
            background: "none", border: "1px solid #4a5568", color: "#a0aec0",
            cursor: "pointer", fontSize: 13, display: "flex", alignItems: "center", gap: 6,
            padding: "5px 12px", borderRadius: 20,
          }}
          onMouseEnter={e => { e.currentTarget.style.borderColor = "#6366f1"; e.currentTarget.style.color = "#a5b4fc"; }}
          onMouseLeave={e => { e.currentTarget.style.borderColor = "#4a5568"; e.currentTarget.style.color = "#a0aec0"; }}
        >
          ← Back
        </button>
      )}

      {/* Step 1 — Framework */}
      {step === "framework" && (
        <>
          <p style={{ margin: 0, fontSize: 15, color: "#a0aec0" }}>Which framework?</p>
          <div>
            {["Drupal 10", "Drupal 11", "Laravel", "Claude Chooses"].map(l => pill(l, pickFramework))}
          </div>
        </>
      )}

      {/* Step 2 — Output type */}
      {step === "outputType" && (
        <>
          <p style={{ margin: 0, fontSize: 15, color: "#a0aec0" }}>What would you like to generate?</p>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "#4a5568" }}>
            Framework: <span style={{ color: "#a5b4fc" }}>{FW_LABEL[framework]}</span>
          </p>
          {framework === "claude" && (
            <p style={{ margin: "2px 0 0", fontSize: 12, color: "#68d391", maxWidth: 360, textAlign: "center" }}>
              Claude will pick the best framework based on your use case.
            </p>
          )}
          <div>
            {[frameworkOutputLabel(), "HTML Prototype", "Both"].map(l => pill(l, pickOutputType))}
          </div>
        </>
      )}

      {/* Step 3 — Data source */}
      {step === "dataSource" && (
        <>
          <p style={{ margin: 0, fontSize: 15, color: "#a0aec0" }}>
            {showArchOption() ? "What data source would you like to use?" : "Would you like to use Figma designs?"}
          </p>
          <div>
            {(showArchOption()
              ? ["Architecture Plan", "Figma", "Both", "Skip"]
              : ["Yes, use Figma", "Skip"]
            ).map(l => pill(l, pickDataSource))}
          </div>
        </>
      )}

      {/* Step 4 — Figma defaults */}
      {step === "figmaDefaults" && (
        <>
          <p style={{ margin: 0, fontSize: 15, color: "#a0aec0" }}>Use default Figma parameters?</p>
          <div>
            {["Yes, use defaults", "No, I'll configure them"].map(l => pill(l, pickFigmaDefaults))}
          </div>
        </>
      )}

      {/* Step 5 — Figma config */}
      {step === "figmaConfig" && awaitingField && (
        <>
          <p style={{ margin: 0, fontSize: 15, color: "#a0aec0" }}>
            {fieldLabels[awaitingField]}{" "}
            <span style={{ color: "#4a5568", fontSize: 12 }}>(or type <code>skip</code>)</span>
          </p>
          <input
            value={inputVal}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && submitField()}
            autoFocus
            style={{
              padding: "10px 14px", borderRadius: 8, border: "1px solid #4a5568",
              background: "#2d3748", color: "#e2e8f0", fontSize: 14, width: 300, outline: "none",
            }}
          />
          <button
            onClick={submitField}
            style={{
              padding: "8px 20px", borderRadius: 8,
              background: "linear-gradient(135deg,#6366f1,#8b5cf6)",
              color: "#fff", border: "none", cursor: "pointer", fontSize: 14,
            }}
          >
            Next
          </button>
        </>
      )}
    </div>
  );
}

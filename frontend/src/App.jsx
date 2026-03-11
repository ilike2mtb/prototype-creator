import { useState } from "react";
import Onboarding from "./components/Onboarding";
import Chat from "./components/Chat";

export default function App() {
  const [config, setConfig] = useState(null);
  return config
    ? <Chat {...config} onReset={() => setConfig(null)} />
    : <div style={{ display:"flex",height:"100vh",background:"#0f1117",fontFamily:"system-ui,sans-serif" }}>
        <Onboarding onComplete={setConfig} />
      </div>;
}

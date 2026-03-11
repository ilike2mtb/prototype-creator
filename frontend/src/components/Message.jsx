export default function Message({ msg }) {
  const isUser = msg.role === "user";
  return (
    <div style={{ display:"flex", justifyContent: isUser ? "flex-end" : "flex-start", marginBottom: 12 }}>
      <div style={{
        maxWidth: "75%", padding: "10px 14px", fontSize: 14, lineHeight: 1.6,
        whiteSpace: "pre-wrap", wordBreak: "break-word", borderRadius: isUser ? "16px 16px 4px 16px" : "16px 16px 16px 4px",
        background: isUser ? "linear-gradient(135deg,#6366f1,#8b5cf6)" : "#1e2433",
        border: isUser ? "none" : "1px solid #2d3748", color: "#e2e8f0"
      }}>
        {msg.content}
      </div>
    </div>
  );
}

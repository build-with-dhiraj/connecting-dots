import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Connecting Dots",
  description:
    "Personal second-brain that ingests saves from WhatsApp, YouTube, Instagram, and LinkedIn, then resurfaces them in context.",
};

const pageStyle: React.CSSProperties = {
  margin: "0 auto",
  maxWidth: 640,
  padding: "64px 24px",
  fontFamily:
    "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  color: "#111",
  lineHeight: 1.6,
  background: "#fff",
};

const linkStyle: React.CSSProperties = {
  color: "#0366d6",
  textDecoration: "none",
};

export default function Page() {
  return (
    <main style={pageStyle}>
      <h1 style={{ fontSize: 32, marginBottom: 16 }}>Connecting Dots</h1>
      <p style={{ fontSize: 16, marginBottom: 32 }}>
        Personal second-brain that ingests saves from WhatsApp, YouTube,
        Instagram, and LinkedIn, then resurfaces them in context.
      </p>

      <nav
        aria-label="Site links"
        style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 48 }}
      >
        <a href="/privacy" style={linkStyle}>
          Privacy Policy
        </a>
        <a href="/terms" style={linkStyle}>
          Terms of Service
        </a>
        <a
          href="https://github.com/build-with-dhiraj/connecting-dots"
          style={linkStyle}
          rel="noopener noreferrer"
        >
          GitHub
        </a>
      </nav>

      <footer style={{ fontSize: 14, color: "#666", borderTop: "1px solid #eee", paddingTop: 16 }}>
        Made by Dhiraj Singh Pawar
      </footer>
    </main>
  );
}

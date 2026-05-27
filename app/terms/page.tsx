import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Terms of Service — Connecting Dots",
  description:
    "Terms of service for Connecting Dots, a personal single-user application.",
};

const pageStyle: React.CSSProperties = {
  margin: "0 auto",
  maxWidth: 720,
  padding: "64px 24px",
  fontFamily:
    "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  color: "#111",
  lineHeight: 1.65,
  background: "#fff",
};

const linkStyle: React.CSSProperties = {
  color: "#0366d6",
  textDecoration: "none",
};

export default function TermsPage() {
  return (
    <main style={pageStyle}>
      <p style={{ marginBottom: 24 }}>
        <a href="/" style={linkStyle}>
          &larr; Home
        </a>
      </p>

      <h1 style={{ fontSize: 32, marginBottom: 8 }}>Terms of Service</h1>
      <p style={{ color: "#666", marginBottom: 32 }}>Last updated: 2026-05-28</p>

      <p>
        This is a personal, single-user application. There is no service offered
        to third parties.
      </p>
      <p>
        By using this application, you (the sole user) accept full responsibility
        for the data ingested and stored.
      </p>
      <p>No warranty of any kind, express or implied, is provided.</p>
      <p>
        Contact:{" "}
        <a href="mailto:dhirajpawar4444@gmail.com" style={linkStyle}>
          dhirajpawar4444@gmail.com
        </a>
        .
      </p>
    </main>
  );
}

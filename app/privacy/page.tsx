import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Privacy Policy — Connecting Dots",
  description:
    "Privacy policy for Connecting Dots, a personal second-brain application used solely by Dhiraj Singh Pawar.",
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

const h2Style: React.CSSProperties = {
  fontSize: 20,
  marginTop: 32,
  marginBottom: 8,
};

const linkStyle: React.CSSProperties = {
  color: "#0366d6",
  textDecoration: "none",
};

export default function PrivacyPage() {
  return (
    <main style={pageStyle}>
      <p style={{ marginBottom: 24 }}>
        <a href="/" style={linkStyle}>
          &larr; Home
        </a>
      </p>

      <h1 style={{ fontSize: 32, marginBottom: 8 }}>Privacy Policy</h1>
      <p style={{ color: "#666", marginBottom: 32 }}>Last updated: 2026-05-28</p>

      <h2 style={h2Style}>Who this app is for</h2>
      <p>
        Connecting Dots is a personal application used solely by Dhiraj Singh
        Pawar (<a href="mailto:dhirajpawar4444@gmail.com" style={linkStyle}>dhirajpawar4444@gmail.com</a>).
        It is not a public service. No accounts are offered, and there are no other
        users.
      </p>

      <h2 style={h2Style}>What data is collected</h2>
      <p>The application processes only data the sole user explicitly sends to it:</p>
      <ul>
        <li>
          WhatsApp messages the user sends to their own WhatsApp Business number
          (text content, URLs, and references to media attachments).
        </li>
        <li>
          Gmail messages the user forwards to a specific label in their own Gmail
          account.
        </li>
        <li>
          LinkedIn data-export ZIP archives the user uploads from their own LinkedIn
          account.
        </li>
        <li>
          Open Graph (OG) metadata fetched from URLs the user explicitly shares
          (page titles, descriptions, and thumbnail image references).
        </li>
      </ul>

      <h2 style={h2Style}>Where the data is stored</h2>
      <ul>
        <li>
          An Obsidian vault on the user&apos;s personal laptop. This is the primary,
          permanent store.
        </li>
        <li>
          A LanceDB embeddings file located next to the Obsidian vault on the same
          laptop.
        </li>
        <li>
          A transient Upstash Redis stream entry that exists only for the seconds
          between Meta&apos;s webhook delivery and the user&apos;s local Python
          consumer pulling the message. The stream is auto-trimmed within seconds
          of consumption.
        </li>
        <li>
          Vercel function logs, retained according to Vercel&apos;s standard log
          retention policy.
        </li>
      </ul>

      <h2 style={h2Style}>What data is NOT collected</h2>
      <ul>
        <li>No third-party analytics or telemetry.</li>
        <li>No advertising identifiers.</li>
        <li>No location data.</li>
        <li>No contact list access.</li>
        <li>No broader phone history or device data.</li>
      </ul>

      <h2 style={h2Style}>Sharing</h2>
      <p>
        None. No data is shared, sold, or transmitted to any third party. All
        ingested content remains within the user&apos;s own infrastructure.
      </p>

      <h2 style={h2Style}>Retention</h2>
      <ul>
        <li>
          The Obsidian vault is permanent until the user explicitly deletes its
          contents.
        </li>
        <li>
          The Upstash Redis stream is transient: each entry is consumed and cleared
          within seconds.
        </li>
        <li>
          Vercel function logs follow Vercel&apos;s default retention policy.
        </li>
      </ul>

      <h2 style={h2Style}>Third parties involved</h2>
      <p>The following third-party services are part of the data path:</p>
      <ul>
        <li>Meta (WhatsApp Cloud API) — delivers WhatsApp messages via webhook.</li>
        <li>Upstash (Redis) — transient relay between the webhook and the local consumer.</li>
        <li>Vercel — hosts the webhook function.</li>
        <li>Google (Gmail IMAP) — source of forwarded email messages.</li>
        <li>LinkedIn — source of user-initiated data export downloads.</li>
        <li>
          Any URL the user explicitly shares — fetched once for Open Graph metadata.
        </li>
      </ul>

      <h2 style={h2Style}>Contact</h2>
      <p>
        Questions about this policy can be sent to{" "}
        <a href="mailto:dhirajpawar4444@gmail.com" style={linkStyle}>
          dhirajpawar4444@gmail.com
        </a>
        .
      </p>
    </main>
  );
}

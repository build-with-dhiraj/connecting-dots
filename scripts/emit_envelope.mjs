// Emit a sample InboundEnvelope as JSON on stdout, conforming to the TS type
// generated from schemas/inbound_envelope.schema.json. Used by `make test-bridge`
// to verify the schema round-trips between TS and Python.
//
// Note: this is a .mjs script run via Node — no TS compilation required.
// It hand-builds the envelope shape and emits JSON. The TS type is checked
// at build time via `tsc --noEmit` (next build) for the call sites in lib/.

const envelope = {
  message_id: "wamid.TEST_BRIDGE_ROUNDTRIP_001",
  message_type: "url",
  url: "https://example.com/article",
  source: "whatsapp",
  captured_at: new Date("2026-05-27T12:00:00Z").toISOString(),
  raw_payload: {
    from: "15551234567",
    id: "wamid.TEST_BRIDGE_ROUNDTRIP_001",
    timestamp: "1716811200",
    type: "text",
    text: { body: "Check this out https://example.com/article" },
  },
};

process.stdout.write(JSON.stringify(envelope));

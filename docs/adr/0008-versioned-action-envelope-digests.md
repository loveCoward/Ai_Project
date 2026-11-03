# Versioned canonical action-envelope digests

Status: accepted design direction; the overall technical design remains in draft.

An approved action's fixed parameter digest is computed from a versioned canonical action envelope containing the MCP tool and target identity, operation, exact business parameters, and the parameter contract version. Technical call identity, retry timing, tracing metadata, and credentials are excluded. The canonical representation makes retries produce the same digest while any field that changes the business effect produces a new digest and therefore requires a new approval action identity. The canonicalization and digest algorithms, compatibility rules, and target response contract remain to be selected and tested; this ADR does not describe an implemented protocol.

# Background analysis uses an attempt-bound current authorization scope

Status: accepted design direction; the overall technical design remains in draft.

Background analysis outlives the browser request that starts it, so it must not depend on retaining that browser's JWT. Each Proposal Analysis Attempt is bound to its authenticated initiator, task, input revision, and launch-time allowed scope; each later restricted operation also checks current database authorization. The effective scope is their intersection: later grants do not expand an existing attempt, revocation stops further restricted work, and an invalidated attempt retains only source-restricted audit results without writing to the current proposal candidate. Interaction, approval, or recovery requires fresh authentication, and work after invalidation uses a new attempt.

This chooses attempt-bound execution over binding the run to the browser token's lifetime, which would interrupt legitimate work when the token expires. It also avoids treating the initiator's old permissions as a durable grant, which could allow a run to continue after access is revoked. The consequences are per-operation authorization checks, provenance-aware trace access, and explicit handling of in-flight calls and checkpoint termination.

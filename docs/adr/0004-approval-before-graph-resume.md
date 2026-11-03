# Persist business approval before resuming the graph

Status: accepted design direction; the overall technical design remains in draft.

A reviewer decision is authoritative only after the business service checks current identity, source access, self-approval, the fixed submission revision and exact action parameters, then commits the approval and audit record in a short transaction. If a paused graph must continue, the same business transaction records a resume intent; only after commit may the graph receive `Command(resume=...)`. A resumed interrupt node can restart, so it reads and verifies the existing decision instead of creating another approval or external effect. If the checkpoint lags, recovery reconciles against the persisted decision and snapshot without treating a replay as another authorization.

The alternative of resuming first and recording approval inside the graph leaves a harder ambiguity after node replay or business-write failure. This direction makes the business decision authoritative while leaving the resume trigger, current-authentication checks after restart, and idempotent reconciliation to detailed design; it is not an implemented approval or publication protocol.

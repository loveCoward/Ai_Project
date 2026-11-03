# Fence stale resume claimants at checkpoint writes

Status: accepted design direction; the overall technical design remains in draft.

Each re-claim of a resume intent receives a higher fencing generation. Graph checkpoint progress and the related receipt must conditionally persist that generation at the write boundary, so a paused or expired claimant cannot write after a newer claimant has taken ownership. A generation read before the call is insufficient because the old process can pause after the read. If the selected checkpoint storage cannot reject an older generation during persistence, the recovery remains pending verification rather than claiming that fencing is effective. The exact saver extension, transaction boundary, and business-record reconciliation remain to be designed; this ADR does not describe an implemented protocol.

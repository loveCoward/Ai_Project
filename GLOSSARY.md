# Enterprise AI Presales

This glossary defines the business language for the enterprise AI presales workspace. It keeps customer opportunities, proposal work, evidence, review decisions, and durable business facts distinct.

## Business Objects

**Opportunity**:
A potential customer engagement that contains the customer background, business context, and relevant sales history.
_Avoid_: Lead, account, deal

**Presales Task**:
A bounded piece of proposal work performed for an opportunity, from requirement intake through review and approval.
_Avoid_: Chat session, run, job

**Proposal Analysis Attempt**:
One execution of the analysis work for a presales task, with its own outcome and retained history. Repeating the analysis creates a new attempt, not a new opportunity or a replacement of the previous attempt.
_Avoid_: Presales task, proposal version

**Proposal Package**:
The complete reviewable presales deliverable for a task, including requirement coverage, evidence, solution content, risks, clarification questions, and review history.
_Avoid_: Report, document

**Proposal Draft**:
A mutable working version of a proposal package that has not yet become an approved formal version.
_Avoid_: Final proposal, official version

**Proposal Version**:
A distinct edition of a proposal package with its own review and approval history. An approved edition is immutable, whereas an unapproved edition may still be a draft or require changes.
_Avoid_: File copy, analysis attempt

**Formal Proposal Version**:
An approved, immutable edition of a complete proposal package. Material changes are represented by a new version requiring its own review and approval.
_Avoid_: Draft, generated report

**Requirement**:
A customer-stated need, constraint, expectation, or acceptance condition that the proposal must address.
_Avoid_: Prompt, feature request

**Mandatory Requirement**:
A customer requirement explicitly designated as necessary for an acceptable proposal, rather than an ordinary preference. Its importance is distinct from how well the current solution covers it.
_Avoid_: High-confidence requirement, critical risk

**Coverage Status**:
The business assessment of one requirement: covered, partially covered, uncovered, clarification needed, or explicitly out of product scope.
_Avoid_: Confidence score, completion percentage

**Product-fit Coverage**:
The proportion of requirements in the currently confirmed customer scope that are fully supported by the proposed product solution. Partial, uncovered, unclear, and out-of-product-scope requirements do not count as fully covered; this is distinct from how accurately an Agent analyzes them.
_Avoid_: Agent quality score, extraction recall

**Evidence**:
A traceable piece of customer, product, technical, deployment, security, delivery, or case-study information used to support a proposal conclusion.
_Avoid_: Context, citation text

**Clarification Question**:
An unresolved question whose answer is needed to confirm a requirement, assess a risk, or make a reliable proposal commitment.
_Avoid_: Follow-up, missing data

**Risk**:
A condition that may prevent the proposed solution from meeting customer expectations or being delivered safely, securely, and on time.
_Avoid_: Warning, issue

## Governance

**Review Record**:
An auditor-visible assessment of a proposal package or version, including the reviewer, comments, and review outcome.
_Avoid_: Feedback, chat message

**Approval Record**:
The auditable decision that authorizes or rejects a proposal version or an external business write operation.
_Avoid_: Confirmation, submit result

**Approved Action Set**:
The explicit collection of business write operations, targets, and parameters authorized by one approval decision. It is not permission for unspecified future writes.
_Avoid_: Blanket approval, role permission

**Publication Completion**:
The outcome in which every operation in an approved publication action set has been confirmed successful. An approved proposal can exist while publication is still incomplete.
_Avoid_: Content approval, task creation approval

**Derived Artifact**:
A proposal, export, memory item, or trace whose content is produced using source material. Its readership remains constrained by the sources; generation or approval does not make the content unrestricted.
_Avoid_: Public summary, declassified copy

**External Write**:
Any action that changes the opportunity workspace or creates an official business record outside the current working draft.
_Avoid_: Save, update

**Long-term Memory**:
A durable customer- or opportunity-scoped business statement with provenance and an explicit trust/lifecycle status. Retained sourced statements are not necessarily confirmed facts eligible for definite reuse.
_Avoid_: Chat history, model memory

**Memory Status**:
The recorded standing of a retained business statement, such as pending confirmation, confirmed, approved, expired, or conflicting. It is distinct from the model's confidence in its own output.
_Avoid_: Confidence score, conversational relevance

## Knowledge and Evaluation

**Knowledge Document**:
A versioned source document made available for evidence retrieval, such as product, technical, security, delivery, or customer-case material.
_Avoid_: Upload, attachment

**Published Knowledge Version**:
A particular edition of an internal knowledge document whose publication has been approved for reuse within its permitted readership. It is distinct from a newly uploaded candidate or a retired historical edition.
_Avoid_: Latest upload, candidate document

**Customer Material**:
Customer-provided requirements or background information belonging to a particular opportunity or presales task. It is distinct from reusable internal product and case-study knowledge.
_Avoid_: Shared product knowledge, global knowledge

**Evidence Snapshot**:
The set of document versions and source locations used for a particular proposal analysis attempt or version, preserved for historical traceability after later document changes. It records the historical evidence basis, not a guarantee of identical future model output.
_Avoid_: Index snapshot, cache

**Golden Dataset**:
A fixed collection of representative inputs and expected business judgments used to compare Agent capability across versions.
_Avoid_: Test fixture, sample data

**Agent Capability Evaluation**:
An assessment of requirement analysis, reasoning, generation, evidence use, and risk identification using a real model or real local LLM rather than only workflow fakes.
_Avoid_: Unit test, smoke test

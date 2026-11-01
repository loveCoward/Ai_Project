# Presales Requirements Decision Register

Updated: 2026-11-01 (Asia/Shanghai).

Status: requirements baseline v0.1 approved and frozen by the user on 2026-11-01 (Asia/Shanghai); Q1-Q45 and P01-P09 accepted. Development, deployment, real-model calls, and Git commits are not authorized by this approval.

Source: the user's project summary and explicit answers in this chat. Q1-Q8 and Q10-Q45 selected option 1. Q9 was replaced by the user's two-layer testing/evaluation policy. Repeated confirmations are recorded once. The user subsequently approved v0.1 and accepted P01-P09; those details are now approved requirements, not measured results.

## Confirmed Decisions

| ID | Confirmed choice |
| --- | --- |
| Q1 | Produce a traceable, evidence-backed proposal package; human approval is required for formal publication. |
| Q2 | Serve B2B software/IT presales teams; use a manufacturing customer buying an enterprise AI workflow platform as the fixed first demonstration. |
| Q3 | Model opportunities, presales tasks, requirements, evidence, drafts, proposal versions, reviews, approvals, business memory, and knowledge documents explicitly. |
| Q4 | Support drafts, review, return for changes, approval, and replacement by newer versions. Approved formal versions must not be overwritten. |
| Q5 | Use JWT and three local demonstration accounts with distinct sales, presales architect, and reviewer permissions; no self-approval. |
| Q6 | Label missing evidence and clarification needs; do not promote unsupported model inference to confirmed fact. |
| Q7 | Define minimum capability acceptance criteria and retain human review. Numerical targets were subsequently approved in v0.1, P07. |
| Q8 | Allow bounded degradation and partial results with visible errors, retained state, and retry or recovery. |
| Q9 | Engineering automation uses Stub/Fake Model for flow, authorization, error handling, and contracts. Agent capability evaluation uses a real OpenAI-compatible model or a real local LLM. Fake results are not capability evidence. |
| Q10 | Use a versioned structured JSON contract as the source for Markdown and HTML rendering. |
| Q11 | Separate task and version identities: an opportunity can have multiple tasks, and a task can produce multiple drafts and formal versions. |
| Q12 | Approve the complete proposal package, not independent chapters. Material changes require a new version and approval. |
| Q13 | Sales reads opportunities and proposals. Architects analyze, edit drafts, handle evidence, and submit reviews. Reviewers approve or return work and authorize external writes. Authorization applies beyond the UI; submitters cannot approve their own changes. |
| Q14 | Retain five coverage labels: covered, partially covered, uncovered, clarification needed, and explicitly out of product scope. Link individual requirements to capabilities, evidence, and risks. |
| Q15 | Retain conflicting evidence with source, version, and applicability. Do not silently let the model select a winner. |
| Q16 | Classify risks by security/compliance, deployment, integration, data, delivery, and product capability, with low, medium, high, and critical severity. Critical risks block approval; high risks need explicit reviewer acknowledgment. |
| Q17 | Retrieve current effective, reviewed knowledge versions by default. Retain historical versions for audit and record the evidence basis used by an analysis. |
| Q18 | Eligible memory includes explicitly confirmed information, sourced facts, and approved structured results. Unconfirmed model inference stays out of durable fact memory. |
| Q19 | Use a fixed golden dataset and per-metric minimums; an aggregate score must not conceal failures in permission or approval rules. |
| Q20 | Real demonstrations and capability evaluations use an explicitly configured real model. Offline Fake demonstrations require explicit selection and must not silently replace a real-model run. |
| Q21 | Maintain distinct task and version lifecycles. Task states include new, analyzing, awaiting review, awaiting supplementation, approved, completed, failed, and cancelled. Version states include draft, awaiting review, changes requested, rejected, approved, and retired. Transition guards were subsequently approved in v0.1, P01/P04. |
| Q22 | Architects create task requests and upload/process customer material. Sales remains read-only. Creating the external task record requires reviewer approval; Q31 subsequently specifies the timing relative to local preparation. |
| Q23 | Isolate customer material by opportunity or task. Reuse internal product/technical/security/delivery/case knowledge only within the user's permitted scope. |
| Q24 | Covered means documented applicable capability, supporting evidence, and no unresolved key limitation. Partial support and unclear evidence must not be reported as full coverage; Q41 subsequently resolves uncovered versus out of product scope. |
| Q25 | Architects may explain conflicts and recommend a resolution. Conflicts affecting formal commitments require reviewer confirmation. |
| Q26 | Critical risks block formal approval; high risks require acceptance or mitigation; medium risks need a recorded disposition; low risks can accompany publication as delivery cautions. Retain severity, mitigation, owner, and approval decision. |
| Q27 | Keep clarification questions in the package. Update affected requirements and risks only from customer confirmation or sourced business facts. Changes affecting formal versions require renewed review and approval. |
| Q28 | Architects can edit structured drafts, coverage assessments, evidence links, risks, and questions. Retain editor/time; material changes require renewed validation and approval. |
| Q29 | At most one active analysis per task. Reruns create new attempts and retain old traces. Cancellation is permitted before formal approval; cancelled or partially failed results cannot directly become formal versions. |
| Q30 | An approval authorizes an explicit set of external operations and parameters. Record per-operation outcomes and prevent duplicate effects. Changed parameters require renewed approval. |
| Q31 | Prepare customer materials and an initial proposal in the local workspace before creating the external task record. Create that record through Opportunity Workspace MCP only after a reviewer approves the task-creation action. This approval does not automatically authorize later publication. |
| Q32 | Architects upload and submit internal knowledge documents. Reviewers approve publication, replacement, and retirement. Sales may read permitted published documents. |
| Q33 | Distinguish mandatory and ordinary requirements. Mandatory requirements cannot remain uncovered or awaiting clarification at formal approval; partial coverage requires explicit reviewer risk acceptance. Q41 additionally blocks mandatory requirements that are out of product scope. |
| Q34 | Memory retains source, time, scope, and trust/lifecycle status such as pending confirmation, confirmed, approved, expired, and conflicting. Only confirmed or approved content qualifies for reuse as a definite fact; a source alone does not establish confirmation. |
| Q35 | Capture customer budget, timeline, and contract requirements without automatically generating binding prices, delivery dates, or contractual commitments. Such generated suggestions must remain explicitly nonbinding or awaiting confirmation. |
| Q36 | Model, embedding, reranking, and observability calls may only use content within the current user's authorized scope. Do not send restricted material across that boundary. Display or record actual model and service modes. Permitted readership alone does not yet specify approved external recipients or data classes. |
| Q37 | Freeze the golden dataset, metric definitions, and per-metric minimums before the first capability evaluation. Use deterministic metrics as hard gates with judge/human scoring as supplements, and record model versions. Specific thresholds were subsequently approved in v0.1, P07. |
| Q38 | Scope the release to a single-tenant local demonstration with one active analysis per task, a limited demonstration workload, and fixed-dataset evaluation. No production concurrency or SLA commitment is made; exact operational limits were subsequently approved in v0.1, P08. |
| Q39 | Retain partially completed proposal packages with explicit missing sections, failed nodes, degradation, and manual follow-up. Missing critical content prevents entry into formal approval. |
| Q40 | Report parsing outcomes per file and preserve valid results. Failure to parse essential customer materials requires supplementation or manual handling, not a silent complete proposal. Do not add OCR to the first release. |
| Q41 | Out-of-product-scope requirements stay in the customer requirement denominator and count as not fully covered. Mandatory out-of-scope requirements block approval. Changes to the customer requirement scope require explicit customer confirmation evidence. Product-fit coverage is separate from Agent analysis quality. |
| Q42 | Use labeled synthetic customer material and authorized public or owned sources only. Do not accept real sensitive customer material in the first release. Apply this boundary to model, embedding, reranking, and observability services; observability excludes source text by default. Synthetic cases and fictional product capabilities must be labeled, not represented as real evidence. |
| Q43 | Derived proposals, exports, memory, and traces retain source access restrictions. Role permission to view proposals is not permission to see restricted derived content. Package approval does not declassify sources; no separate declassified-publication workflow is added in the first release. |
| Q44 | Distinguish business approval from publication completion. Preserve successful writes and approval decisions when an approved action set partially fails; show publication pending recovery until every listed action is confirmed successful. Do not repeat known successes; reconcile unknown outcomes before retrying. |
| Q45 | Stop broad questionnaire rounds and prepare a consolidated requirements baseline with permissions, transitions, exceptions, output contract, acceptance examples, proposed numerical targets, and operational limits. Clearly mark author proposals for user review. Requirements approval and development authorization remain separate. |

## Reconciliation Notes

- The original summary's suggestion that only the model ID remains undecided is no longer a sufficient description of requirements readiness.
- Stub/Fake Model remains suitable for engineering automation and explicitly labeled offline demos, not for measuring real Agent competence.
- Q34 narrows reuse of the memory admitted under Q18: a sourced statement can be retained without being treated as a confirmed fact. Approval of a package does not turn its explicitly uncertain claims into confirmed facts. Expired or conflicting content must not be silently presented as current certainty.
- Q31 resolves local preparation versus external task creation. Task-creation approval and final-package/write-set approval occur at different points; the former is not blanket authorization for the latter.
- Q32 resolves knowledge publication ownership; candidate uploads are not automatically approved reusable knowledge.
- Q41 closes the mandatory out-of-product-scope loophole and retains these requirements in business coverage calculations. A reviewer cannot remove a customer requirement merely to improve the score.
- Q42 narrows data eligibility for every service under Q36; Q43 extends source restrictions to derived outputs. Read authorization does not itself authorize external disclosure or unrestricted exports.
- Q44 preserves the distinction between approved content and successful publication; partial external writes must remain visible and recoverable.
- A proposal version is not necessarily approved. Immutability applies to formal approved versions; the glossary now makes this distinction explicit.
- Evidence snapshots preserve historical provenance. They do not guarantee bit-for-bit reproduction of nondeterministic real-model output.
- Critical risks remain blocking under Q16/Q26; the exception for explicitly accepting high risk does not extend to critical risk.
- Business requirements are not claims about implemented repository behavior. No product implementation or real-model capability validation is recorded here.

## Baseline Approval

Q41-Q45 resolved the remaining broad business questions. On 2026-11-01 the user explicitly approved v0.1 and accepted P01-P09, including transition guards, derived-artifact permissions, knowledge/memory validity, publication recovery and termination, numerical capability targets and scoring, dataset composition, operational limits, and acceptance scenarios. This closes requirements review for this release, not implementation or test execution.

Baseline path: `docs/superpowers/specs/初版业务需求分析文档.md`. The filename and heading were renamed at the user's request; v0.1 requirements and approval status remain unchanged. Review status and proposal identifiers are maintained in that document.

## Approved Requirements; Execution Still Pending

- A complete task/version transition table, including guards, review snapshots, rejection, retirement, cancellation, and recovery.
- A permission matrix for source data and derived artifacts, an output contract, and an exception/recovery table.
- Requirements for a fixed golden dataset, its review process, metric formulas/rubrics, numerical targets, model provenance, and a real-model evaluation protocol are approved. The actual dataset has not been created or reviewed, and no capability result has been measured.
- Explicit file/size/concurrency/time limits and over-limit behavior, labeled as configurable first-release boundaries rather than measured production SLAs.
- The consolidated v0.1 requirements document and its 27 acceptance scenarios are approved; former P01-P09 proposals are now confirmed details. Approval is distinct from permission to begin development. No scenario or numerical target has been executed or validated as a product result.

## Documentation Boundary

This register records the approved first-release requirements baseline and its authorization boundary. State-transition, permission, output-contract and acceptance requirements are frozen by v0.1 approval. This does not claim that a machine JSON Schema, executable tests, actual golden data, or measured performance already exists. Later business changes require recorded user approval; implementation artifacts must remain consistent with this baseline.

## Accepted Detail Groups

Approval date: 2026-11-01 (Asia/Shanghai). Evidence: the user explicitly approved v0.1 and accepted P01-P09 in this chat.

| ID | Approved scope | Status |
| --- | --- | --- |
| P01 | Task/version transitions, withdrawal, revision and termination guards | Accepted |
| P02 | Derived-artifact access, exports and external disclosure boundaries | Accepted |
| P03 | Coverage, risk, clarification and submission gates | Accepted |
| P04 | Publication dependencies, recovery, unknown outcomes and termination | Accepted |
| P05 | Knowledge validity and memory trust/lifecycle | Accepted |
| P06 | Output contract, content/audit envelope and presentation | Accepted |
| P07 | Dataset plan, evaluation metrics, scoring and thresholds | Accepted |
| P08 | Operational limits, deadlines and retry budgets | Accepted |
| P09 | Delivery, explicit real evaluation and separate development authorization | Accepted |

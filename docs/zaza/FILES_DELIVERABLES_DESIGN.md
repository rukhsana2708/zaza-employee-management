# Files / Deliverables - required future Phase D module

Status: DESIGN / ROADMAP ONLY. Not implemented by Phase C. No uploads, migrations, object-storage
resources, provider connection, signed URLs, scanning, quotas, billing or AI integration exist here.
The future implementation requires separate review/approval and migration/compatibility planning.

## Product semantics and relationships

Files / Deliverables is a first-class module alongside Tasks/Projects, not an attachment afterthought.
Example: a task requires five proposals. Five logical submissions can satisfy its target; uploading a new
version of one proposal does not count as a sixth submission. Show submitted and approved counts separately.
Required outputs, submission time, submitter, immutable version, review status, approved/needs-revision and
optional manager feedback are explicit records. Computer activity or timer length proves none of these.

Employee -> Task -> optional Task Work Session context
Task/Project -> Deliverable Requirement -> File Submission -> immutable File Version(s) -> Manager Review

Task work-session provenance is optional context, not a prerequisite for legitimate off-computer work or
submission. A requirement belongs to the authorized task/project. A submission names its requirement and
uploader; a review names the EXACT version reviewed. Replacing a version cannot inherit an old approval
silently. Status/count projections preserve edits, reviews, withdrawals and tombstones in history. Task
completion/progress remains explicit or separately defined workflow; no automatic completion from telemetry.

## Data placement

Tenant PostgreSQL owns deliverable requirements, submissions, file/document IDs, local task/project/employee
relationships, uploader principal, original filename, MIME/type, size, private object key, checksum/integrity
metadata, immutable versions, timestamps, upload/scan/submission/review state, feedback and audit references.
Tenant context determines organization ownership; client-supplied tenant/employee IDs never establish authority.
Stable workforce identities are reused. Do not duplicate employee or user directories.

Private object storage (expected direction Google Cloud Storage or equivalent) holds the binary content.
Do not store large document binaries in PostgreSQL. Separate logical file identity from immutable physical
versions. Server-generated object keys use isolated tenant authority/namespaces and opaque version identities;
original filenames never become paths. A prefix convention alone is not authorization. No public buckets.
Storage/provider adapters stay outside core task/workforce models. No provider resources are created now.

## Future authorization and upload lifecycle

- Existing SaaS session -> current membership -> TenantResolver -> explicit capability -> task participation,
  employee lifecycle and applicable manager scope. File visibility does not grant coworker monitoring access.
- My Work uploads derive employee identity from explicit linkage, never from request fields. Read-only LEAVE
  access must not be reused as a mutation grant; employee work/lifecycle rules apply to new submissions.
- New upload/download/review/moderation capabilities require reviewed explicit grants. An organization-wide
  workforce-reading permission is not automatically unrestricted file-content access.
- Before upload, validate destination/task/requirement, reserve quota and enforce file-size/type limits.
  Short-lived signed upload mechanisms may be issued only after authorization, scoped to the intended object
  and supported method/content constraints. They are temporary capabilities, never permanent access.
- New content is quarantined. On completion, recheck current authority and assignment/lifecycle, verify actual
  object identity, size, checksum and MIME/type using content validation rather than trusting extension/headers.
  Scan for malware; failed/unscanned content is unavailable for ordinary downloads/review.
- Validate/sanitize filenames, avoid traversal/control characters, and do not execute active document content.
  Use safe attachment serving, appropriate content types and nosniff protections rather than rendering arbitrary
  uploaded HTML/scripts inside the application origin.
- Mint short-lived downloads after fresh tenant/object/participant/scope authorization. Signed links are bearer
  capabilities for their lifetime; strict immediate revocation may require an authenticated proxy. A URL alone
  must never provide permanent cross-tenant access. Do not log signed URLs, tokens or private object keys.
- Recheck manager reporting/task scope on each operation. Former managers cannot retain access through a
  stale session. In-flight grants have bounded lifetime and completion validation; ambiguous offline actions
  cannot silently attach files to a task after authorization is lost.

## Limits, audit, retention and deletion

Use a central entitlement boundary later for total storage, maximum file size, retention, advanced exports
and deliverable history. Reserve/reconcile quota transactionally to prevent concurrent uploads exceeding
limits. Abandoned reservations/quarantined objects need bounded cleanup. No billing is implemented now.

Business state changes and tenant audit are transactional. Record actor, action, version/reference, time and
reason; do not duplicate document bodies into generic audit or application logs. Tenant-local outbox events
can drive scanning, notifications and deletion, with idempotent consumers. No distributed event platform is
needed. Preserve version/review history and safe tombstones. Retention/offboarding must handle metadata,
objects, derived artifacts and backups under separately approved policy; no silent destructive deletion.
Disabling a future entitlement is not authorization to erase retained customer files.

## Exports and AI

Standard Sheets/Excel/CSV exports may include an authorized allowlist: task, employee display reference,
required/submitted/approved counts, timestamps, submission/review status and filenames where appropriate.
Do not automatically export document contents, object keys, credentials or signed links. Filenames are
untrusted text and require spreadsheet/formula-injection protection. Dedicated file exports/downloads need
separate authorization; a reporting-sheet integration is not a document-content access grant.

Uploaded documents are NEVER automatically sent to an AI model. Future summarization, version comparison,
deliverable checks or structured extraction require explicit authorized invocation and applicable organization,
privacy/security and provider-processing policy. File content remains authoritative; AI interpretation is
labeled derived, with source-version provenance. AI cannot grant permission, approve an output or determine
attendance/task completion by itself. No automatic file-upload-to-AI event consumer is approved.

## Future implementation/test gates

Phase D design must include requirements/submission/version/review schemas and reviewed migrations, private
storage and least-privilege authority, authenticated upload/download, quarantine/scanning, integrity/type/size
checks, quotas, deletion/retention, audit, explicit capabilities and rollout/rollback compatibility. Test tenant
A/B identical IDs, forged object keys, unauthorized signed-link minting, stale manager/employee scope,
revoked grants, malicious filenames/content, quota races, version/review binding, tombstone/history behavior,
scan failures/retries, exports excluding content, and no unsolicited AI calls.

This document records requirements only. Phase C implements personal read-only reports, not this module.

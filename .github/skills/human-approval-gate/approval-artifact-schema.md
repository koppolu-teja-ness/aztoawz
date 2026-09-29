# Approval Artifact Schema

Required fields:
- approval_id
- plan_hash
- decision (Approve|Reject|Modify)
- reviewer_id
- reviewer_signature
- rationale
- created_at
- expires_at
- related_findings[]

Validation rules:
- plan_hash must match candidate plan
- signature must verify reviewer identity
- expired artifacts are invalid
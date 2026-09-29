# Migration Plan

- Run ID: mig-5227076c6cc4
- Generated At (UTC): 2026-09-29T01:21:13.059417+00:00
- Plan Hash: sha256:ef00fb3563431b4449c813ad1f5016c0804d54980ddd7f7503b725b6d0c5a7a7
- Total Plan Items: 1

## Plan Table

| Stage | Resource | Sequence | Dependency | Owner |
| --- | --- | --- | --- | --- |
| Functions | Microsoft.Web/sites/func-app | 1 | None | platform-team |

## Mapping Decisions And Rule IDs

| Source Resource | Mapped | Target Construct | rule_id | Confidence | Caveats |
| --- | --- | --- | --- | --- | --- |
| Microsoft.Web/sites/func-app | Yes | AWS::Lambda::Function | fn-001-function-app | 0.94 | None |

## Manual-Handling Dependencies

- None

## Approval Trail

| Decision | Reviewer | Timestamp | Plan Hash | Comments |
| --- | --- | --- | --- | --- |
| NONE | N/A | N/A | N/A | No approval record captured |

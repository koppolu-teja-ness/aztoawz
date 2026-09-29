# Risk Report

- Run ID: mig-a9744576ae6e
- Generated At (UTC): 2026-09-29T01:21:13.031927+00:00
- Plan Hash: sha256:ef00fb3563431b4449c813ad1f5016c0804d54980ddd7f7503b725b6d0c5a7a7

## Risk Assessment

| Resource | Risk Level | Rationale | Required Action |
| --- | --- | --- | --- |
| Microsoft.Web/sites/func-app | AUTO (6) | Mapping confidence from rule fn-001-function-app: 0.94 | Deploy mapped construct: AWS::Lambda::Function |

## UNMAPPED Summary

- No unmapped items

## Rule IDs Referenced

- fn-001-function-app

## Approval Trail

| Decision | Reviewer | Timestamp | Plan Hash | Comments |
| --- | --- | --- | --- | --- |
| APPROVE | reviewer-1 | 2026-09-29T01:21:13.031927+00:00 | sha256:ef00fb3563431b4449c813ad1f5016c0804d54980ddd7f7503b725b6d0c5a7a7 | approved for deployment |

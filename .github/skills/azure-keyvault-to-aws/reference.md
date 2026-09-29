# Reference Mapping Table

| Azure Construct | AWS Target | Notes |
|---|---|---|
| Vault secret metadata | Secrets Manager secret | Value transfer outside LLM path |
| Key Vault key | KMS key | Key policy must be principal-scoped |
| Certificate | ACM certificate | Import/process certificate metadata only |
| Access policy/RBAC | IAM policy + role/user | Least privilege required |
| Soft delete | Recovery window/deletion controls | Preserve retention intent |
# Redaction Patterns

Allowed:
- secret_name
- secret_version
- access_scope

Forbidden:
- plaintext secret value
- private key material
- certificate private key
- token contents

Test pattern:
- Assert forbidden values never appear in logs/prompts/traces/snapshots
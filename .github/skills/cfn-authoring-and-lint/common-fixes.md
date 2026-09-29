# Common Fixes

- Replace hardcoded ARNs with parameters/mappings.
- Add DeletionPolicy and UpdateReplacePolicy for stateful resources.
- Ensure IAM policies are principal- and resource-scoped.
- Add required properties validated by cfn-lint.
- Resolve public-access findings via explicit deny or private configs.
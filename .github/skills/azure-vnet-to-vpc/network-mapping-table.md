# Network Mapping Table

| Azure | AWS | Notes |
|---|---|---|
| VNet | VPC | Address space translation required |
| Subnet | Subnet | Preserve AZ strategy where possible |
| NSG | Security Group + NACL | Split stateful/stateless controls |
| Route Table (UDR) | Route Table | Validate next-hop equivalence |
| VNet Peering | VPC Peering | CIDR overlap must be checked |
| Private Endpoint | PrivateLink/VPC Endpoint | Service-specific validation |
| ExpressRoute | Manual | Out of scope for auto-migration |
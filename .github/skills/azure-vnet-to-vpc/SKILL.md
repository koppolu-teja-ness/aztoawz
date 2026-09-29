---
name: azure-vnet-to-vpc
description: Use when mapping Azure virtual networking constructs into AWS VPC topology, controls, and connectivity while preserving security boundaries.
---
# Azure VNet to VPC Skill

## Use This Skill When
- Source includes VNet, subnet, NSG, UDR, peering, service endpoint, or private endpoint resources.

## Rules
- VNet/subnet layout MUST map to VPC/subnet design with route intent preserved.
- NSG semantics MUST be split correctly across Security Groups and NACLs.
- Service endpoints MUST map to VPC endpoints where supported.
- ExpressRoute or complex on-prem peering MUST be flagged as manual-handling dependency.
- Every mapping decision MUST cite rule_id.

See network-mapping-table.md for detailed equivalence notes.
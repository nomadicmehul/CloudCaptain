---
name: vpc-connectivity-triage
description: Find out why A cannot reach B in an AWS VPC, or reaches it only sometimes, by pinning down the exact question, exporting a read-only snapshot, walking security groups, network ACLs and route tables in both directions, naming the rule that drops the packet, and proving the smallest fix before and after
version: 1.0.0
phase: cloud
lesson: cloud/vpc-reachability
tags: [aws, vpc, security-groups, network-acl, route-tables, nat-gateway, peering, transit-gateway, reachability-analyzer, flow-logs, debugging]
---

# VPC connectivity triage

Use this when a connection inside AWS times out or is refused, works only some of the time, broke after a network change, or when someone proposes "open the security group to 0.0.0.0/0" or "recreate the NAT gateway" as the fix. During triage change nothing: every change you make before the diagnosis destroys evidence, and recreating a NAT gateway changes its public IP.

## 1. Pin down the question

Write it as one line before running anything: **source ENI, destination address, protocol, destination port, client OS**.

```bash
aws ec2 describe-instances --instance-ids i-SRC --query 'Reservations[0].Instances[0].NetworkInterfaces[].[NetworkInterfaceId,SubnetId,PrivateIpAddress,Association.PublicIp,Groups[].GroupId]'
aws ec2 describe-network-interfaces --filters Name=addresses.private-ip-address,Values=DEST_IP --query 'NetworkInterfaces[0].[NetworkInterfaceId,SubnetId,VpcId,Groups[].GroupId,Association.PublicIp]'
```

| Detail | Why it matters |
|---|---|
| destination is a private or a public IP | a public IP of your own instance goes out through the internet gateway and back, and security group references stop matching |
| destination is a DNS name | resolve it from the source (`dig +short NAME`); a private hosted zone and a public answer give different paths |
| client OS or service | sets the reply's port range: Linux 32768-60999, Windows 49152-65535, NAT gateway, load balancers and Lambda 1024-65535 |
| "times out" or "connection refused" | a refusal (RST) means the packet arrived and nothing listens: the network is fine, check the service |
| always or sometimes | "sometimes" almost always means an ACL that covers only part of the ephemeral range, or several NAT gateways or targets with different rules |

## 2. Export a snapshot (read-only)

```bash
mkdir snap
for call in describe-vpcs describe-subnets describe-route-tables describe-security-groups describe-network-acls \
  describe-network-interfaces describe-instances describe-internet-gateways describe-nat-gateways \
  describe-vpc-peering-connections describe-transit-gateways describe-transit-gateway-attachments \
  describe-vpc-endpoints describe-prefix-lists; do
  aws ec2 $call --output json > snap/$call.json
done
```

`ec2:Describe*` is enough. In a large account add `--filters Name=vpc-id,Values=VPC_A,VPC_B` to the calls that accept it. Keep the directory: it is the "before" evidence.

## 3. Walk the path

```bash
python3 vpcpath.py check i-SRC DEST_IP --port 443 --snapshot snap
python3 vpcpath.py check i-SRC DEST_IP --port 443 --ephemeral 49152-65535 --snapshot snap   # a Windows client
python3 vpcpath.py check internet i-DEST --port 443 --snapshot snap                         # inbound from the internet
```

Or with AWS's analyser, where the account allows it (billed per analysis):

```bash
NIP=$(aws ec2 create-network-insights-path --source i-SRC --destination i-DEST --protocol tcp --destination-port 443 --query NetworkInsightsPath.NetworkInsightsPathId --output text)
NIA=$(aws ec2 start-network-insights-analysis --network-insights-path-id $NIP --query NetworkInsightsAnalysis.NetworkInsightsAnalysisId --output text)
aws ec2 describe-network-insights-analyses --network-insights-analysis-ids $NIA --query 'NetworkInsightsAnalyses[0].[Status,NetworkPathFound,Explanations[].ExplanationCode]'
```

Read the first hop that is not `allow`, and note whether it is on the forward or the return path.

## 4. Confirm the deciding rule and fix it

| Code (vpcpath or Reachability Analyzer) | Confirm with | Smallest fix |
|---|---|---|
| `ENI_SG_RULES_MISMATCH`, `SG_HAS_NO_RULES` (ingress) | `aws ec2 describe-security-groups --group-ids SG --query 'SecurityGroups[0].IpPermissions'` | one rule for the port, from the source's security group (same VPC or peered) or its CIDR, never `0.0.0.0/0` on all ports |
| `ENI_SG_RULES_MISMATCH` (egress) | `--query 'SecurityGroups[0].IpPermissionsEgress'` | one outbound rule for the port and destination; the default allow-all was removed on purpose, ask why |
| `SG_REFERENCES_NOT_PRESERVED`, `SG_REFERENCING_SUPPORT` | the path crosses a NAT gateway, an internet gateway or a transit gateway | use the private IP, or a CIDR rule, or turn on security group referencing on the transit gateway and its attachments |
| `SUBNET_ACL_RESTRICTION` forward | `aws ec2 describe-network-acls --filters Name=association.subnet-id,Values=SUBNET --query 'NetworkAcls[0].Entries'` | a numbered allow rule below any deny that matches; remember the rule's CIDR is the destination outbound and the source inbound |
| `SUBNET_ACL_RESTRICTION` on the return path, or `partial` | the same command; compare the reply port range with the rule | allow the reply on the client's whole ephemeral range (1024-65535 when NAT gateways, load balancers or unknown clients are involved) |
| `NO_ROUTE_TO_DESTINATION` | `aws ec2 describe-route-tables --filters Name=association.subnet-id,Values=SUBNET` (no result: the main table applies) | add the route in the table the subnet really uses; for peering, on both sides |
| `BAD_STATE_ROUTE` (blackhole) | the route's `State` | the target (NAT gateway, ENI, peering) was deleted: point the route at its replacement |
| `IGW_PUBLIC_IP_ASSOCIATION_FOR_EGRESS` | `Association.PublicIp` on the ENI | route through a NAT gateway instead, or attach an Elastic IP if the instance must be public |
| `IGW_PRIVATE_IP_ASSOCIATION_FOR_INGRESS` | the destination has no public IP | put a load balancer in a public subnet in front of it; do not make the instance public |
| `IGW_DESTINATION_ADDRESS_NOT_IN_RFC1918_EGRESS` | a private destination matched `0.0.0.0/0 -> igw-` | the peering or transit gateway route is missing from this subnet's table |
| `PCX_REQUIRES_ADDRESS_IN_VPC_CIDR` | the destination is beyond the peer VPC | peering is not transitive: use a transit gateway, or a NAT gateway in your own VPC |
| `VPCPATH_ASYMMETRIC_RETURN` | the destination subnet's route for the client's address | replies must leave the way requests came: an internet-facing instance needs a subnet that routes to the internet gateway |
| `NO_POSSIBLE_DESTINATION` | no ENI has that address | stale DNS record, terminated instance, wrong IP |
| `unknown` (VPN, appliance, private NAT) | the route target | continue the analysis on the far side (on-premises firewall, appliance rules) |

Before changing anything, prove the fix offline: edit the JSON in a copy of `snap/` the way the change would, and rerun `check`. It must turn the verdict to `reachable` without changing any other question in your matrix.

## 5. Cross-check with flow logs

```bash
aws ec2 describe-flow-logs --filter Name=resource-id,Values=VPC_ID
```

| Records on the destination ENI for one connection | Meaning |
|---|---|
| `ACCEPT` request, `REJECT` reply (reverse 5-tuple) | a network ACL dropped the reply: stateless return path |
| one `REJECT` for the request | a security group or the destination subnet's ACL dropped it on the way in |
| `ACCEPT` both ways, client still times out | the drop is after the ENI: the other subnet's ACL, an asymmetric route, or the client side |
| nothing at all | the packet never reached this ENI: routing, the source side, or the wrong destination address |

## 6. Verify

Apply the one change, export a second snapshot, and rerun the same question and the rest of the matrix:

```bash
python3 vpcpath.py check i-SRC DEST_IP --port 443 --snapshot snap-after
python3 vpcpath.py matrix --questions questions.json --snapshot snap-after
```

Then test with real traffic from the source (`curl -sv`, `nc -vz HOST PORT`), several times when the symptom was intermittent.

## 7. Report

State: the question (source ENI, destination, protocol, port, client port range); the verdict before (reachable, blocked or partial) with the hop, the component and the rule that decided, and the explanation code; the flow log evidence; the single change made, as the exact CLI command or Terraform diff; the verdict after; and which other questions in the matrix were rerun. Never list "opened the security group to 0.0.0.0/0", "recreated the NAT gateway" or "replaced the custom ACL with the default" as a resolution.

"""vpcpath: answer "can A reach B on TCP 443" from `aws ec2 describe-*` JSON. It walks security groups, network ACLs, route tables (longest prefix match), internet and NAT gateways, peering, transit gateways and gateway endpoints, checks the return path, and names the rule that decided every hop."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Network = ipaddress.IPv4Network
Address = ipaddress.IPv4Address
Ports = Tuple[int, int]

PROTOCOL_NUMBERS = {"tcp": 6, "udp": 17, "icmp": 1}
PROTOCOL_NAMES = {6: "tcp", 17: "udp", 1: "icmp", -1: "all"}
SG_PROTOCOLS = {"tcp": 6, "udp": 17, "icmp": 1, "icmpv6": 58, "-1": -1, "all": -1}
ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
LINUX_EPHEMERAL: Ports = (32768, 60999)
ANY_CLIENT: Ports = (1024, 65535)
NAT_PORTS: Ports = (1024, 65535)
ASTERISK = 32767
INTERNET = ipaddress.IPv4Address("198.51.100.10")
RFC1918 = [ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
RFC6598 = ipaddress.IPv4Network("100.64.0.0/10")
EVERYWHERE = ipaddress.IPv4Network("0.0.0.0/0")
PROPAGATED_ORIGINS = ("EnableVgwRoutePropagation",)
ROUTE_TARGET_KEYS = (
    "GatewayId",
    "NatGatewayId",
    "VpcPeeringConnectionId",
    "TransitGatewayId",
    "NetworkInterfaceId",
    "InstanceId",
    "EgressOnlyInternetGatewayId",
    "LocalGatewayId",
    "CarrierGatewayId",
    "CoreNetworkArn",
)
TARGET_KINDS = (
    ("local", "local"),
    ("igw-", "igw"),
    ("nat-", "nat"),
    ("pcx-", "peering"),
    ("tgw-", "tgw"),
    ("vpce-", "endpoint"),
    ("vgw-", "vgw"),
    ("eigw-", "eigw"),
    ("eni-", "eni"),
    ("i-", "instance"),
)
ATTACHED = ("available", "attached")
ALLOW, DENY, PARTIAL, INFO, UNKNOWN = "allow", "deny", "partial", "info", "unknown"
REFERENCES_OK = ("", "")
LOST_AT_GATEWAY = ("SG_REFERENCES_NOT_PRESERVED", "the packet crossed a NAT or internet gateway, so it no longer carries a member's private IP")
LOST_OUTBOUND = ("SG_REFERENCES_NOT_PRESERVED", "outbound rules can reference groups only in the same VPC or a peered VPC")
LOST_AT_TGW = ("SG_REFERENCING_SUPPORT", "the transit gateway does not have security group referencing support turned on, so group membership is lost")
VERDICTS = ("reachable", "partial", "blocked", "unknown")


class InputError(ValueError):
    pass


class Stop(Exception):
    pass


def network(text: Any, where: str) -> Network:
    try:
        return ipaddress.IPv4Network(str(text), strict=False)
    except ValueError:
        raise InputError(f"{where}: {text!r} is not an IPv4 CIDR block")


def address(text: Any, where: str) -> Address:
    try:
        return ipaddress.IPv4Address(str(text).strip())
    except ValueError:
        raise InputError(f"{where}: {text!r} is not an IPv4 address")


def is_rfc1918(ip: Address) -> bool:
    return any(ip in n for n in RFC1918)


def name_of(obj: Dict[str, Any]) -> str:
    for key in ("Tags", "TagSet"):
        for tag in obj.get(key) or []:
            if tag.get("Key") == "Name":
                return str(tag.get("Value") or "")
    return ""


def show_ports(ports: Ports) -> str:
    return str(ports[0]) if ports[0] == ports[1] else f"{ports[0]}-{ports[1]}"


def parse_ports(text: str) -> Ports:
    low, sep, high = str(text).partition("-")
    try:
        ports = (int(low), int(high if sep else low))
    except ValueError:
        raise InputError(f"port range {text!r} is not LOW-HIGH, for example 32768-60999")
    if not 0 <= ports[0] <= ports[1] <= 65535:
        raise InputError(f"port range {text!r} must be within 0-65535, low first")
    return ports


@dataclass
class Vpc:
    id: str
    cidrs: List[Network]
    name: str = ""


@dataclass
class Subnet:
    id: str
    vpc: str
    cidr: Network
    zone: str = ""
    name: str = ""


@dataclass
class Route:
    destination: Optional[Network]
    prefix_list: str
    target: str
    state: str = "active"
    origin: str = "CreateRoute"

    @property
    def kind(self) -> str:
        for prefix, kind in TARGET_KINDS:
            if self.target == prefix or (prefix.endswith("-") and self.target.startswith(prefix)):
                return kind
        return "other"

    @property
    def rank(self) -> int:
        if self.origin in PROPAGATED_ORIGINS:
            return 0
        return 1 if self.prefix_list else 2

    def describe(self) -> str:
        text = f"{self.prefix_list or self.destination} -> {self.target}"
        if self.origin in PROPAGATED_ORIGINS:
            text += " (propagated)"
        if self.state == "blackhole":
            text += " (blackhole)"
        return text


@dataclass
class RouteTable:
    id: str
    vpc: str
    routes: List[Route]
    subnets: List[str]
    main: bool
    name: str = ""


@dataclass
class Permission:
    protocol: int
    low: int
    high: int
    cidrs: List[Network]
    groups: List[str]
    prefix_lists: List[str]

    def covers(self, protocol: int, port: int) -> bool:
        if self.protocol == -1:
            return True
        if self.protocol != protocol:
            return False
        if protocol in (6, 17):
            return self.low <= port <= self.high
        if protocol == 1:
            return self.low == -1 or self.low == port
        return True

    def describe(self) -> str:
        if self.protocol == -1:
            return "all"
        name = PROTOCOL_NAMES.get(self.protocol, str(self.protocol))
        if self.protocol in (6, 17):
            return f"{name} {show_ports((self.low, self.high))}"
        if self.protocol == 1:
            return "icmp all" if self.low == -1 else f"icmp type {self.low}"
        return name


@dataclass
class SecurityGroup:
    id: str
    name: str
    vpc: str
    ingress: List[Permission]
    egress: List[Permission]


@dataclass
class AclEntry:
    number: int
    egress: bool
    protocol: int
    low: int
    high: int
    cidr: Network
    allow: bool

    def matches(self, protocol: int, remote: Address) -> bool:
        return (self.protocol == -1 or self.protocol == protocol) and remote in self.cidr

    def describe(self) -> str:
        number = "*" if self.number >= ASTERISK else str(self.number)
        if self.protocol == -1:
            what = "all"
        elif self.protocol in (6, 17):
            what = f"{PROTOCOL_NAMES[self.protocol]} {show_ports((self.low, self.high))}"
        elif self.protocol == 1:
            what = "icmp all" if (self.low, self.high) == (0, 255) else f"icmp type {self.low}"
        else:
            what = f"protocol {self.protocol}"
        return f"rule {number} {'allow' if self.allow else 'deny'} {what} {'to' if self.egress else 'from'} {self.cidr}"


def asterisk(egress: bool) -> AclEntry:
    return AclEntry(ASTERISK, egress, -1, 0, 65535, EVERYWHERE, False)


@dataclass
class NetworkAcl:
    id: str
    vpc: str
    entries: List[AclEntry]
    subnets: List[str]
    default: bool
    name: str = ""


@dataclass
class Interface:
    id: str
    vpc: str
    subnet: str
    private_ips: List[Address]
    public_ips: Dict[Address, Address]
    groups: List[str]
    instance: str = ""
    name: str = ""
    primary: bool = True

    @property
    def label(self) -> str:
        return self.name or self.instance or self.id


@dataclass
class InternetGateway:
    id: str
    vpcs: List[str]


@dataclass
class NatGateway:
    id: str
    vpc: str
    subnet: str
    private_ip: Optional[Address]
    public_ip: Optional[Address]
    state: str
    connectivity: str


@dataclass
class Peering:
    id: str
    requester: str
    accepter: str
    requester_cidrs: List[Network]
    accepter_cidrs: List[Network]
    status: str


@dataclass
class TgwAttachment:
    id: str
    tgw: str
    vpc: str
    state: str


@dataclass
class Endpoint:
    id: str
    vpc: str
    service: str
    kind: str


@dataclass
class Snapshot:
    vpcs: Dict[str, Vpc] = field(default_factory=dict)
    subnets: Dict[str, Subnet] = field(default_factory=dict)
    route_tables: List[RouteTable] = field(default_factory=list)
    groups: Dict[str, SecurityGroup] = field(default_factory=dict)
    acls: List[NetworkAcl] = field(default_factory=list)
    interfaces: Dict[str, Interface] = field(default_factory=dict)
    igws: Dict[str, InternetGateway] = field(default_factory=dict)
    nats: Dict[str, NatGateway] = field(default_factory=dict)
    peerings: Dict[str, Peering] = field(default_factory=dict)
    tgw_sg_referencing: Dict[str, bool] = field(default_factory=dict)
    tgw_attachments: List[TgwAttachment] = field(default_factory=list)
    endpoints: Dict[str, Endpoint] = field(default_factory=dict)
    prefix_lists: Dict[str, Tuple[str, List[Network]]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def subnet(self, subnet_id: str) -> Subnet:
        if subnet_id not in self.subnets:
            raise InputError(f"subnet {subnet_id} is not in the snapshot; export `aws ec2 describe-subnets`")
        return self.subnets[subnet_id]

    def route_table_for(self, subnet_id: str) -> RouteTable:
        subnet = self.subnet(subnet_id)
        explicit = [t for t in self.route_tables if subnet_id in t.subnets]
        if explicit:
            return explicit[0]
        main = [t for t in self.route_tables if t.vpc == subnet.vpc and t.main]
        if main:
            return main[0]
        raise InputError(f"no route table covers {subnet_id} (no explicit association and no main table for {subnet.vpc}); export `aws ec2 describe-route-tables`")

    def acl_for(self, subnet_id: str) -> NetworkAcl:
        subnet = self.subnet(subnet_id)
        explicit = [a for a in self.acls if subnet_id in a.subnets]
        if explicit:
            return explicit[0]
        default = [a for a in self.acls if a.vpc == subnet.vpc and a.default]
        if default:
            return default[0]
        raise InputError(f"no network ACL covers {subnet_id}; export `aws ec2 describe-network-acls`")

    def interface_with_private_ip(self, ip: Address, vpc: str = "") -> Optional[Interface]:
        for iface in self.interfaces.values():
            if ip in iface.private_ips and (not vpc or iface.vpc == vpc):
                return iface
        return None

    def interface_with_public_ip(self, ip: Address) -> Optional[Interface]:
        for iface in self.interfaces.values():
            if ip in iface.public_ips:
                return iface
        return None

    def vpc_cidrs(self, vpc_id: str) -> List[Network]:
        if vpc_id in self.vpcs:
            return self.vpcs[vpc_id].cidrs
        return sorted({s.cidr for s in self.subnets.values() if s.vpc == vpc_id})

    def igws_of(self, vpc_id: str) -> List[str]:
        return sorted(g.id for g in self.igws.values() if vpc_id in g.vpcs)

    def label(self, component: str) -> str:
        names = {**{k: v.name for k, v in self.vpcs.items()}, **{k: v.name for k, v in self.subnets.items()}}
        names.update({t.id: t.name for t in self.route_tables})
        names.update({a.id: a.name for a in self.acls})
        names.update({k: v.name for k, v in self.groups.items()})
        name = names.get(component, "")
        return f"{component} ({name})" if name and name != component else component


def read_documents(paths: Sequence[str]) -> Dict[str, List[Any]]:
    merged: Dict[str, List[Any]] = {}
    files: List[str] = []
    for path in paths:
        if path != "-" and os.path.isdir(path):
            files.extend(os.path.join(path, n) for n in sorted(os.listdir(path)) if n.endswith(".json"))
        else:
            files.append(path)
    if not files:
        raise InputError(f"no .json files in {', '.join(paths)}; save `aws ec2 describe-* --output json` there")
    for path in files:
        try:
            if path == "-":
                doc = json.load(sys.stdin)
            else:
                with open(path, encoding="utf-8") as handle:
                    doc = json.load(handle)
        except OSError as exc:
            raise InputError(f"cannot read {path}: {exc.strerror}")
        except json.JSONDecodeError as exc:
            raise InputError(f"{path} is not JSON: {exc.msg} at line {exc.lineno}")
        if not isinstance(doc, dict):
            raise InputError(f"{path}: expected the object an `aws ec2 describe-*` call prints, found a {type(doc).__name__}")
        for key, value in doc.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
    return merged


def make_permission(raw: Dict[str, Any], where: str) -> Permission:
    text = str(raw.get("IpProtocol", "-1")).lower()
    if text in SG_PROTOCOLS:
        protocol = SG_PROTOCOLS[text]
    elif text.isdigit():
        protocol = int(text)
    else:
        raise InputError(f"{where}: unknown IpProtocol {text!r}")
    low, high = raw.get("FromPort"), raw.get("ToPort")
    if protocol in (6, 17):
        low = 0 if low in (None, -1) else int(low)
        high = 65535 if high in (None, -1) else int(high)
    else:
        low = -1 if low is None else int(low)
        high = -1 if high is None else int(high)
    return Permission(
        protocol,
        low,
        high,
        [network(r["CidrIp"], where) for r in raw.get("IpRanges") or [] if r.get("CidrIp")],
        [p["GroupId"] for p in raw.get("UserIdGroupPairs") or [] if p.get("GroupId")],
        [p["PrefixListId"] for p in raw.get("PrefixListIds") or [] if p.get("PrefixListId")],
    )


def make_acl_entry(raw: Dict[str, Any], where: str) -> Optional[AclEntry]:
    if not raw.get("CidrBlock"):
        return None
    text = str(raw.get("Protocol", "-1")).lower()
    protocol = PROTOCOL_NUMBERS.get(text, -1 if text in ("-1", "all") else None)
    if protocol is None:
        if not text.isdigit():
            raise InputError(f"{where}: unknown Protocol {text!r}")
        protocol = int(text)
    low, high = 0, 65535
    if protocol in (6, 17) and raw.get("PortRange"):
        low, high = int(raw["PortRange"]["From"]), int(raw["PortRange"]["To"])
    elif protocol == 1:
        kind = int((raw.get("IcmpTypeCode") or {}).get("Type", -1))
        low, high = (0, 255) if kind == -1 else (kind, kind)
    action = str(raw.get("RuleAction", "")).lower()
    if action not in ("allow", "deny"):
        raise InputError(f"{where}: RuleAction must be allow or deny, found {action!r}")
    return AclEntry(int(raw["RuleNumber"]), bool(raw.get("Egress")), protocol, low, high, network(raw["CidrBlock"], where), action == "allow")


def make_route(raw: Dict[str, Any], where: str) -> Optional[Route]:
    destination = network(raw["DestinationCidrBlock"], where) if raw.get("DestinationCidrBlock") else None
    prefix_list = str(raw.get("DestinationPrefixListId") or "")
    if destination is None and not prefix_list:
        return None
    target = next((str(raw[k]) for k in ROUTE_TARGET_KEYS if raw.get(k)), "")
    if not target:
        raise InputError(f"{where}: route to {destination or prefix_list} has no target")
    return Route(destination, prefix_list, target, str(raw.get("State") or "active"), str(raw.get("Origin") or "CreateRoute"))


def add_interface(snap: Snapshot, raw: Dict[str, Any], instance: str = "", instance_name: str = "") -> None:
    eni_id = raw.get("NetworkInterfaceId")
    if not eni_id:
        return
    where = f"network interface {eni_id}"
    attachment = raw.get("Attachment") or {}
    instance = str(attachment.get("InstanceId") or instance)
    privates: List[Address] = []
    publics: Dict[Address, Address] = {}
    for entry in raw.get("PrivateIpAddresses") or [{"PrivateIpAddress": raw.get("PrivateIpAddress"), "Association": raw.get("Association")}]:
        if not entry.get("PrivateIpAddress"):
            continue
        private = address(entry["PrivateIpAddress"], where)
        privates.append(private)
        public = (entry.get("Association") or {}).get("PublicIp")
        if public:
            publics[address(public, where)] = private
    if raw.get("PrivateIpAddress") and not privates:
        privates.append(address(raw["PrivateIpAddress"], where))
    top = (raw.get("Association") or {}).get("PublicIp")
    if top and privates:
        publics.setdefault(address(top, where), privates[0])
    if not privates:
        raise InputError(f"{where} has no private IPv4 address")
    existing = snap.interfaces.get(eni_id)
    name = name_of(raw) or instance_name or (existing.name if existing else "")
    snap.interfaces[eni_id] = Interface(
        eni_id,
        str(raw.get("VpcId") or (existing.vpc if existing else "")),
        str(raw.get("SubnetId") or (existing.subnet if existing else "")),
        privates,
        publics,
        [g["GroupId"] for g in raw.get("Groups") or [] if g.get("GroupId")],
        instance or (existing.instance if existing else ""),
        name,
        int(attachment.get("DeviceIndex", 0) or 0) == 0,
    )


def build_snapshot(doc: Dict[str, List[Any]]) -> Snapshot:
    snap = Snapshot()
    try:
        for raw in doc.get("Vpcs", []):
            cidrs = [
                network(a["CidrBlock"], raw["VpcId"])
                for a in raw.get("CidrBlockAssociationSet") or []
                if (a.get("CidrBlockState") or {}).get("State", "associated") == "associated"
            ]
            if not cidrs and raw.get("CidrBlock"):
                cidrs = [network(raw["CidrBlock"], raw["VpcId"])]
            snap.vpcs[raw["VpcId"]] = Vpc(raw["VpcId"], cidrs, name_of(raw))
        for raw in doc.get("Subnets", []):
            snap.subnets[raw["SubnetId"]] = Subnet(raw["SubnetId"], raw["VpcId"], network(raw["CidrBlock"], raw["SubnetId"]), str(raw.get("AvailabilityZone") or ""), name_of(raw))
        for raw in doc.get("RouteTables", []):
            where = f"route table {raw['RouteTableId']}"
            routes = [r for r in (make_route(x, where) for x in raw.get("Routes") or []) if r is not None]
            associations = raw.get("Associations") or []
            snap.route_tables.append(
                RouteTable(
                    raw["RouteTableId"],
                    raw["VpcId"],
                    routes,
                    [a["SubnetId"] for a in associations if a.get("SubnetId")],
                    any(a.get("Main") for a in associations),
                    name_of(raw),
                )
            )
        for raw in doc.get("SecurityGroups", []):
            where = f"security group {raw['GroupId']}"
            snap.groups[raw["GroupId"]] = SecurityGroup(
                raw["GroupId"],
                name_of(raw) or str(raw.get("GroupName") or ""),
                str(raw.get("VpcId") or ""),
                [make_permission(p, where) for p in raw.get("IpPermissions") or []],
                [make_permission(p, where) for p in raw.get("IpPermissionsEgress") or []],
            )
        for raw in doc.get("NetworkAcls", []):
            where = f"network ACL {raw['NetworkAclId']}"
            entries = [e for e in (make_acl_entry(x, where) for x in raw.get("Entries") or []) if e is not None]
            snap.acls.append(
                NetworkAcl(
                    raw["NetworkAclId"],
                    raw["VpcId"],
                    sorted(entries, key=lambda e: (e.egress, e.number)),
                    [a["SubnetId"] for a in raw.get("Associations") or [] if a.get("SubnetId")],
                    bool(raw.get("IsDefault")),
                    name_of(raw),
                )
            )
        for raw in doc.get("NetworkInterfaces", []):
            add_interface(snap, raw)
        for reservation in doc.get("Reservations", []):
            for instance in reservation.get("Instances") or []:
                iid = instance["InstanceId"]
                label = name_of(instance)
                for raw in instance.get("NetworkInterfaces") or []:
                    if raw.get("NetworkInterfaceId") in snap.interfaces:
                        known = snap.interfaces[raw["NetworkInterfaceId"]]
                        known.instance = known.instance or iid
                        known.name = known.name or label
                    else:
                        add_interface(snap, dict(raw, VpcId=raw.get("VpcId") or instance.get("VpcId")), iid, label)
                if not instance.get("NetworkInterfaces") and instance.get("PrivateIpAddress"):
                    add_interface(
                        snap,
                        {
                            "NetworkInterfaceId": f"{iid}-eth0",
                            "VpcId": instance.get("VpcId"),
                            "SubnetId": instance.get("SubnetId"),
                            "PrivateIpAddress": instance["PrivateIpAddress"],
                            "Association": {"PublicIp": instance["PublicIpAddress"]} if instance.get("PublicIpAddress") else None,
                            "Groups": instance.get("SecurityGroups") or [],
                        },
                        iid,
                        label,
                    )
        for raw in doc.get("InternetGateways", []):
            vpcs = [a["VpcId"] for a in raw.get("Attachments") or [] if a.get("State") in ATTACHED]
            snap.igws[raw["InternetGatewayId"]] = InternetGateway(raw["InternetGatewayId"], vpcs)
        for raw in doc.get("NatGateways", []):
            where = f"NAT gateway {raw['NatGatewayId']}"
            addresses = (raw.get("NatGatewayAddresses") or [{}])[0]
            snap.nats[raw["NatGatewayId"]] = NatGateway(
                raw["NatGatewayId"],
                raw["VpcId"],
                raw["SubnetId"],
                address(addresses["PrivateIp"], where) if addresses.get("PrivateIp") else None,
                address(addresses["PublicIp"], where) if addresses.get("PublicIp") else None,
                str(raw.get("State") or ""),
                str(raw.get("ConnectivityType") or "public"),
            )
        for raw in doc.get("VpcPeeringConnections", []):
            where = f"peering connection {raw['VpcPeeringConnectionId']}"
            sides = []
            for key in ("RequesterVpcInfo", "AccepterVpcInfo"):
                info = raw.get(key) or {}
                cidrs = [network(c["CidrBlock"], where) for c in info.get("CidrBlockSet") or [] if c.get("CidrBlock")]
                if not cidrs and info.get("CidrBlock"):
                    cidrs = [network(info["CidrBlock"], where)]
                sides.append((str(info.get("VpcId") or ""), cidrs))
            snap.peerings[raw["VpcPeeringConnectionId"]] = Peering(
                raw["VpcPeeringConnectionId"], sides[0][0], sides[1][0], sides[0][1], sides[1][1], str((raw.get("Status") or {}).get("Code") or "")
            )
        for raw in doc.get("TransitGateways", []):
            snap.tgw_sg_referencing[raw["TransitGatewayId"]] = (raw.get("Options") or {}).get("SecurityGroupReferencingSupport") == "enable"
        for raw in doc.get("TransitGatewayAttachments", []):
            if raw.get("ResourceType", "vpc") == "vpc":
                snap.tgw_attachments.append(TgwAttachment(raw["TransitGatewayAttachmentId"], raw["TransitGatewayId"], raw["ResourceId"], str(raw.get("State") or "")))
        for raw in doc.get("TransitGatewayVpcAttachments", []):
            if not any(a.id == raw["TransitGatewayAttachmentId"] for a in snap.tgw_attachments):
                snap.tgw_attachments.append(TgwAttachment(raw["TransitGatewayAttachmentId"], raw["TransitGatewayId"], raw["VpcId"], str(raw.get("State") or "")))
        for raw in doc.get("VpcEndpoints", []):
            kind = raw.get("VpcEndpointType") or ("Gateway" if raw.get("RouteTableIds") else "Interface")
            snap.endpoints[raw["VpcEndpointId"]] = Endpoint(raw["VpcEndpointId"], raw["VpcId"], str(raw.get("ServiceName") or ""), kind)
        for raw in doc.get("PrefixLists", []):
            where = f"prefix list {raw['PrefixListId']}"
            texts = list(raw.get("Cidrs") or []) + [e["Cidr"] for e in raw.get("Entries") or [] if e.get("Cidr")]
            cidrs = [network(c, where) for c in texts if ":" not in str(c)]
            snap.prefix_lists[raw["PrefixListId"]] = (str(raw.get("PrefixListName") or ""), cidrs)
    except KeyError as exc:
        raise InputError(f"a describe output is missing the field {exc.args[0]}; pass the JSON exactly as `aws ec2 describe-* --output json` prints it")
    if not snap.subnets:
        raise InputError("no Subnets in the snapshot; export at least describe-vpcs, describe-subnets, describe-route-tables, describe-security-groups, describe-network-acls and describe-network-interfaces")
    for table in snap.route_tables:
        for route in table.routes:
            if route.prefix_list and route.prefix_list not in snap.prefix_lists:
                snap.warnings.append(f"{table.id} routes {route.prefix_list} to {route.target}, but the prefix list's CIDRs are not in the snapshot (export `aws ec2 describe-prefix-lists`); that route matches nothing")
    return snap


def load_snapshot(paths: Sequence[str]) -> Snapshot:
    return build_snapshot(read_documents(paths))


def route_networks(snap: Snapshot, route: Route) -> List[Network]:
    if route.destination is not None:
        return [route.destination]
    return snap.prefix_lists.get(route.prefix_list, ("", []))[1]


def lookup(snap: Snapshot, table: RouteTable, dest: Address) -> Optional[Tuple[Route, Network, List[str]]]:
    candidates = []
    for route in table.routes:
        matching = [n for n in route_networks(snap, route) if dest in n]
        if matching:
            best = max(matching, key=lambda n: n.prefixlen)
            candidates.append((best.prefixlen, route.rank, route, best))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], -c[1]))
    winner = candidates[0]
    return winner[2], winner[3], [c[2].describe() for c in candidates[1:]]


def acl_decide(acl: NetworkAcl, egress: bool, protocol: int, ports: Ports, remote: Address) -> List[Tuple[int, int, AclEntry]]:
    remaining = [ports]
    decided: List[Tuple[int, int, AclEntry]] = []
    for entry in acl.entries:
        if entry.egress != egress or not entry.matches(protocol, remote):
            continue
        span = (0, 65535) if entry.protocol == -1 else (entry.low, entry.high)
        left: List[Ports] = []
        for low, high in remaining:
            cut_low, cut_high = max(low, span[0]), min(high, span[1])
            if cut_low > cut_high:
                left.append((low, high))
                continue
            decided.append((cut_low, cut_high, entry))
            if low < cut_low:
                left.append((low, cut_low - 1))
            if cut_high < high:
                left.append((cut_high + 1, high))
        remaining = left
        if not remaining:
            break
    decided.extend((low, high, asterisk(egress)) for low, high in remaining)
    return sorted(decided, key=lambda d: d[0])


@dataclass
class Party:
    label: str
    address: Address
    interface: Optional[Interface] = None
    public: bool = False

    def describe(self, snap: Snapshot) -> str:
        if self.interface is None:
            return f"{self.label} ({self.address})" if self.label != str(self.address) else str(self.address)
        iface = self.interface
        how = "public IP of " if self.public else ""
        return f"{iface.label} {self.address} ({how}{iface.id}, {iface.subnet})"

    def to_json(self) -> Dict[str, Any]:
        iface = self.interface
        return {
            "label": self.label,
            "address": str(self.address),
            "interface": iface.id if iface else None,
            "instance": iface.instance if iface else None,
            "subnet": iface.subnet if iface else None,
            "vpc": iface.vpc if iface else None,
            "public": self.public,
        }


def first_public(iface: Interface, private: Address) -> Optional[Address]:
    mapped = sorted(p for p, q in iface.public_ips.items() if q == private)
    return mapped[0] if mapped else None


def find_interface(snap: Snapshot, text: str) -> Interface:
    if text in snap.interfaces:
        return snap.interfaces[text]
    matches = [i for i in snap.interfaces.values() if text in (i.instance, i.name)]
    owners = {i.instance or i.id for i in matches}
    if len(owners) > 1:
        raise InputError(f"{text!r} names several instances ({', '.join(sorted(owners))}); use an instance or ENI id")
    if not matches:
        raise InputError(f"{text!r} is not an instance id, ENI id, Name tag, IP address, prefix list or 'internet' in this snapshot")
    return sorted(matches, key=lambda i: (not i.primary, i.id))[0]


def resolve(snap: Snapshot, text: str, from_internet: bool = False) -> Party:
    text = text.strip()
    if text == "internet":
        return Party("internet", INTERNET)
    for pl_id, (pl_name, cidrs) in snap.prefix_lists.items():
        if text in (pl_id, pl_name) and cidrs:
            return Party(pl_name or pl_id, next(cidrs[0].hosts()) if cidrs[0].num_addresses > 1 else cidrs[0].network_address)
    try:
        ip: Optional[Address] = ipaddress.IPv4Address(text)
    except ValueError:
        ip = None
    if ip is not None:
        private = snap.interface_with_private_ip(ip)
        if private is not None:
            return Party(private.label, ip, private)
        public = snap.interface_with_public_ip(ip)
        if public is not None:
            return Party(public.label, ip, public, public=True)
        return Party(str(ip), ip)
    iface = find_interface(snap, text)
    if from_internet and iface.public_ips:
        return Party(iface.label, sorted(iface.public_ips)[0], iface, public=True)
    return Party(iface.label, iface.private_ips[0], iface)


@dataclass
class Hop:
    direction: str
    kind: str
    component: str
    decision: str
    rule: str
    code: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"direction": self.direction, "kind": self.kind, "component": self.component, "decision": self.decision, "rule": self.rule, "code": self.code}


@dataclass
class Leg:
    subnet: str
    outbound: bool
    remote: Address
    reply_ports: Ports


@dataclass
class Answer:
    source: Party
    destination: Party
    protocol: int
    port: int
    client_ports: Ports
    hops: List[Hop]
    verdict: str
    reason: str
    code: str
    warnings: List[str]

    @property
    def blocked_at(self) -> Optional[Hop]:
        return next((h for h in self.hops if h.decision in (DENY, UNKNOWN)), None)


class Tracer:
    def __init__(self, snap: Snapshot, protocol: int, port: int, client_ports: Ports):
        self.snap = snap
        self.protocol = protocol
        self.port = ICMP_ECHO_REQUEST if protocol == 1 else port
        self.source_ports = client_ports
        self.hops: List[Hop] = []
        self.legs: List[Leg] = []
        self.reply_route: Optional[Tuple[str, Address, str, str]] = None
        self.stateful: List[str] = []

    @property
    def what(self) -> str:
        return "icmp echo" if self.protocol == 1 else f"{PROTOCOL_NAMES[self.protocol]} {self.port}"

    def add(self, direction: str, kind: str, component: str, decision: str, rule: str, code: str = "") -> None:
        self.hops.append(Hop(direction, kind, component, decision, rule, code))
        if decision in (DENY, UNKNOWN):
            raise Stop()

    def reply_ports(self) -> Ports:
        return (ICMP_ECHO_REPLY, ICMP_ECHO_REPLY) if self.protocol == 1 else self.source_ports

    def leg(self, subnet_id: str, outbound: bool, remote: Address) -> None:
        acl = self.snap.acl_for(subnet_id)
        self.legs.append(Leg(subnet_id, outbound, remote, self.reply_ports()))
        entry = acl_decide(acl, outbound, self.protocol, (self.port, self.port), remote)[0][2]
        kind = "nacl-out" if outbound else "nacl-in"
        text = f"{self.snap.label(acl.id)} on {subnet_id}: {entry.describe()} decides {self.what} {'to' if outbound else 'from'} {remote}"
        self.add("forward", kind, acl.id, ALLOW if entry.allow else DENY, text, "" if entry.allow else "SUBNET_ACL_RESTRICTION")

    def match_peer(self, perm: Permission, remote: Address, counterpart: Optional[Interface], references: Tuple[str, str]) -> Tuple[str, bool]:
        for cidr in perm.cidrs:
            if remote in cidr:
                return str(cidr), False
        for pl in perm.prefix_lists:
            if pl not in self.snap.prefix_lists:
                self.snap.warnings.append(f"a security group rule references {pl}, whose CIDRs are not in the snapshot")
            elif any(remote in c for c in self.snap.prefix_lists[pl][1]):
                return pl, False
        member = counterpart is not None and any(g in counterpart.groups for g in perm.groups)
        if member and not references[0] and counterpart is not None and remote in counterpart.private_ips:
            return next(g for g in perm.groups if g in counterpart.groups), False
        return "", member

    def sg_check(self, iface: Interface, egress: bool, remote: Address, counterpart: Optional[Interface], references: Tuple[str, str]) -> None:
        direction = "egress" if egress else "ingress"
        kind = f"sg-{direction}"
        total = 0
        lost = ""
        for gid in iface.groups:
            group = self.snap.groups.get(gid)
            if group is None:
                raise InputError(f"security group {gid} of {iface.id} is not in the snapshot; export `aws ec2 describe-security-groups`")
            perms = group.egress if egress else group.ingress
            total += len(perms)
            for perm in perms:
                if not perm.covers(self.protocol, self.port):
                    continue
                hit, near = self.match_peer(perm, remote, counterpart, references)
                if hit:
                    self.stateful.append(gid)
                    self.add("forward", kind, gid, ALLOW, f"{self.snap.label(gid)} {direction} {perm.describe()} {'to' if egress else 'from'} {hit}")
                    return
                if near and not lost:
                    lost = f"{gid} allows {perm.describe()} {'to' if egress else 'from'} {', '.join(perm.groups)}, but {references[1]}"
        groups = ", ".join(iface.groups) or "no security groups"
        text = f"no {direction} rule of {groups} on {iface.id} allows {self.what} {'to' if egress else 'from'} {remote}"
        code = "SG_HAS_NO_RULES" if total == 0 else "ENI_SG_RULES_MISMATCH"
        if lost:
            text, code = f"{text}; {lost}", references[0]
        self.add("forward", kind, iface.groups[0] if iface.groups else iface.id, DENY, text, code)

    def route_hop(self, direction: str, table: RouteTable, dest: Address) -> Route:
        choice = lookup(self.snap, table, dest)
        if choice is None:
            self.add(direction, "route", table.id, DENY, f"{self.snap.label(table.id)} has no route that covers {dest}", "NO_ROUTE_TO_DESTINATION")
            raise Stop()
        route, matched, others = choice
        text = f"{self.snap.label(table.id)}: {dest} matches {route.describe()}"
        if route.prefix_list:
            text += f" ({matched} in the prefix list)"
        if others:
            text += f"; longest prefix /{matched.prefixlen} wins over {', '.join(others)}"
        if route.state == "blackhole":
            self.add(direction, "route", table.id, DENY, f"{text}; the route is a blackhole, its target no longer exists", "BAD_STATE_ROUTE")
        self.add(direction, "route", table.id, ALLOW, text)
        return route

    def from_interface(self, src: Party, dst: Party) -> None:
        eni = src.interface
        assert eni is not None
        table = self.snap.route_table_for(eni.subnet)
        choice = lookup(self.snap, table, dst.address)
        kind = choice[0].kind if choice else ""
        peer = dst.interface if dst.interface is not None and not dst.public else None
        references = REFERENCES_OK if kind in ("local", "peering") else (LOST_OUTBOUND if kind == "tgw" else LOST_AT_GATEWAY)
        self.sg_check(eni, True, dst.address, peer, references)
        route = self.route_hop("forward", table, dst.address)
        same_subnet = route.kind == "local" and peer is not None and peer.subnet == eni.subnet
        if same_subnet:
            self.add("forward", "nacl-out", eni.subnet, INFO, "same subnet: network ACLs are evaluated when traffic enters or leaves a subnet, not inside it")
        else:
            self.leg(eni.subnet, True, dst.address)
        if route.kind == "local":
            self.deliver(eni.vpc, dst, src.address, eni, REFERENCES_OK, ("local", "local"), eni.subnet)
        elif route.kind == "igw":
            self.internet_egress(route.target, eni.vpc, first_public(eni, src.address), src.address, dst, eni.id, eni)
        elif route.kind == "nat":
            self.nat_egress(route.target, eni, src.address, dst)
        elif route.kind == "peering":
            self.peering(route.target, eni, src.address, dst)
        elif route.kind == "tgw":
            self.transit(route.target, eni, src.address, dst)
        elif route.kind == "endpoint":
            self.endpoint(route.target, dst)
        else:
            self.add("forward", route.kind, route.target, UNKNOWN, f"traffic leaves the modelled network through {route.target} ({route.kind}); vpcpath stops here", "UNSUPPORTED_COMPONENT")

    def deliver(self, vpc_id: str, dst: Party, seen: Address, counterpart: Optional[Interface], references: Tuple[str, str], expect: Tuple[str, str], source_subnet: str) -> None:
        target = self.snap.interface_with_private_ip(dst.address, vpc_id)
        if target is None:
            self.add("forward", "deliver", vpc_id, DENY, f"the route reaches {self.snap.label(vpc_id)}, but no network interface there has {dst.address}", "NO_POSSIBLE_DESTINATION")
            raise Stop()
        if target.subnet == source_subnet:
            self.add("forward", "nacl-in", target.subnet, INFO, "same subnet: no network ACL is evaluated")
        else:
            self.leg(target.subnet, False, seen)
        self.sg_check(target, False, seen, counterpart, references)
        self.add("forward", "deliver", target.id, INFO, f"delivered to {target.label} at {dst.address}")
        self.reply_route = (target.subnet, seen, expect[0], expect[1])

    def internet_egress(self, igw_id: str, vpc_id: str, public: Optional[Address], private: Address, dst: Party, via: str, origin: Interface) -> None:
        igw = self.snap.igws.get(igw_id)
        if igw is None or vpc_id not in igw.vpcs:
            self.add("forward", "igw", igw_id, DENY, f"{igw_id} is not attached to {vpc_id}", "BAD_STATE_ATTACHMENT")
        if is_rfc1918(dst.address):
            self.add("forward", "igw", igw_id, DENY, f"{dst.address} is a private (RFC 1918) address; the route sent it to {igw_id}, and internet gateways drop private destinations", "IGW_DESTINATION_ADDRESS_NOT_IN_RFC1918_EGRESS")
        if dst.address in RFC6598:
            self.add("forward", "igw", igw_id, DENY, f"{dst.address} is in the shared range 100.64.0.0/10; {igw_id} drops it", "IGW_DESTINATION_ADDRESS_NOT_IN_RFC6598_EGRESS")
        if public is None:
            self.add("forward", "igw", igw_id, DENY, f"{via} has no public IPv4 address, so {igw_id} has nothing to translate {private} to", "IGW_PUBLIC_IP_ASSOCIATION_FOR_EGRESS")
            raise Stop()
        self.add("forward", "igw", igw_id, ALLOW, f"{igw_id} translates the source {private} to {public} (one to one, ports unchanged)")
        target = dst.interface if dst.public else None
        if target is not None:
            self.internet_ingress(target, dst, public, origin)
        else:
            self.add("forward", "deliver", "internet", INFO, f"leaves AWS for {dst.address}")

    def internet_ingress(self, target: Interface, dst: Party, seen: Address, origin: Optional[Interface] = None) -> None:
        igws = self.snap.igws_of(target.vpc)
        if not igws:
            self.add("forward", "igw", target.vpc, DENY, f"{target.vpc} has no internet gateway attached, so {dst.address} is not reachable from the internet", "NO_PATH")
        if is_rfc1918(seen):
            self.add("forward", "igw", igws[0], DENY, f"the source {seen} is a private (RFC 1918) address; {igws[0]} drops it on the way in", "IGW_SOURCE_ADDRESS_NOT_IN_RFC1918_INGRESS")
        private = target.public_ips.get(dst.address, target.private_ips[0])
        self.add("forward", "igw", igws[0], ALLOW, f"{igws[0]} translates the destination {dst.address} to {private}")
        self.leg(target.subnet, False, seen)
        self.sg_check(target, False, seen, origin, LOST_AT_GATEWAY)
        self.add("forward", "deliver", target.id, INFO, f"delivered to {target.label} at {private}")
        self.reply_route = (target.subnet, seen, "igw", igws[0])

    def from_internet(self, src: Party, dst: Party) -> None:
        if dst.interface is None:
            if is_rfc1918(dst.address):
                self.add("forward", "igw", "internet", DENY, f"{dst.address} is a private address; nothing on the internet routes to it", "IGW_PRIVATE_IP_ASSOCIATION_FOR_INGRESS")
            raise InputError(f"neither {src.address} nor {dst.address} belongs to a network interface in the snapshot")
        if not dst.public:
            reason = "has no public IPv4 address" if not dst.interface.public_ips else f"was addressed by its private IP {dst.address}"
            self.add("forward", "igw", dst.interface.vpc, DENY, f"{dst.interface.label} {reason}; an internet gateway only accepts traffic for a public IP mapped to an interface", "IGW_PRIVATE_IP_ASSOCIATION_FOR_INGRESS")
        self.internet_ingress(dst.interface, dst, src.address)

    def nat_egress(self, nat_id: str, eni: Interface, source: Address, dst: Party) -> None:
        nat = self.snap.nats.get(nat_id)
        if nat is None:
            raise InputError(f"NAT gateway {nat_id} is not in the snapshot; export `aws ec2 describe-nat-gateways`")
        if nat.state != "available":
            self.add("forward", "nat", nat_id, DENY, f"{nat_id} is {nat.state or 'in an unknown state'}, not available", "BAD_STATE")
        if nat.connectivity != "public":
            self.add("forward", "nat", nat_id, UNKNOWN, f"{nat_id} is a {nat.connectivity} NAT gateway; it forwards through a transit gateway or virtual private gateway, which vpcpath does not follow", "UNSUPPORTED_COMPONENT")
        if nat.subnet != eni.subnet:
            self.leg(nat.subnet, False, source)
        self.source_ports = NAT_PORTS
        translated = nat.private_ip or source
        self.add("forward", "nat", nat_id, ALLOW, f"{nat_id} translates the source {source} to {translated} and picks a source port in {show_ports(NAT_PORTS)}")
        table = self.snap.route_table_for(nat.subnet)
        route = self.route_hop("forward", table, dst.address)
        if route.kind != "igw":
            self.add("forward", "route", table.id, DENY, f"a public NAT gateway reaches the internet through an internet gateway route in its own subnet; {table.id} sends {dst.address} to {route.target}", "NO_ROUTE_TO_DESTINATION")
        self.leg(nat.subnet, True, dst.address)
        self.internet_egress(route.target, nat.vpc, nat.public_ip, translated, dst, nat_id, eni)

    def peering(self, pcx_id: str, eni: Interface, source: Address, dst: Party) -> None:
        pcx = self.snap.peerings.get(pcx_id)
        if pcx is None:
            raise InputError(f"peering connection {pcx_id} is not in the snapshot; export `aws ec2 describe-vpc-peering-connections`")
        if pcx.status != "active":
            self.add("forward", "peering", pcx_id, DENY, f"{pcx_id} is {pcx.status or 'in an unknown state'}, not active", "BAD_STATE")
        if eni.vpc == pcx.requester:
            other, cidrs = pcx.accepter, self.snap.vpc_cidrs(pcx.accepter) or pcx.accepter_cidrs
        elif eni.vpc == pcx.accepter:
            other, cidrs = pcx.requester, self.snap.vpc_cidrs(pcx.requester) or pcx.requester_cidrs
        else:
            self.add("forward", "peering", pcx_id, DENY, f"{pcx_id} does not connect {eni.vpc}", "BAD_STATE_ATTACHMENT")
            raise Stop()
        if not any(dst.address in c for c in cidrs):
            self.add("forward", "peering", pcx_id, DENY, f"{dst.address} is not in the peer VPC {other} ({', '.join(map(str, cidrs))}); peering is not transitive and never forwards to the peer's gateways", "PCX_REQUIRES_ADDRESS_IN_VPC_CIDR")
        self.add("forward", "peering", pcx_id, ALLOW, f"{pcx_id} carries {source} -> {dst.address} from {eni.vpc} into {other}, addresses unchanged")
        self.deliver(other, dst, source, eni, REFERENCES_OK, ("peering", pcx_id), "")

    def transit(self, tgw_id: str, eni: Interface, source: Address, dst: Party) -> None:
        live = [a for a in self.snap.tgw_attachments if a.tgw == tgw_id and a.state == "available"]
        if not any(a.vpc == eni.vpc for a in live):
            self.add("forward", "tgw", tgw_id, DENY, f"{eni.vpc} has no available attachment to {tgw_id}", "BAD_STATE_ATTACHMENT")
        reachable = [(n.prefixlen, a) for a in live if a.vpc != eni.vpc for n in self.snap.vpc_cidrs(a.vpc) if dst.address in n]
        if not reachable:
            self.add("forward", "tgw", tgw_id, DENY, f"no attachment of {tgw_id} leads to a VPC that contains {dst.address} (vpcpath assumes every VPC attachment propagates its CIDRs into one transit gateway route table)", "NO_ROUTE_TO_DESTINATION")
        attachment = max(reachable, key=lambda r: r[0])[1]
        self.add("forward", "tgw", tgw_id, ALLOW, f"{tgw_id} forwards {dst.address} to {attachment.id} ({attachment.vpc})")
        references = REFERENCES_OK if self.snap.tgw_sg_referencing.get(tgw_id) else LOST_AT_TGW
        self.deliver(attachment.vpc, dst, source, eni, references, ("tgw", tgw_id), "")

    def endpoint(self, vpce_id: str, dst: Party) -> None:
        endpoint = self.snap.endpoints.get(vpce_id)
        service = endpoint.service if endpoint else vpce_id
        if self.protocol not in (6, 1):
            self.add("forward", "endpoint", vpce_id, DENY, f"{vpce_id} is a gateway endpoint, which carries only TCP and ICMP echo; this is {self.what}", "VPCE_GATEWAY_PROTOCOL_RESTRICTION")
        self.add("forward", "endpoint", vpce_id, ALLOW, f"gateway endpoint {vpce_id} to {service}; its endpoint policy (IAM) is evaluated by the service, not here")
        self.add("forward", "deliver", service, INFO, f"{dst.address} at {service}, without a public IP or NAT")

    def return_path(self) -> None:
        if self.stateful:
            self.add("return", "stateful", ", ".join(dict.fromkeys(self.stateful)), INFO, "security groups track the connection: the reply passes the groups that allowed the request, whatever their rules in the other direction")
        if self.reply_route is not None:
            subnet_id, seen, kind, target = self.reply_route
            table = self.snap.route_table_for(subnet_id)
            route = self.route_hop("return", table, seen)
            subnet = self.snap.subnet(subnet_id)
            ok = route.kind == "local" if kind == "local" else (route.target in self.snap.igws_of(subnet.vpc) if kind == "igw" else route.target == target)
            if not ok:
                came = "the VPC router" if kind == "local" else target
                text = f"{self.snap.label(table.id)}: the reply to {seen} leaves through {route.target}, but the request arrived through {came}"
                if route.kind == "nat":
                    text += "; the NAT gateway rewrites the source, so the client never matches the reply to its connection"
                self.hops[-1] = Hop("return", "route", table.id, DENY, text, "VPCPATH_ASYMMETRIC_RETURN")
                raise Stop()
        for leg in reversed(self.legs):
            acl = self.snap.acl_for(leg.subnet)
            segments = acl_decide(acl, not leg.outbound, self.protocol, leg.reply_ports, leg.remote)
            allowed = [s for s in segments if s[2].allow]
            denied = [s for s in segments if not s[2].allow]
            kind = "nacl-in" if leg.outbound else "nacl-out"
            toward = "from" if leg.outbound else "to"
            what = "icmp echo reply" if self.protocol == 1 else f"{PROTOCOL_NAMES[self.protocol]} ports {show_ports(leg.reply_ports)}"
            head = f"{self.snap.label(acl.id)} on {leg.subnet}, reply {what} {toward} {leg.remote}: "
            if not denied:
                self.add("return", kind, acl.id, ALLOW, head + "; ".join(f"{show_ports(s[:2])} by {s[2].describe()}" for s in allowed))
            elif not allowed:
                self.add("return", kind, acl.id, DENY, head + "; ".join(f"{show_ports(s[:2])} by {s[2].describe()}" for s in denied) + " (network ACLs are stateless: the reply needs its own rule)", "SUBNET_ACL_RESTRICTION")
            else:
                lost = sum(s[1] - s[0] + 1 for s in denied)
                share = 100.0 * lost / (leg.reply_ports[1] - leg.reply_ports[0] + 1)
                parts = [f"{show_ports(s[:2])} {'allowed' if s[2].allow else 'denied'} by {s[2].describe()}" for s in segments]
                self.add("return", kind, acl.id, PARTIAL, head + "; ".join(parts) + f"; about {share:.0f}% of connections fail, depending on the source port", "SUBNET_ACL_RESTRICTION")


def trace(snap: Snapshot, source: str, destination: str, protocol: str = "tcp", port: int = 0, ephemeral: Optional[Ports] = None) -> Answer:
    number = PROTOCOL_NUMBERS.get(protocol)
    if number is None:
        raise InputError(f"protocol must be tcp, udp or icmp, not {protocol!r}")
    if number != 1 and not 0 < port <= 65535:
        raise InputError(f"{protocol} needs a destination port between 1 and 65535")
    src = resolve(snap, source)
    dst = resolve(snap, destination, from_internet=src.interface is None)
    if src.interface is not None and src.public:
        src = Party(src.label, src.interface.public_ips[src.address], src.interface)
    client = ephemeral or (LINUX_EPHEMERAL if src.interface is not None else ANY_CLIENT)
    tracer = Tracer(snap, number, port, client)
    try:
        if src.interface is not None:
            tracer.from_interface(src, dst)
        else:
            tracer.from_internet(src, dst)
        tracer.return_path()
    except Stop:
        pass
    hops = tracer.hops
    stop = next((h for h in hops if h.decision in (DENY, UNKNOWN)), None)
    partial = [h for h in hops if h.decision == PARTIAL]
    if stop is not None:
        verdict = "blocked" if stop.decision == DENY else "unknown"
        reason, code = f"{stop.direction} {stop.kind}: {stop.rule}", stop.code
    elif partial:
        verdict = "partial"
        reason, code = " | ".join(f"return {h.kind}: {h.rule}" for h in partial), partial[0].code
    else:
        verdict = "reachable"
        reason, code = "every hop allows it, the request and the reply", ""
    return Answer(src, dst, number, tracer.port, client, hops, verdict, reason, code, list(dict.fromkeys(snap.warnings)))


def answer_json(answer: Answer) -> Dict[str, Any]:
    stop = answer.blocked_at
    return {
        "from": answer.source.to_json(),
        "to": answer.destination.to_json(),
        "protocol": PROTOCOL_NAMES[answer.protocol],
        "port": None if answer.protocol == 1 else answer.port,
        "client_ports": show_ports(answer.client_ports),
        "verdict": answer.verdict,
        "reachable": answer.verdict == "reachable",
        "reason": answer.reason,
        "explanation_code": answer.code or None,
        "blocked_at": stop.to_json() if stop else None,
        "forward": [h.to_json() for h in answer.hops if h.direction == "forward"],
        "return": [h.to_json() for h in answer.hops if h.direction == "return"],
        "warnings": answer.warnings,
    }


def render(answer: Answer, snap: Snapshot) -> str:
    what = "icmp echo" if answer.protocol == 1 else f"{PROTOCOL_NAMES[answer.protocol]}/{answer.port}"
    lines = [f"{what} from {answer.source.describe(snap)} to {answer.destination.describe(snap)}, client ports {show_ports(answer.client_ports)}"]
    for direction in ("forward", "return"):
        hops = [h for h in answer.hops if h.direction == direction]
        if hops:
            lines.append(direction)
            lines.extend(f"  {h.decision:<8} {h.kind:<11} {h.rule}" + (f" [{h.code}]" if h.code else "") for h in hops)
    lines.append(f"{answer.verdict.upper()}: {answer.reason}" + (f" [{answer.code}]" if answer.code else ""))
    lines.extend(f"warning: {w}" for w in answer.warnings)
    return "\n".join(lines)


def read_questions(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}")
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not JSON: {exc.msg} at line {exc.lineno}")
    questions = doc.get("questions") if isinstance(doc, dict) else doc
    if not isinstance(questions, list) or not questions:
        raise InputError(f"{path}: expected a list of questions, or an object with a questions list")
    for i, q in enumerate(questions):
        if not isinstance(q, dict) or not q.get("from") or not q.get("to"):
            raise InputError(f"{path}: question {i + 1} needs from and to")
        if q.get("expect") and q["expect"] not in VERDICTS:
            raise InputError(f"{path}: question {i + 1} expects {q['expect']!r}; use one of {', '.join(VERDICTS)}")
    return questions


def run_matrix(snap: Snapshot, questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for i, q in enumerate(questions):
        ephemeral = parse_ports(q["ephemeral"]) if q.get("ephemeral") else None
        answer = trace(snap, str(q["from"]), str(q["to"]), str(q.get("protocol") or "tcp"), int(q.get("port") or 0), ephemeral)
        expect, want_code = q.get("expect"), q.get("code")
        ok = (not expect or answer.verdict == expect) and (not want_code or answer.code == want_code)
        rows.append({"name": q.get("name") or f"q{i + 1}", "ok": ok, "expect": expect, "expect_code": want_code, **answer_json(answer)})
    return rows


def render_matrix(rows: List[Dict[str, Any]]) -> str:
    width = max(len(r["name"]) for r in rows)
    lines = []
    for r in rows:
        what = r["protocol"] if r["port"] is None else f"{r['protocol']}/{r['port']}"
        flag = "ok  " if r["ok"] else "FAIL"
        got = r["verdict"] + (f" {r['explanation_code']}" if r["explanation_code"] else "")
        line = f"{flag} {r['name']:<{width}}  {r['from']['label']} -> {r['to']['label']} {what}: {got}"
        if not r["ok"]:
            line += f" (expected {r['expect']}{' ' + r['expect_code'] if r['expect_code'] else ''}): {r['reason']}"
        lines.append(line)
    passed = sum(1 for r in rows if r["ok"])
    lines.append(f"{passed}/{len(rows)} as expected")
    return "\n".join(lines)


def subnet_rows(snap: Snapshot) -> List[Dict[str, Any]]:
    rows = []
    for subnet in sorted(snap.subnets.values(), key=lambda s: (s.vpc, s.cidr)):
        table = snap.route_table_for(subnet.id)
        acl = snap.acl_for(subnet.id)
        default = next((r.target for r in table.routes if r.destination == EVERYWHERE), None)
        public = any(r.kind == "igw" and r.target in snap.igws_of(subnet.vpc) for r in table.routes)
        rows.append(
            {
                "subnet": subnet.id,
                "name": subnet.name,
                "vpc": subnet.vpc,
                "cidr": str(subnet.cidr),
                "zone": subnet.zone,
                "route_table": table.id,
                "route_table_association": "explicit" if subnet.id in table.subnets else "main",
                "default_route": default,
                "network_acl": acl.id,
                "network_acl_association": "explicit" if subnet.id in acl.subnets else "default",
                "public": public,
            }
        )
    return rows


def render_subnets(rows: List[Dict[str, Any]]) -> str:
    header = ("subnet", "cidr", "zone", "route table", "0.0.0.0/0", "network acl", "public")
    table = [header] + [
        (
            r["subnet"] + (f" ({r['name']})" if r["name"] else ""),
            r["cidr"],
            r["zone"],
            f"{r['route_table']} ({r['route_table_association']})",
            r["default_route"] or "none",
            f"{r['network_acl']} ({r['network_acl_association']})",
            "yes" if r["public"] else "no",
        )
        for r in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in table)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="vpcpath", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="can FROM reach TO? hop by hop, forward and return")
    check.add_argument("source", metavar="FROM", help="instance id, ENI id, Name tag, IP address, or internet")
    check.add_argument("destination", metavar="TO", help="instance id, ENI id, Name tag, IP address (private or public), prefix list id or name, or internet")
    check.add_argument("--port", type=int, default=0, help="destination port (tcp and udp)")
    check.add_argument("--protocol", choices=sorted(PROTOCOL_NUMBERS), default="tcp")
    check.add_argument("--ephemeral", help="the client's source port range, LOW-HIGH (default 32768-60999 for an instance, 1024-65535 for the internet)")
    matrix = sub.add_parser("matrix", help="answer every question in a JSON file and compare with the expected verdicts")
    matrix.add_argument("--questions", required=True, help='JSON: [{"name", "from", "to", "protocol", "port", "expect", "code"}, ...]')
    subnets = sub.add_parser("subnets", help="every subnet with its route table, default route, network ACL and whether it is public")
    for command in (check, matrix, subnets):
        command.add_argument("--snapshot", action="append", required=True, help="a directory of `aws ec2 describe-*` JSON files, a single file, or - (repeatable)")
        command.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        snap = load_snapshot(args.snapshot)
        if args.command == "check":
            answer = trace(snap, args.source, args.destination, args.protocol, args.port, parse_ports(args.ephemeral) if args.ephemeral else None)
            print(json.dumps(answer_json(answer), indent=2) if args.json else render(answer, snap))
            return 0 if answer.verdict == "reachable" else 1
        if args.command == "matrix":
            rows = run_matrix(snap, read_questions(args.questions))
            print(json.dumps(rows, indent=2) if args.json else render_matrix(rows))
            return 0 if all(r["ok"] for r in rows) else 1
        rows = subnet_rows(snap)
        print(json.dumps(rows, indent=2) if args.json else render_subnets(rows))
        return 0
    except InputError as exc:
        print(f"vpcpath: {exc}", file=sys.stderr)
        return 2



if __name__ == "__main__":
    raise SystemExit(main())

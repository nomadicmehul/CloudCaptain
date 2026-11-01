#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

MOTO_VERSION="5.2.5"
REGION="eu-central-1"
F="code/fixtures"
WORK="$(mktemp -d)"
PIDS=()
fail() {
  echo "SMOKE FAIL: $*" >&2
  if [[ -f "$WORK/moto.log" ]]; then echo "--- moto_server log (last 20 lines)" >&2; tail -20 "$WORK/moto.log" >&2; fi
  exit 1
}
cleanup() {
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

vp() {
  local code=0
  python3 code/main.py "$@" || code=$?
  return "$code"
}
check_json() {
  local out="$1"
  shift
  local code=0
  python3 code/main.py check "$@" --json > "$out" || code=$?
  echo "$code"
}
field() {
  python3 - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else ("" if value is None else value))
PY
}

echo "▸ vpcpath on the fixture network: 17 questions whose answers follow from AWS's documented rules"
vp matrix --snapshot "$F/shop" --questions "$F/questions.json" > "$WORK/fixture-matrix.txt" || fail "fixture matrix: $(cat "$WORK/fixture-matrix.txt")"
cat "$WORK/fixture-matrix.txt"
grep -q "^17/17 as expected$" "$WORK/fixture-matrix.txt" || fail "expected 17/17: $(tail -3 "$WORK/fixture-matrix.txt")"

echo "▸ the NAT gateway reply: the public subnet's ACL admits only part of the NAT's port range"
CODE="$(check_json "$WORK/nat.json" batch internet --port 443 --snapshot "$F/shop")"
[[ "$CODE" == "1" && "$(field "$WORK/nat.json" verdict)" == "partial" ]] || fail "batch -> internet should be partial (exit $CODE): $(cat "$WORK/nat.json")"
python3 - "$WORK/nat.json" <<'PY' || fail "partial hop: $(cat "$WORK/nat.json")"
import json, sys
doc = json.load(open(sys.argv[1]))
hop = next(h for h in doc["return"] if h["decision"] == "partial")
assert hop["component"] == "acl-0public" and hop["kind"] == "nacl-in", hop
assert "1024-32767 denied by rule *" in hop["rule"] and "32768-65535 allowed by rule 120" in hop["rule"], hop["rule"]
assert [h["kind"] for h in doc["forward"]] == ["sg-egress", "route", "nacl-out", "nacl-in", "nat", "route", "nacl-out", "igw", "deliver"], doc["forward"]
print("  " + hop["rule"])
PY

echo "▸ the stateless return path: etl -> db is allowed in and dropped on the way back"
CODE="$(check_json "$WORK/etl.json" etl db --port 5432 --snapshot "$F/shop")"
[[ "$CODE" == "1" && "$(field "$WORK/etl.json" blocked_at direction)" == "return" && "$(field "$WORK/etl.json" blocked_at component)" == "acl-0private" ]] || fail "etl -> db should be blocked on the return path: $(cat "$WORK/etl.json")"
echo "  $(field "$WORK/etl.json" reason)"

echo "▸ bad input exits 2 with the reason"
echo '{"Subnets": "nope"}' > "$WORK/bad.json"
CODE=0
python3 code/main.py check web db --port 5432 --snapshot "$WORK/bad.json" > /dev/null 2> "$WORK/bad.err" || CODE=$?
[[ "$CODE" == "2" ]] && grep -q "no Subnets" "$WORK/bad.err" || fail "a snapshot without subnets should exit 2: code $CODE, $(cat "$WORK/bad.err")"

echo "▸ moto $MOTO_VERSION in server mode: the EC2 API without an AWS account"
python3 -m venv "$WORK/venv" || fail "python3 -m venv"
"$WORK/venv/bin/pip" install -q "moto[server]==$MOTO_VERSION" > "$WORK/pip.log" 2>&1 || fail "pip install moto: $(tail -20 "$WORK/pip.log")"
if command -v aws >/dev/null; then
  AWS="aws"
else
  echo "  (aws CLI not installed; installing awscli into the venv)"
  "$WORK/venv/bin/pip" install -q awscli > "$WORK/pip-cli.log" 2>&1 || fail "pip install awscli: $(tail -20 "$WORK/pip-cli.log")"
  AWS="$WORK/venv/bin/aws"
fi
"$AWS" --version > "$WORK/aws-version.txt" 2>&1 || true
echo "  $(cat "$WORK/aws-version.txt")"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
"$WORK/venv/bin/moto_server" -H 127.0.0.1 -p "$PORT" > "$WORK/moto.log" 2>&1 &
PIDS+=("$!")
EP="http://127.0.0.1:$PORT"
for _ in $(seq 1 100); do curl -s -o /dev/null "$EP/moto-api/" && break; sleep 0.2; done
curl -s -o /dev/null "$EP/moto-api/" || fail "moto_server did not start on $PORT"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION="$REGION" AWS_PAGER="" AWS_EC2_METADATA_DISABLED=true
ec2() { "$AWS" --endpoint-url "$EP" --output json ec2 "$@"; }
get() { "$AWS" --endpoint-url "$EP" --output text ec2 "$@"; }
tag() { echo "ResourceType=$1,Tags=[{Key=Name,Value=$2}]"; }

echo "▸ build the shop VPC with the AWS CLI: public and private subnets, IGW, NAT gateway, route tables"
VPC="$(get create-vpc --cidr-block 10.0.0.0/16 --tag-specifications "$(tag vpc shop)" --query Vpc.VpcId)"
PUB="$(get create-subnet --vpc-id "$VPC" --cidr-block 10.0.1.0/24 --availability-zone "${REGION}a" --tag-specifications "$(tag subnet public-a)" --query Subnet.SubnetId)"
PRIV="$(get create-subnet --vpc-id "$VPC" --cidr-block 10.0.2.0/24 --availability-zone "${REGION}a" --tag-specifications "$(tag subnet private-a)" --query Subnet.SubnetId)"
IGW="$(get create-internet-gateway --query InternetGateway.InternetGatewayId)"
ec2 attach-internet-gateway --internet-gateway-id "$IGW" --vpc-id "$VPC" > /dev/null
RT_PUB="$(get create-route-table --vpc-id "$VPC" --tag-specifications "$(tag route-table public)" --query RouteTable.RouteTableId)"
ec2 create-route --route-table-id "$RT_PUB" --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW" > /dev/null
ec2 associate-route-table --route-table-id "$RT_PUB" --subnet-id "$PUB" > /dev/null
NAT_EIP="$(get allocate-address --domain vpc --query AllocationId)"
NAT="$(get create-nat-gateway --subnet-id "$PUB" --allocation-id "$NAT_EIP" --query NatGateway.NatGatewayId)"
[[ "$(get describe-nat-gateways --nat-gateway-ids "$NAT" --query 'NatGateways[0].State')" == "available" ]] || fail "NAT gateway $NAT is not available"
RT_PRIV="$(get create-route-table --vpc-id "$VPC" --tag-specifications "$(tag route-table private)" --query RouteTable.RouteTableId)"
ec2 create-route --route-table-id "$RT_PRIV" --destination-cidr-block 0.0.0.0/0 --nat-gateway-id "$NAT" > /dev/null
ec2 associate-route-table --route-table-id "$RT_PRIV" --subnet-id "$PRIV" > /dev/null
ec2 create-vpc-endpoint --vpc-id "$VPC" --service-name "com.amazonaws.$REGION.s3" --route-table-ids "$RT_PRIV" > /dev/null

echo "▸ a peered analytics VPC and a legacy VPC behind a transit gateway"
VPC2="$(get create-vpc --cidr-block 10.1.0.0/16 --tag-specifications "$(tag vpc analytics)" --query Vpc.VpcId)"
SUB2="$(get create-subnet --vpc-id "$VPC2" --cidr-block 10.1.1.0/24 --availability-zone "${REGION}b" --query Subnet.SubnetId)"
PCX="$(get create-vpc-peering-connection --vpc-id "$VPC" --peer-vpc-id "$VPC2" --query VpcPeeringConnection.VpcPeeringConnectionId)"
ec2 accept-vpc-peering-connection --vpc-peering-connection-id "$PCX" > /dev/null
ec2 create-route --route-table-id "$RT_PRIV" --destination-cidr-block 10.1.0.0/16 --vpc-peering-connection-id "$PCX" > /dev/null
RT2="$(get describe-route-tables --filters "Name=vpc-id,Values=$VPC2" --query 'RouteTables[?Associations[?Main]].RouteTableId | [0]')"
ec2 create-route --route-table-id "$RT2" --destination-cidr-block 10.0.2.0/24 --vpc-peering-connection-id "$PCX" > /dev/null
VPC3="$(get create-vpc --cidr-block 172.16.0.0/16 --tag-specifications "$(tag vpc legacy)" --query Vpc.VpcId)"
SUB3="$(get create-subnet --vpc-id "$VPC3" --cidr-block 172.16.1.0/24 --availability-zone "${REGION}a" --query Subnet.SubnetId)"
TGW="$(get create-transit-gateway --description core --options SecurityGroupReferencingSupport=disable --query TransitGateway.TransitGatewayId)"
ec2 create-transit-gateway-vpc-attachment --transit-gateway-id "$TGW" --vpc-id "$VPC" --subnet-ids "$PRIV" > /dev/null
ec2 create-transit-gateway-vpc-attachment --transit-gateway-id "$TGW" --vpc-id "$VPC3" --subnet-ids "$SUB3" > /dev/null
ec2 create-route --route-table-id "$RT_PRIV" --destination-cidr-block 172.16.0.0/12 --transit-gateway-id "$TGW" > /dev/null
RT3="$(get describe-route-tables --filters "Name=vpc-id,Values=$VPC3" --query 'RouteTables[?Associations[?Main]].RouteTableId | [0]')"
ec2 create-route --route-table-id "$RT3" --destination-cidr-block 10.0.0.0/8 --transit-gateway-id "$TGW" > /dev/null

echo "▸ security groups: allow-only, stateful, one of them referencing another group"
sg() { get create-security-group --group-name "$1" --description "$1" --vpc-id "$2" --query GroupId; }
SG_WEB="$(sg web "$VPC")"; SG_DB="$(sg db "$VPC")"; SG_BATCH="$(sg batch "$VPC")"; SG_REPORT="$(sg report "$VPC")"
SG_ETL="$(sg etl "$VPC2")"; SG_LDAP="$(sg ldap "$VPC3")"
ec2 authorize-security-group-ingress --group-id "$SG_WEB" --protocol tcp --port 443 --cidr 0.0.0.0/0 > /dev/null
ec2 authorize-security-group-ingress --group-id "$SG_WEB" --protocol tcp --port 22 --cidr 192.0.2.0/24 > /dev/null
ec2 authorize-security-group-ingress --group-id "$SG_DB" --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=$SG_WEB}]" "IpProtocol=tcp,FromPort=5432,ToPort=5432,IpRanges=[{CidrIp=10.1.0.0/16}]" > /dev/null
ec2 revoke-security-group-egress --group-id "$SG_DB" --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]' > /dev/null
ec2 authorize-security-group-egress --group-id "$SG_DB" --ip-permissions 'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]' > /dev/null
ec2 authorize-security-group-ingress --group-id "$SG_REPORT" --protocol tcp --port 443 --cidr 0.0.0.0/0 > /dev/null
ec2 authorize-security-group-ingress --group-id "$SG_LDAP" --ip-permissions "IpProtocol=tcp,FromPort=636,ToPort=636,UserIdGroupPairs=[{GroupId=$SG_BATCH}]" "IpProtocol=tcp,FromPort=389,ToPort=389,IpRanges=[{CidrIp=10.0.2.0/24}]" > /dev/null

echo "▸ network ACLs: stateless, numbered, with a copied 32768-65535 return range on the NAT subnet"
nacl_entry() { ec2 create-network-acl-entry --network-acl-id "$1" "$2" --rule-number "$3" --protocol "$4" --cidr-block "$5" --rule-action "$6" ${7:+--port-range "$7"} > /dev/null; }
ACL_PUB="$(get create-network-acl --vpc-id "$VPC" --tag-specifications "$(tag network-acl public)" --query NetworkAcl.NetworkAclId)"
nacl_entry "$ACL_PUB" --ingress 100 tcp 0.0.0.0/0 allow From=443,To=443
nacl_entry "$ACL_PUB" --ingress 110 tcp 192.0.2.0/24 allow From=22,To=22
nacl_entry "$ACL_PUB" --ingress 120 tcp 0.0.0.0/0 allow From=32768,To=65535
nacl_entry "$ACL_PUB" --ingress 130 -1 10.0.0.0/16 allow
nacl_entry "$ACL_PUB" --egress 100 -1 0.0.0.0/0 allow
ACL_PRIV="$(get create-network-acl --vpc-id "$VPC" --tag-specifications "$(tag network-acl private)" --query NetworkAcl.NetworkAclId)"
nacl_entry "$ACL_PRIV" --ingress 100 tcp 10.0.1.0/24 allow From=5432,To=5432
nacl_entry "$ACL_PRIV" --ingress 105 tcp 10.1.0.0/16 allow From=5432,To=5432
nacl_entry "$ACL_PRIV" --ingress 110 tcp 0.0.0.0/0 allow From=1024,To=65535
nacl_entry "$ACL_PRIV" --ingress 115 tcp 0.0.0.0/0 allow From=443,To=443
nacl_entry "$ACL_PRIV" --egress 100 tcp 0.0.0.0/0 allow From=443,To=443
nacl_entry "$ACL_PRIV" --egress 110 tcp 10.0.1.0/24 allow From=1024,To=65535
nacl_entry "$ACL_PRIV" --egress 120 tcp 172.16.0.0/12 allow From=389,To=389
nacl_entry "$ACL_PRIV" --egress 125 tcp 172.16.0.0/12 allow From=636,To=636
for pair in "$PUB:$ACL_PUB" "$PRIV:$ACL_PRIV"; do
  SUBNET="${pair%%:*}"
  ASSOC="$(get describe-network-acls --filters "Name=association.subnet-id,Values=$SUBNET" --query "NetworkAcls[0].Associations[?SubnetId=='$SUBNET'].NetworkAclAssociationId | [0]")"
  ec2 replace-network-acl-association --association-id "$ASSOC" --network-acl-id "${pair##*:}" > /dev/null
done

echo "▸ instances with the fixture's private IPs, one public IP and one Elastic IP"
AMI="$(get describe-images --owners amazon --query 'Images[0].ImageId')"
run() { get run-instances --image-id "$AMI" --instance-type t3.micro --tag-specifications "$(tag instance "$1")" "${@:2}" --query 'Instances[0].InstanceId'; }
run web --network-interfaces "DeviceIndex=0,SubnetId=$PUB,Groups=$SG_WEB,PrivateIpAddress=10.0.1.10,AssociatePublicIpAddress=true" > /dev/null
run worker --subnet-id "$PUB" --security-group-ids "$SG_BATCH" --private-ip-address 10.0.1.30 > /dev/null
run db --subnet-id "$PRIV" --security-group-ids "$SG_DB" --private-ip-address 10.0.2.20 > /dev/null
run batch --subnet-id "$PRIV" --security-group-ids "$SG_BATCH" --private-ip-address 10.0.2.30 > /dev/null
REPORT="$(run report --subnet-id "$PRIV" --security-group-ids "$SG_REPORT" --private-ip-address 10.0.2.40)"
run etl --subnet-id "$SUB2" --security-group-ids "$SG_ETL" --private-ip-address 10.1.1.40 > /dev/null
run ldap --subnet-id "$SUB3" --security-group-ids "$SG_LDAP" --private-ip-address 172.16.1.5 > /dev/null
REPORT_EIP="$(get allocate-address --domain vpc --query AllocationId)"
ec2 associate-address --allocation-id "$REPORT_EIP" --instance-id "$REPORT" > /dev/null

export_snapshot() {
  local dir="$1"
  mkdir -p "$dir"
  for call in describe-vpcs describe-subnets describe-route-tables describe-security-groups describe-network-acls describe-network-interfaces describe-instances \
    describe-internet-gateways describe-nat-gateways describe-vpc-peering-connections describe-transit-gateways describe-transit-gateway-attachments describe-vpc-endpoints describe-prefix-lists; do
    ec2 "$call" > "$dir/$call.json" 2> "$WORK/export.err" || fail "aws ec2 $call: $(cat "$WORK/export.err")"
  done
  python3 - "$dir/describe-prefix-lists.json" > "$WORK/pl-ids.txt" <<'PY'
import json, sys
for pl in json.load(open(sys.argv[1]))["PrefixLists"]:
    if not pl.get("Cidrs"):
        print(pl["PrefixListId"])
PY
  : > "$WORK/pl-entries.jsonl"
  while IFS= read -r pl; do
    [[ -n "$pl" ]] || continue
    ec2 get-managed-prefix-list-entries --prefix-list-id "$pl" > "$WORK/pl.json" 2>/dev/null || continue
    python3 -c 'import json,sys; print(json.dumps({"id": sys.argv[1], "cidrs": [e["Cidr"] for e in json.load(open(sys.argv[2]))["Entries"]]}))' "$pl" "$WORK/pl.json" >> "$WORK/pl-entries.jsonl"
  done < "$WORK/pl-ids.txt"
  python3 - "$dir/describe-prefix-lists.json" "$WORK/pl-entries.jsonl" <<'PY'
import json, sys
path, extra = sys.argv[1], sys.argv[2]
doc = json.load(open(path))
cidrs = {}
for line in open(extra):
    row = json.loads(line)
    cidrs[row["id"]] = row["cidrs"]
for pl in doc["PrefixLists"]:
    pl.setdefault("Cidrs", cidrs.get(pl["PrefixListId"], []))
    if not pl["Cidrs"]:
        pl["Cidrs"] = cidrs.get(pl["PrefixListId"], [])
json.dump(doc, open(path, "w"), indent=2)
PY
}

echo "▸ export the describe-* outputs, then ask vpcpath the same 17 questions about the emulated network"
export_snapshot "$WORK/moto"
ls "$WORK/moto" | sed 's/^/  /'
CODE=0
vp matrix --snapshot "$WORK/moto" --questions "$F/questions.json" > "$WORK/moto-matrix.txt" || CODE=$?
cat "$WORK/moto-matrix.txt"
[[ "$CODE" == "0" ]] && grep -q "^17/17 as expected$" "$WORK/moto-matrix.txt" || fail "the moto export should answer like the fixture (exit $CODE)"

echo "▸ Use It: the describe calls you read by hand, against vpcpath's hops"
ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$PRIV" --query 'RouteTables[0].Routes[]' > "$WORK/rt-priv.json"
python3 - "$WORK/rt-priv.json" "$NAT" "$PCX" "$TGW" <<'PY' || fail "private route table: $(cat "$WORK/rt-priv.json")"
import json, sys
routes, nat, pcx, tgw = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3], sys.argv[4]
targets = {r.get("DestinationCidrBlock") or r.get("DestinationPrefixListId"): r.get("NatGatewayId") or r.get("VpcPeeringConnectionId") or r.get("TransitGatewayId") or r.get("GatewayId") for r in routes}
assert targets["10.0.0.0/16"] == "local" and targets["0.0.0.0/0"] == nat, targets
assert targets["10.1.0.0/16"] == pcx and targets["172.16.0.0/12"] == tgw, targets
assert any(k.startswith("pl-") and v.startswith("vpce-") for k, v in targets.items()), targets
for k, v in targets.items():
    print(f"  {k:<18} -> {v}")
PY
ec2 describe-network-acls --filters "Name=association.subnet-id,Values=$PUB" --query 'NetworkAcls[0].Entries' > "$WORK/acl-pub.json"
python3 - "$WORK/acl-pub.json" <<'PY' || fail "public ACL entries: $(cat "$WORK/acl-pub.json")"
import json, sys
entries = json.load(open(sys.argv[1]))
inbound = sorted((e for e in entries if not e["Egress"]), key=lambda e: e["RuleNumber"])
rule = next(e for e in inbound if e["RuleNumber"] == 120)
assert rule["PortRange"] == {"From": 32768, "To": 65535} and rule["RuleAction"] == "allow", rule
print("  inbound rules in evaluation order:", [e["RuleNumber"] for e in inbound])
PY
[[ "$(get describe-security-groups --group-ids "$SG_DB" --query 'SecurityGroups[0].IpPermissions[?FromPort==`5432`].UserIdGroupPairs[].GroupId | [0]')" == "$SG_WEB" ]] || fail "sg db should reference sg web"
vp subnets --snapshot "$WORK/moto" --json > "$WORK/subnets.json" || fail "vpcpath subnets: $(cat "$WORK/subnets.json")"
python3 - "$WORK/subnets.json" "$PUB" "$PRIV" "$IGW" "$NAT" <<'PY' || fail "subnets: $(cat "$WORK/subnets.json")"
import json, sys
rows = {r["subnet"]: r for r in json.load(open(sys.argv[1]))}
pub, priv, igw, nat = sys.argv[2:6]
assert rows[pub]["public"] and rows[pub]["default_route"] == igw, rows[pub]
assert not rows[priv]["public"] and rows[priv]["default_route"] == nat, rows[priv]
print(f"  {pub}: public via {igw}; {priv}: private, 0.0.0.0/0 -> {nat}")
PY

echo "▸ fix the two ACLs with the CLI and ask again: NAT replies get 1024-65535, peered replies get an outbound rule"
ec2 replace-network-acl-entry --network-acl-id "$ACL_PUB" --ingress --rule-number 120 --protocol tcp --port-range From=1024,To=65535 --cidr-block 0.0.0.0/0 --rule-action allow > /dev/null
nacl_entry "$ACL_PRIV" --egress 115 tcp 10.1.0.0/16 allow From=1024,To=65535
export_snapshot "$WORK/moto-fixed"
CODE="$(check_json "$WORK/fixed-nat.json" batch internet --port 443 --snapshot "$WORK/moto-fixed")"
[[ "$CODE" == "0" && "$(field "$WORK/fixed-nat.json" verdict)" == "reachable" ]] || fail "batch -> internet should be reachable after the fix: $(cat "$WORK/fixed-nat.json")"
CODE="$(check_json "$WORK/fixed-etl.json" etl db --port 5432 --snapshot "$WORK/moto-fixed")"
[[ "$CODE" == "0" ]] || fail "etl -> db should be reachable after the fix: $(cat "$WORK/fixed-etl.json")"
CODE="$(check_json "$WORK/still-ssh.json" internet web --port 22 --snapshot "$WORK/moto-fixed")"
[[ "$CODE" == "1" && "$(field "$WORK/still-ssh.json" explanation_code)" == "SUBNET_ACL_RESTRICTION" ]] || fail "SSH from the internet must stay blocked: $(cat "$WORK/still-ssh.json")"
echo "  batch -> internet: reachable; etl -> db: reachable; internet -> web:22 still blocked"

echo "▸ flow logs and Reachability Analyzer: what the emulator implements"
"$AWS" --endpoint-url "$EP" s3api create-bucket --bucket cc-flow-logs --create-bucket-configuration "LocationConstraint=$REGION" > /dev/null || fail "create flow-log bucket"
ec2 create-flow-logs --resource-type VPC --resource-ids "$VPC" --traffic-type ALL --log-destination-type s3 --log-destination arn:aws:s3:::cc-flow-logs > "$WORK/flow.json" || fail "create-flow-logs"
[[ "$(field "$WORK/flow.json" Unsuccessful)" == "[]" ]] || fail "create-flow-logs: $(cat "$WORK/flow.json")"
[[ "$(get describe-flow-logs --filter "Name=resource-id,Values=$VPC" --query 'FlowLogs[0].TrafficType')" == "ALL" ]] || fail "describe-flow-logs should list the VPC flow log"
echo "  flow log configured on $VPC (moto stores the configuration; it generates no records)"
WEB_ID="$(get describe-instances --filters Name=tag:Name,Values=web --query 'Reservations[0].Instances[0].InstanceId')"
if ec2 create-network-insights-path --source "$WEB_ID" --destination-ip 198.51.100.10 --protocol tcp --destination-port 443 > "$WORK/nip.json" 2> "$WORK/nip.err"; then
  echo "  (this moto release accepts create-network-insights-path; vpcpath's verdicts are still checked against the documented fixture)"
else
  echo "  (moto does not implement Reachability Analyzer; its explanation codes are checked against the documented fixture instead)"
fi

echo "SMOKE OK: cloud/vpc-reachability"

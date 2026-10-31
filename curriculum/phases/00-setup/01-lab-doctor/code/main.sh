#!/usr/bin/env bash
set -euo pipefail

CLUSTER="${1:-cc-lab}"

if ! command -v kind >/dev/null; then
  echo "kind is not installed; run: python3 code/main.py plan" >&2
  exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "cluster $CLUSTER already exists"
else
  kind create cluster --name "$CLUSTER" --wait 120s
fi

kubectl cluster-info --context "kind-$CLUSTER"
kubectl get nodes -o wide
kubectl run lab-check --image=alpine:3.22 --restart=Never --rm -i --quiet -- sh -c 'echo "pod ok on $(uname -r)"'
echo "lab ready: kind-$CLUSTER  (delete with: kind delete cluster --name $CLUSTER)"

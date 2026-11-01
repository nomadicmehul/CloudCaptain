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
CTX=(--context "kind-$CLUSTER")
kubectl "${CTX[@]}" delete pod lab-check --ignore-not-found --wait=true >/dev/null
kubectl "${CTX[@]}" run lab-check --image=alpine:3.22 --restart=Never -- sh -c 'echo "pod ok on $(uname -r)"' >/dev/null
kubectl "${CTX[@]}" wait --for=jsonpath='{.status.phase}'=Succeeded pod/lab-check --timeout=180s >/dev/null
kubectl "${CTX[@]}" logs lab-check
kubectl "${CTX[@]}" delete pod lab-check --wait=false >/dev/null
echo "lab ready: kind-$CLUSTER  (delete with: kind delete cluster --name $CLUSTER)"

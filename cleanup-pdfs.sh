#!/bin/bash
# ============================================================
# CloudCaptain PDF Cleanup Script
# ============================================================
# All PDF content has been converted to native Markdown pages.
# This script removes the original PDF directories to reduce
# repo size from ~3.9GB to under 50MB.
#
# Run from the CloudCaptain repo root:
#   chmod +x cleanup-pdfs.sh
#   ./cleanup-pdfs.sh
# ============================================================

set -euo pipefail

echo "🧹 CloudCaptain PDF Cleanup"
echo "=========================="
echo ""

# Count before
pdf_count=$(find . -name "*.pdf" -type f | wc -l)
pdf_size=$(find . -name "*.pdf" -type f -exec du -ch {} + 2>/dev/null | tail -1 | cut -f1)
echo "Found $pdf_count PDF files ($pdf_size total)"
echo ""

# Directories containing PDFs (all content converted to Markdown)
dirs_to_remove=(
  "AWS"
  "Ansible"
  "Azure"
  "Bash"
  "CI-CD"
  "Chef"
  "Cloud Computing"
  "Cloud Security"
  "DevOps"
  "DevSecOps"
  "Docker"
  "FinOps"
  "Git"
  "GitOps"
  "Github Actions"
  "Google Cloud Provider"
  "Gradle"
  "Jenkins"
  "Kubernetes"
  "Linux"
  "Multi Cloud"
  "Nephio"
  "Networking"
  "Nginx"
  "Podman"
  "Python"
  "SRE"
  "TERRAFORM"
  "Yaml"
)

echo "The following directories will be removed:"
for dir in "${dirs_to_remove[@]}"; do
  if [ -d "$dir" ]; then
    size=$(du -sh "$dir" 2>/dev/null | cut -f1)
    echo "  📁 $dir ($size)"
  fi
done

echo ""
read -p "Proceed with deletion? (y/N) " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

echo ""
for dir in "${dirs_to_remove[@]}"; do
  if [ -d "$dir" ]; then
    echo "Removing: $dir"
    rm -rf "$dir"
  fi
done

echo ""
echo "✅ Done! PDF directories removed."
echo ""
echo "Next steps:"
echo "  1. git add -A"
echo "  2. git commit -m 'Remove PDF files — all content converted to native Markdown'"
echo "  3. git push"
echo ""
echo "Optional — to also remove PDFs from git history (saves space on clone):"
echo "  git filter-branch --tree-filter 'rm -rf AWS Ansible Azure Bash CI-CD Chef \"Cloud Computing\" \"Cloud Security\" DevOps DevSecOps Docker FinOps Git GitOps \"Github Actions\" \"Google Cloud Provider\" Gradle Jenkins Kubernetes Linux \"Multi Cloud\" Nephio Networking Nginx Podman Python SRE TERRAFORM Yaml' HEAD"
echo "  git push --force"
echo "  ⚠️  Force push rewrites history — coordinate with contributors first!"

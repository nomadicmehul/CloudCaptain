#!/bin/bash
# ============================================================
# CloudCaptain PDF Cleanup Script
# ============================================================
# All PDF content has been converted to native Markdown pages.
# This script ONLY removes .pdf files, preserving scripts,
# YAML examples, markdown docs, and other non-PDF resources.
#
# Run from the CloudCaptain repo root:
#   chmod +x cleanup-pdfs.sh
#   ./cleanup-pdfs.sh
# ============================================================

set -euo pipefail

echo "CloudCaptain PDF Cleanup"
echo "========================"
echo ""

# Count before
pdf_count=$(find . -name "*.pdf" -type f -not -path "./.git/*" | wc -l)
pdf_size=$(find . -name "*.pdf" -type f -not -path "./.git/*" -exec du -ch {} + 2>/dev/null | tail -1 | cut -f1)
echo "Found $pdf_count PDF files ($pdf_size total)"
echo ""

# Show breakdown by directory
echo "PDF files by directory:"
find . -name "*.pdf" -type f -not -path "./.git/*" | sed 's|^\./||' | cut -d'/' -f1 | sort | uniq -c | sort -rn
echo ""

read -p "Delete all $pdf_count PDF files? Non-PDF files (scripts, YAML, markdown) will be preserved. (y/N) " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

echo ""
echo "Removing PDF files..."
find . -name "*.pdf" -type f -not -path "./.git/*" -delete
echo ""

# Clean up empty directories left behind
echo "Cleaning up empty directories..."
find . -type d -empty -not -path "./.git/*" -delete 2>/dev/null || true

echo ""
remaining=$(find . -name "*.pdf" -type f -not -path "./.git/*" | wc -l)
echo "Done! $remaining PDF files remaining (should be 0)."
echo ""
echo "Next steps:"
echo "  1. git add -A"
echo "  2. git commit -m 'Remove PDF files - all content converted to native Markdown'"
echo "  3. git push"
echo ""
echo "Optional - to also remove PDFs from git history (saves clone size):"
echo "  git filter-repo --path-glob '*.pdf' --invert-paths"
echo "  git push --force"
echo "  Warning: Force push rewrites history - coordinate with contributors first!"

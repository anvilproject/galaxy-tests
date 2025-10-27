#!/bin/bash
# Combined script to clean up all redundant firewall rules
# This script deletes duplicate cloudbridge rules and stale GKE health check rules

set -e

echo "==========================================="
echo "GCP Firewall Rules Cleanup"
echo "==========================================="
echo ""

# Get current count
CURRENT=$(gcloud compute firewall-rules list --format="value(name)" | wc -l)
echo "Current rule count: $CURRENT / 500 rules"
echo ""

# Generate deletion scripts
echo "Analyzing firewall rules..."
python3 "$(dirname "$0")/analyze_firewall_rules.py" > /tmp/cloudbridge-analysis.txt 2>&1
python3 "$(dirname "$0")/delete-stale-gke-rules.py" > /tmp/gke-cleanup.sh 2>&1

# Extract deletion commands for cloudbridge
grep "^gcloud compute firewall-rules delete firewall-" /tmp/cloudbridge-analysis.txt > /tmp/delete-cloudbridge.sh || echo "# No cloudbridge rules to delete" > /tmp/delete-cloudbridge.sh
chmod +x /tmp/delete-cloudbridge.sh

# Make GKE cleanup executable
chmod +x /tmp/gke-cleanup.sh

# Count rules to be deleted
CLOUDBRIDGE_COUNT=$(grep -c "^gcloud" /tmp/delete-cloudbridge.sh || echo "0")
GKE_COUNT=$(grep -c "^gcloud compute firewall-rules delete k8s-" /tmp/gke-cleanup.sh || echo "0")
TOTAL_DELETE=$((CLOUDBRIDGE_COUNT + GKE_COUNT))

echo ""
echo "Rules to be deleted:"
echo "  - $CLOUDBRIDGE_COUNT duplicate cloudbridge-net-36735a rules"
echo "  - $GKE_COUNT stale GKE health check rules"
echo "  - Total: $TOTAL_DELETE rules"
echo ""
echo "After cleanup: ~$((CURRENT - TOTAL_DELETE)) / 500 rules"
echo ""

if [ "$1" != "--force" ]; then
  read -p "Continue? (yes/no): " CONFIRM
  if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 0
  fi
fi

echo ""
echo "Step 1: Deleting duplicate cloudbridge rules..."
if [ "$CLOUDBRIDGE_COUNT" -gt 0 ]; then
  bash /tmp/delete-cloudbridge.sh
  echo "  ✓ Deleted $CLOUDBRIDGE_COUNT cloudbridge duplicate rules"
else
  echo "  ✓ No cloudbridge duplicate rules to delete"
fi

echo ""
echo "Step 2: Deleting stale GKE health check rules..."
if [ "$GKE_COUNT" -gt 0 ]; then
  bash /tmp/gke-cleanup.sh
  echo "  ✓ Deleted $GKE_COUNT stale GKE health check rules"
else
  echo "  ✓ No stale GKE health check rules to delete"
fi

echo ""
echo "Cleanup complete!"
echo ""
NEW_COUNT=$(gcloud compute firewall-rules list --format="value(name)" | wc -l)
SAVED=$((CURRENT - NEW_COUNT))
echo "Previous rule count: $CURRENT"
echo "New rule count: $NEW_COUNT / 500 rules"
echo "Rules deleted: $SAVED"
echo "Remaining capacity: $((500 - NEW_COUNT)) rules"

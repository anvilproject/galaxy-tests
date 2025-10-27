# Firewall Rules Cleanup Scripts

These scripts help manage and clean up GCP firewall rules to prevent hitting the 500 rule quota limit.

## Problem

The GCP project hit the firewall quota limit (500 rules) with 496 rules, preventing new GKE LoadBalancer services from creating necessary firewall rules. This caused the error:

```
Error syncing load balancer: failed to ensure load balancer: googleapi: Error 403: QUOTA_EXCEEDED - Quota 'FIREWALLS' exceeded. Limit: 500.0 globally.
```

## Root Causes

1. **105 duplicate cloudbridge-net-36735a rules** - Multiple identical firewall rules from repeated CloudLaunch/CloudBridge K8s deployments
2. **175 stale GKE health check rules** - Rules from deleted GKE clusters that weren't cleaned up automatically

## Scripts

### 1. `analyze_firewall_rules.py`

Analyzes all firewall rules and identifies duplicates and stale rules.

**Usage:**
```bash
python3 .github/scripts/analyze_firewall_rules.py
```

**Output:**
- Summary of rules by network
- List of duplicate cloudbridge rules
- List of stale GKE health check rules
- Deletion commands

### 2. `delete-stale-gke-rules.py`

Identifies and generates deletion script for GKE health check rules from deleted clusters.

**Usage:**
```bash
python3 .github/scripts/delete-stale-gke-rules.py > delete-gke-rules.sh
bash delete-gke-rules.sh
```

### 3. `cleanup-firewall-rules.sh` (Recommended)

Combined script that runs both analysis and cleanup operations.

**Usage:**
```bash
# Interactive mode (asks for confirmation)
bash .github/scripts/cleanup-firewall-rules.sh

# Force mode (no confirmation)
bash .github/scripts/cleanup-firewall-rules.sh --force
```

**What it does:**
1. Analyzes current firewall rules
2. Identifies duplicates and stale rules
3. Shows summary of rules to be deleted
4. Asks for confirmation (unless --force)
5. Deletes duplicate cloudbridge rules
6. Deletes stale GKE health check rules
7. Shows final statistics

## Expected Results

**Before cleanup:** 496/500 rules (99% of quota)
**After cleanup:** ~216/500 rules (43% of quota)
**Savings:** 280 rules (56% reduction)

## Detailed Analysis

See `FIREWALL_ANALYSIS_SUMMARY.md` for:
- Detailed breakdown of duplicate rules
- List of stale clusters
- Prevention strategies
- Workflow improvements
- Monitoring recommendations

## Quick Start

To clean up firewall rules immediately:

```bash
# 1. Review what will be deleted
python3 .github/scripts/analyze_firewall_rules.py | less

# 2. Run cleanup (recommended)
bash .github/scripts/cleanup-firewall-rules.sh
```

## Prevention

### Update Workflow Cleanup Jobs

The cleanup job in `new-productiontest.yaml` and `new-edgetest.yaml` should ensure firewall rules are deleted:

```yaml
cleanup:
  if: always()  # Run even if tests fail
  needs: [deploygke, testgalaxy1]
  steps:
    - name: Delete stale health check firewall rules
      continue-on-error: true
      run: |
        CLUSTER_PREFIX="${{needs.deploygke.outputs.prefix}}"
        gcloud compute firewall-rules list --format="value(name)" \
          --filter="name~k8s-.*-node-http-hc AND targetTags:gke-${CLUSTER_PREFIX}-*" | \
          xargs -r -n1 gcloud compute firewall-rules delete --quiet
```

### Periodic Cleanup

Add a weekly scheduled workflow to clean up orphaned firewall rules:

```yaml
name: Cleanup Orphaned Firewall Rules
on:
  schedule:
    - cron: '0 0 * * 0'  # Weekly on Sunday
  workflow_dispatch:

jobs:
  cleanup:
    runs-on: ubuntu-latest
    steps:
      - uses: 'actions/checkout@v4'
      - uses: 'google-github-actions/auth@v2'
        with:
          workload_identity_provider: 'projects/526897014808/locations/global/workloadIdentityPools/galaxy-tests-identity-pool/providers/gxy-tests-provider'
          service_account: 'galaxy-tests-repo-actions-sa@anvil-and-terra-development.iam.gserviceaccount.com'
      - name: 'Set up Cloud SDK'
        uses: 'google-github-actions/setup-gcloud@v2'
      - name: Run firewall cleanup
        run: bash .github/scripts/cleanup-firewall-rules.sh --force
```

## Monitoring

Monitor firewall rule count to prevent future quota issues:

```bash
RULE_COUNT=$(gcloud compute firewall-rules list --format="value(name)" | wc -l)
echo "Current firewall rules: $RULE_COUNT / 500"

if [ $RULE_COUNT -gt 400 ]; then
  echo "WARNING: Approaching firewall quota limit!"
fi
```

## Safety

- Scripts use `--quiet` flag to avoid interactive prompts
- The main cleanup script asks for confirmation unless `--force` is used
- Backup current state before cleanup:
  ```bash
  gcloud compute firewall-rules list > firewall-backup-$(date +%Y%m%d).txt
  ```

## Questions?

See `FIREWALL_ANALYSIS_SUMMARY.md` for complete analysis and recommendations.

# GCP Firewall Rules Analysis - QUOTA_EXCEEDED Issue

## Problem
You've hit the GCP firewall quota limit of 500 rules with **496 total rules**. This is preventing new GKE LoadBalancer services from creating necessary firewall rules.

## Root Causes

### 1. Duplicate CloudBridge Rules (119 rules → can reduce to 14)
The `cloudbridge-net-36735a` network has **105 duplicate firewall rules** that are exact copies:
- **12 duplicates** each for ports: 10250, 10251, 10252, 10256, 6443, 4430
- **14 duplicates** each for ports: 22, 80, 443

These appear to be from repeated CloudLaunch/CloudBridge Kubernetes deployments that didn't clean up properly.

### 2. Stale GKE Health Check Rules (175 rules - ALL can be deleted)
There are **175 GKE node health check rules** from clusters that **no longer exist**:
- Format: `k8s-[hash]-node-http-hc`
- These are automatically created by GKE for LoadBalancer health checks
- They should be deleted when clusters are deleted, but aren't being cleaned up
- **ALL 175 rules are from deleted clusters** (0 active clusters found)

Examples of stale clusters:
- `gke-edge-25-04-*` series (April 2025 edge deployments)
- `gke-prod-25-09-*` series (September 2025 production deployments)
- `gke-ks-gke-cluster-*` and `gke-ks-gkm-cluster-*` series

## Recommendations

### Immediate Action (Delete 280 rules → Down to ~216 rules)

1. **Delete 105 duplicate cloudbridge rules** (keeps 14)
2. **Delete 175 stale GKE health check rules** (keeps 0)

This will free up **280 rules** (56% reduction) and bring you down to **~216 rules** (well under the 500 limit).

### Scripts Provided

#### 1. Delete Duplicate CloudBridge Rules
```bash
# Run the deletion script
bash /tmp/analyze_firewall_rules.py > delete-cloudbridge-duplicates.sh
bash delete-cloudbridge-duplicates.sh
```

#### 2. Delete Stale GKE Health Check Rules
```bash
# Run the deletion script
bash /tmp/delete-stale-gke-rules.sh
```

## Detailed Analysis

### CloudBridge Duplicate Breakdown

| Port/Purpose | Count | Keep | Delete |
|--------------|-------|------|--------|
| tcp:10250 (Kubelet API) | 12 | 1 | 11 |
| tcp:10251 (kube-scheduler) | 12 | 1 | 11 |
| tcp:10252 (kube-controller-manager) | 12 | 1 | 11 |
| tcp:10256 (kube-proxy) | 12 | 1 | 11 |
| tcp:22 (SSH) | 14 | 1 | 13 |
| tcp:443 (HTTPS) | 14 | 1 | 13 |
| tcp:4430 (Rancher) | 12 | 1 | 11 |
| tcp:6443 (Kubernetes API) | 12 | 1 | 11 |
| tcp:80 (HTTP) | 14 | 1 | 13 |
| Other rules | 5 | 5 | 0 |
| **Total** | **119** | **14** | **105** |

### GKE Health Check Rules by Cluster Type

| Cluster Type | Count | Status |
|--------------|-------|--------|
| edge-25-04-* (April 2025) | ~40 | Deleted |
| edge-25-05-* (May 2025) | ~20 | Deleted |
| edge-25-09-* (Sept 2025) | ~5 | Deleted |
| edge-25-10-* (Oct 2025) | ~15 | Deleted |
| prod-25-07-* to prod-25-10-* | ~35 | Deleted |
| ks-gke-cluster-* | ~12 | Deleted |
| ks-gkm-cluster-* | ~40 | Deleted |
| **Total** | **175** | **All Deleted** |

## Prevention Strategies

### For CloudBridge/CloudLaunch Deployments
1. **Review CloudBridge cleanup**: Ensure firewall rules are deleted when deployments are torn down
2. **Audit existing cloudbridge-net-36735a resources**: Consider deleting the entire network if no longer needed
3. **Use terraform or similar**: Infrastructure-as-code with proper state management prevents duplicate rules

### For GKE Clusters (Your Galaxy Tests)
Your GitHub workflows already have cleanup jobs, but they may not be running reliably.

**Current workflow issues:**
- In `new-productiontest.yaml` and `new-edgetest.yaml`, the cleanup job is commented out or marked `if: always()`
- The cleanup job should run even if tests fail

**Recommended workflow improvements:**

```yaml
cleanup:
  if: always()  # Ensure this runs even on failure
  needs: [deploygke, testgalaxy1]
  runs-on: ubuntu-latest
  steps:
    - name: Delete the GKE cluster
      continue-on-error: true
      run: |
        gcloud container clusters delete "${{needs.deploygke.outputs.prefix}}" --zone "$GKE_ZONE" --quiet

    - name: Delete GCP Disks
      continue-on-error: true
      run: |
        gcloud compute disks delete "${{needs.deploygke.outputs.prefix}}-1-postgres-pd" --zone "$GKE_ZONE" --quiet
        gcloud compute disks delete "${{needs.deploygke.outputs.prefix}}-1-nfs-pd" --zone "$GKE_ZONE" --quiet

    - name: Delete stale health check firewall rules
      continue-on-error: true
      run: |
        # Get health check rules for this cluster
        CLUSTER_PREFIX="${{needs.deploygke.outputs.prefix}}"
        gcloud compute firewall-rules list --format="value(name)" \
          --filter="name~k8s-.*-node-http-hc AND targetTags:gke-${CLUSTER_PREFIX}-*" | \
          xargs -r -n1 gcloud compute firewall-rules delete --quiet
```

### Periodic Cleanup Script
Add a scheduled GitHub Action to clean up orphaned firewall rules:

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
      - name: Authenticate to GCP
        uses: 'google-github-actions/auth@v2'
        with:
          workload_identity_provider: '...'
          service_account: '...'

      - name: Delete orphaned health check rules
        run: |
          # Get all existing clusters
          CLUSTERS=$(gcloud container clusters list --format="value(name)")

          # Get all health check rules
          gcloud compute firewall-rules list \
            --format="csv[no-heading](name,targetTags)" \
            --filter="name~k8s-.*-node-http-hc" | \
          while IFS=',' read -r rule_name target_tag; do
            cluster_exists=false
            for cluster in $CLUSTERS; do
              if echo "$target_tag" | grep -q "$cluster"; then
                cluster_exists=true
                break
              fi
            done

            if [ "$cluster_exists" = false ]; then
              echo "Deleting orphaned rule: $rule_name"
              gcloud compute firewall-rules delete "$rule_name" --quiet
            fi
          done
```

## Execution Plan

1. **Backup current state** (optional but recommended):
   ```bash
   gcloud compute firewall-rules list > firewall-rules-backup-$(date +%Y%m%d).txt
   ```

2. **Delete duplicate cloudbridge rules** (saves 105 rules):
   ```bash
   # See script output from analyze_firewall_rules.py above
   ```

3. **Delete stale GKE health check rules** (saves 175 rules):
   ```bash
   bash /tmp/delete-stale-gke-rules.sh
   ```

4. **Verify cleanup**:
   ```bash
   gcloud compute firewall-rules list --format="value(name)" | wc -l
   # Should show ~216 rules
   ```

5. **Retry your workflow**:
   - The `new-edgetest.yaml` workflow should now succeed
   - Monitor for future quota issues

6. **Implement prevention**:
   - Update workflow cleanup jobs
   - Add periodic cleanup script
   - Review CloudBridge/CloudLaunch usage

## Questions to Investigate

1. **CloudBridge Network**: Is `cloudbridge-net-36735a` still in use?
   - If not, consider deleting the entire network and all its rules
   - Check with: `gcloud compute networks describe cloudbridge-net-36735a`

2. **CloudLaunch Deployments**: Are there active CloudLaunch CM2 deployments?
   - Rules are tagged with `cloudlaunch-cm2`
   - If no active deployments, all 119 rules can be deleted

3. **Workflow Cleanup**: Why aren't cleanup jobs running?
   - Check GitHub Actions logs for failed cleanup jobs
   - Verify GCP permissions for service account

## Monitoring

After cleanup, set up monitoring to prevent future quota issues:

```bash
# Add to your monitoring/alerting system
RULE_COUNT=$(gcloud compute firewall-rules list --format="value(name)" | wc -l)
if [ $RULE_COUNT -gt 400 ]; then
  echo "WARNING: Firewall rule count is $RULE_COUNT (approaching 500 limit)"
  # Send alert
fi
```

## Summary

**Current state:** 496/500 rules (99% of quota)
**After cleanup:** ~216/500 rules (43% of quota)
**Savings:** 280 rules (56% reduction)

This cleanup will resolve your immediate issue and provide significant headroom for new deployments.

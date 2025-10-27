#!/usr/bin/env python3
"""
Delete stale GKE health check firewall rules from deleted clusters.
This script will identify rules associated with clusters that no longer exist.
"""

import subprocess
import json
import sys

def get_existing_clusters():
    """Get list of currently existing GKE clusters."""
    result = subprocess.run(
        ['gcloud', 'container', 'clusters', 'list', '--format=json'],
        capture_output=True,
        text=True
    )
    clusters = json.loads(result.stdout)
    cluster_names = [cluster['name'] for cluster in clusters]
    print(f"Found {len(cluster_names)} existing GKE clusters")
    return cluster_names

def get_gke_health_check_rules():
    """Get all GKE node health check firewall rules."""
    result = subprocess.run(
        ['gcloud', 'compute', 'firewall-rules', 'list', '--format=json'],
        capture_output=True,
        text=True
    )
    all_rules = json.loads(result.stdout)

    # Filter for GKE health check rules
    gke_rules = []
    for rule in all_rules:
        if 'node-http-hc' in rule['name']:
            target_tags = rule.get('targetTags', [])
            if target_tags:
                # Extract cluster name from tag like "gke-clustername-hash-node"
                cluster_tag = target_tags[0]
                # Remove the "-node" suffix and hash to get base cluster name
                # e.g., "gke-prod-25-09-03-00-37-f11f5349-node" -> "gke-prod-25-09-03-00-37"
                parts = cluster_tag.split('-')
                if len(parts) >= 2:
                    # Reconstruct without hash and "-node"
                    cluster_name = '-'.join(parts[:-2]) if parts[-1] == 'node' else '-'.join(parts[:-1])
                    gke_rules.append({
                        'rule_name': rule['name'],
                        'cluster_tag': cluster_tag,
                        'cluster_name': cluster_name
                    })

    print(f"Found {len(gke_rules)} GKE health check firewall rules")
    return gke_rules

def identify_stale_rules(existing_clusters, gke_rules):
    """Identify rules for clusters that no longer exist."""
    stale_rules = []
    for rule in gke_rules:
        # Check if cluster exists
        if rule['cluster_name'] not in existing_clusters:
            stale_rules.append(rule)

    return stale_rules

def main():
    print("# " + "="*80)
    print("# IDENTIFYING STALE GKE HEALTH CHECK RULES")
    print("# " + "="*80 + "\n")

    existing_clusters = get_existing_clusters()
    gke_rules = get_gke_health_check_rules()
    stale_rules = identify_stale_rules(existing_clusters, gke_rules)

    print(f"#\n# Stale rules (from deleted clusters): {len(stale_rules)}")
    print(f"# Active rules (from existing clusters): {len(gke_rules) - len(stale_rules)}\n")

    if len(stale_rules) == 0:
        print("# No stale rules found. All GKE health check rules are for active clusters.")
        return

    print("# Stale rules to delete:\n")
    for rule in stale_rules[:10]:
        print(f"#   {rule['rule_name']} (cluster: {rule['cluster_name']})")
    if len(stale_rules) > 10:
        print(f"#   ... and {len(stale_rules) - 10} more")

    print("#\n" + "="*80)
    print("# DELETION SCRIPT")
    print("# " + "="*80 + "\n")

    print("#!/bin/bash")
    print("# Delete stale GKE health check firewall rules")
    print("set -e\n")

    for i, rule in enumerate(stale_rules):
        print(f"gcloud compute firewall-rules delete {rule['rule_name']} --quiet")
        if (i + 1) % 50 == 0:
            print(f"echo 'Deleted {i+1} rules so far...'")

    print(f"\necho 'Successfully deleted {len(stale_rules)} stale GKE health check rules'")

if __name__ == '__main__':
    main()

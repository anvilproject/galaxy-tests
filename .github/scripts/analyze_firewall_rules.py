#!/usr/bin/env python3
"""Analyze GCP firewall rules to find duplicates and redundant rules."""

import subprocess
import json
from collections import defaultdict

# Get all firewall rules in JSON format
result = subprocess.run(
    ['gcloud', 'compute', 'firewall-rules', 'list', '--format=json'],
    capture_output=True,
    text=True
)

rules = json.loads(result.stdout)

print(f"Total firewall rules: {len(rules)}\n")

# Group rules by network
by_network = defaultdict(list)
for rule in rules:
    network = rule.get('network', '').split('/')[-1]
    by_network[network].append(rule)

print("Rules per network:")
for network, network_rules in sorted(by_network.items(), key=lambda x: len(x[1]), reverse=True):
    print(f"  {network}: {len(network_rules)}")

print("\n" + "="*80)
print("DUPLICATE ANALYSIS - cloudbridge-net-36735a")
print("="*80 + "\n")

# Focus on cloudbridge-net-36735a which has the most rules
cloudbridge_rules = by_network.get('cloudbridge-net-36735a', [])

# Group by (direction, sources, allowed, target_tags) to find exact duplicates
duplicates = defaultdict(list)
for rule in cloudbridge_rules:
    direction = rule.get('direction', '')
    source_ranges = tuple(sorted(rule.get('sourceRanges', [])))
    allowed = tuple(sorted([f"{a.get('IPProtocol', '')}:{','.join(sorted(a.get('ports', [])))}"
                           for a in rule.get('allowed', [])]))
    target_tags = tuple(sorted(rule.get('targetTags', [])))

    key = (direction, source_ranges, allowed, target_tags)
    duplicates[key].append(rule['name'])

print("Duplicate rules (same direction, source, ports, and target tags):\n")
duplicate_count = 0
rules_to_delete = []
for key, names in sorted(duplicates.items()):
    if len(names) > 1:
        direction, sources, allowed, tags = key
        print(f"Group: {direction} | Sources: {sources or 'ANY'} | Allowed: {allowed} | Tags: {tags}")
        print(f"  Count: {len(names)} duplicates")
        print(f"  Rules: {', '.join(sorted(names)[:5])}{'...' if len(names) > 5 else ''}")
        print(f"  KEEP: {names[0]}")
        print(f"  DELETE: {len(names)-1} rules")
        duplicate_count += len(names) - 1
        rules_to_delete.extend(names[1:])
        print()

print(f"Total cloudbridge-net-36735a rules: {len(cloudbridge_rules)}")
print(f"Total duplicate rules that can be deleted: {duplicate_count}")
print(f"Rules remaining after cleanup: {len(cloudbridge_rules) - duplicate_count}\n")

print("="*80)
print("GKE NODE HEALTH CHECK RULES ANALYSIS")
print("="*80 + "\n")

# Analyze GKE node health check rules
gke_hc_rules = [r for r in rules if 'node-http-hc' in r['name']]
print(f"Total GKE node health check rules: {len(gke_hc_rules)}\n")

# Group by cluster
gke_by_cluster = defaultdict(list)
for rule in gke_hc_rules:
    # Extract cluster name from target tags
    tags = rule.get('targetTags', [])
    if tags:
        cluster = tags[0]  # Usually like "gke-clustername-hash-node"
        gke_by_cluster[cluster].append(rule['name'])

print("Health check rules by cluster (may indicate stale rules from deleted clusters):\n")
for cluster, rule_names in sorted(gke_by_cluster.items()):
    print(f"  {cluster}: {len(rule_names)} rules - {rule_names[0]}")

print("\n" + "="*80)
print("RECOMMENDATIONS")
print("="*80 + "\n")

print(f"1. Delete {duplicate_count} duplicate cloudbridge-net-36735a rules")
print(f"2. Review {len(gke_hc_rules)} GKE health check rules - many may be from deleted clusters")
print(f"3. Total potential deletions: {duplicate_count + len(gke_hc_rules)} rules")
print(f"4. After cleanup, you'd have ~{len(rules) - duplicate_count - len(gke_hc_rules)} rules")
print(f"   (well under the 500 limit)\n")

# Create deletion script for cloudbridge duplicates
print("="*80)
print("DELETION COMMANDS FOR CLOUDBRIDGE DUPLICATES")
print("="*80 + "\n")
print("# Save this to delete-duplicate-firewall-rules.sh\n")
print("#!/bin/bash")
print("# Delete duplicate cloudbridge-net-36735a firewall rules")
print("set -e\n")
for i, rule in enumerate(sorted(rules_to_delete)):
    print(f"gcloud compute firewall-rules delete {rule} --quiet")
    if (i + 1) % 50 == 0:
        print(f"echo 'Deleted {i+1} rules so far...'")

#!/bin/bash
# Samples the VM's connection-admission counters for §A5 Theory 2.
#
# The runner records its own view of a failed handshake; nginx records
# requests that already completed one. Neither can say whether a SYN
# reached this machine and went unanswered. These counters can: SynRecv
# and ListenOverflows/ListenDrops move only if the SYN arrived, so they
# separate "dropped before the VM" from "dropped by the VM".
#
# Runs detached on the VM as root, writing counters until the run ends and
# the instance is deleted. Both nginx layers listen in the host network
# namespace (ingress-nginx uses hostNetwork), so the host's own counters
# cover the public listener.

set -u

OUT=${1:-/var/log/anvil-vm-net.log}
INTERVAL=${2:-10}
# The VM outlives neither the job nor its own deletion, but a sampler that
# cannot exit is still a sampler that can be left behind by a failed
# teardown.
MAX_SECONDS=${3:-21600}

exec >>"$OUT" 2>&1

primary_interface() {
  ip route show default 2>/dev/null | awk '/^default/{print $5; exit}'
}

# Both files are "header line, values line" per section; pair them by
# position and print only the named counters.
proc_counters() {
  local file="$1" section="$2" wanted="$3"
  awk -v section="$section" -v wanted=" $wanted " '
    $1 == section {
      if (!seen) { for (i = 2; i <= NF; i++) name[i] = $i; seen = 1; next }
      for (i = 2; i <= NF; i++)
        if (index(wanted, " " name[i] " ")) printf "%s %s\n", name[i], $i
    }' "$file" 2>/dev/null
}

conntrack_counters() {
  # One row per CPU, values in hex. Recorded raw and summed during
  # analysis: Debian's awk is mawk, which has neither strtonum() nor hex
  # input, and a sampler is the wrong place to hand-roll a hex parser.
  # The column names are in the header, captured once above.
  awk 'NR > 1 {printf "conntrack_row %s\n", $0}' /proc/net/stat/nf_conntrack 2>/dev/null
}

{
  echo "=== static $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'kernel %s\n' "$(uname -r)"
  printf 'nproc %s\n' "$(nproc --all 2>/dev/null)"
  printf 'primary_interface %s\n' "$(primary_interface)"
  # Ceilings for the queue depths sampled below: a listener cannot hold
  # more than the smaller of its own backlog and somaxconn, and
  # tcp_max_syn_backlog bounds half-open connections before the kernel
  # starts dropping or issuing syncookies.
  sysctl net.core.somaxconn net.ipv4.tcp_max_syn_backlog net.ipv4.tcp_syncookies \
         net.ipv4.tcp_abort_on_overflow net.ipv4.tcp_syn_retries \
         net.ipv4.tcp_synack_retries 2>/dev/null
  for f in /proc/sys/net/netfilter/nf_conntrack_max /proc/sys/net/netfilter/nf_conntrack_buckets; do
    [ -r "$f" ] && printf '%s %s\n' "$(basename "$f")" "$(cat "$f")"
  done
  head -1 /proc/net/stat/nf_conntrack 2>/dev/null | sed 's/^/conntrack_columns /'
} || true

started=$(date +%s)
while [ $(($(date +%s) - started)) -lt "$MAX_SECONDS" ]; do
  {
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # Half-open connections: a SYN arrived and the handshake has not
    # completed. Unlike everything the runner can see, this is non-zero
    # only if the packet reached this machine.
    printf 'syn_recv %s\n' "$(ss -Htan state syn-recv 2>/dev/null | wc -l)"
    printf 'established %s\n' "$(ss -Htan state established 2>/dev/null | wc -l)"
    printf 'time_wait %s\n' "$(ss -Htan state time-wait 2>/dev/null | wc -l)"

    # Per-listener accept queue. For a listening socket ss reports Recv-Q
    # as the number of completed connections waiting to be accepted and
    # Send-Q as the configured backlog, so Recv-Q approaching Send-Q is
    # the queue pressure Theory 2 predicts.
    #
    # Rows are kept per socket, not merged. ingress-nginx binds :80 once
    # per worker with SO_REUSEPORT, and the kernel hashes each incoming
    # SYN to one of those queues: a single blocked worker can fill its own
    # queue and lose connections while the other queues sit empty. Summing
    # them would hide precisely that.
    #
    # Restricted to the ports the runner actually contacts - 80 for the
    # tests, 443 alongside it, 6443 for kubectl. The node listens on ~60
    # sockets and the rest (etcd, kubelet, CNI) cannot be reached from the
    # runner at all; at 10s intervals they would be most of the file.
    ss -Hltn 2>/dev/null | awk '$4 ~ /:(80|443|6443)$/ {printf "listen_queue %s recvq %s sendq %s\n", $4, $2, $3}'

    # AttemptFails/RetransSegs here are this machine's own outbound
    # connects (to CVMFS, GCP Batch, container registries), not the
    # runner's - useful as a control for whether the VM's egress is
    # losing packets at the same time.
    proc_counters /proc/net/snmp 'Tcp:' \
      'ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs'

    # ListenOverflows: the accept queue was full when a handshake
    # completed. ListenDrops: any reason a SYN was dropped at the
    # listener. SyncookiesSent: the SYN queue overflowed. TCPBacklogDrop:
    # the socket backlog was full. Any of these being non-zero places the
    # loss on this side; all of them staying zero while the runner
    # retransmits places it before this machine.
    proc_counters /proc/net/netstat 'TcpExt:' \
      'ListenOverflows ListenDrops SyncookiesSent SyncookiesRecv SyncookiesFailed TCPBacklogDrop TCPReqQFullDrop TCPReqQFullDoCookies TCPSynRetrans TCPTimeouts TCPAbortOnMemory PruneCalled'

    f=/proc/sys/net/netfilter/nf_conntrack_count
    [ -r "$f" ] && printf 'nf_conntrack_count %s\n' "$(cat "$f")"
    conntrack_counters

    # NIC-level loss, which precedes every counter above. Every interface
    # rather than just the default route's: the route lookup can come back
    # empty, and a sampler that silently records nothing is worse than one
    # that records a little too much. Per-pod veths are the exception -
    # Calico creates one per pod, they are nothing to do with the runner's
    # route in, and there are more of them than every other line combined.
    awk 'NR > 2 {
        gsub(/:/, " ")
        if ($1 != "lo" && $1 !~ /^(cali|veth|lxc)/)
          printf "nic %s rx_errs %s rx_drop %s rx_fifo %s tx_errs %s tx_drop %s tx_fifo %s\n", \
                 $1, $4, $5, $6, $12, $13, $14
      }' /proc/net/dev 2>/dev/null

    cat /proc/loadavg 2>/dev/null
    awk '/^cpu /{printf "cpu_steal %s cpu_idle %s cpu_total %s\n", $9, $5, $2+$3+$4+$5+$6+$7+$8+$9}' /proc/stat 2>/dev/null
    grep -E '^(MemTotal|MemAvailable):' /proc/meminfo 2>/dev/null
  } || true
  sleep "$INTERVAL"
done

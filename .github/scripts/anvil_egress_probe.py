#!/usr/bin/env python3
"""Measure TCP connect success against one host from wherever this runs.

§A5's open question is where handshakes to the VM are lost. The harness
can only ever sample one egress - the GitHub runner - so the same probe
has to be runnable from somewhere else against the same VM to tell a
path-dependent fault from a VM-side one.

Deliberately mimics the harness's traffic shape rather than its volume: a
new connection per request (no keep-alive), many of them, concurrent, all
to one address and port. That shape is what makes an address-translation
layer allocate a port per request, and it is what the job-status poll does
~63,000 times a run.

Reports connect outcomes separately from request outcomes, because §A5 is
a connection-establishment failure: a slow or failed *response* is a
different fault and should not be counted as the same thing.
"""

import argparse
import collections
import errno
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time

STOP = threading.Event()


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.connect_latencies = []
        self.outcomes = collections.Counter()
        self.failures = []

    def record(self, outcome, latency=None, detail=None, at=None):
        with self.lock:
            self.outcomes[outcome] += 1
            if latency is not None:
                self.connect_latencies.append(latency)
            if detail is not None:
                self.failures.append({"at": at, "outcome": outcome, "detail": detail})


def classify(exc):
    """Name the failure the way the kernel does, not the way Python wraps it."""
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        # The connect never completed within our budget. With no RST and no
        # ICMP this is the signature of a SYN that went unanswered.
        return "connect_timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connect_refused"
    if isinstance(exc, ConnectionResetError):
        return "connect_reset"
    if isinstance(exc, OSError):
        if exc.errno == errno.ETIMEDOUT:
            return "connect_timeout_kernel"
        if exc.errno in (errno.EADDRNOTAVAIL, errno.EADDRINUSE):
            # Local port exhaustion - our own limit, not the path's.
            return "local_port_exhaustion"
        if exc.errno == errno.EHOSTUNREACH:
            return "host_unreachable"
        if exc.errno == errno.ENETUNREACH:
            return "net_unreachable"
        return f"oserror_{errno.errorcode.get(exc.errno, exc.errno)}"
    return f"other_{type(exc).__name__}"


def one_request(host, port, path, connect_timeout, read_timeout, counters):
    started = time.monotonic()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(connect_timeout)
        sock.connect((host, port))
    except Exception as exc:  # noqa: BLE001 - the classification is the point
        counters.record(classify(exc), detail=f"{exc}", at=stamp)
        if sock is not None:
            sock.close()
        return
    connect_latency = time.monotonic() - started
    counters.record("connect_ok", latency=connect_latency)

    if path is None:
        sock.close()
        return
    try:
        sock.settimeout(read_timeout)
        request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode())
        # Enough to see the status line; the body is irrelevant here.
        data = sock.recv(64)
        if data.startswith(b"HTTP/"):
            counters.record(f"http_{data.split()[1].decode(errors='replace')}")
        else:
            counters.record("http_no_status")
    except Exception as exc:  # noqa: BLE001
        counters.record("request_" + classify(exc), detail=f"{exc}", at=stamp)
    finally:
        sock.close()


def paced_worker(args, counters, ticket):
    while not STOP.is_set():
        if not ticket.acquire(timeout=0.5):
            continue
        if STOP.is_set():
            return
        one_request(args.host, args.port, args.path, args.connect_timeout,
                    args.read_timeout, counters)


def polling_worker(args, counters, _ticket):
    """One test thread's job-status poll loop, as the client actually runs it.

    Closed loop, not a paced stream: connect, ask, close, wait, repeat. The
    difference matters. A paced probe emits a smooth rate; N independent
    pollers emit bursts whenever their cycles coincide, and burst arrival
    is what stresses an accept queue or a translation table. Reproducing
    the harness's *mean* rate while smoothing away its shape would be
    testing something the harness never does.
    """
    while not STOP.is_set():
        one_request(args.host, args.port, args.path, args.connect_timeout,
                    args.read_timeout, counters)
        if STOP.wait(args.poll_interval):
            return


def pacer(rate, ticket, duration):
    """Hand out one connection slot per 1/rate seconds."""
    interval = 1.0 / rate
    deadline = time.monotonic() + duration
    nxt = time.monotonic()
    while not STOP.is_set() and time.monotonic() < deadline:
        nxt += interval
        sleep_for = nxt - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        ticket.release()
    STOP.set()


def wait_until(epoch):
    """Block until a shared wall-clock start, so two arms overlap exactly.

    Comparing two egresses only means anything if they run against the
    same host at the same time - the thing being tested varies by the
    hour.
    """
    if not epoch:
        return
    delay = epoch - time.time()
    if delay > 0:
        print(f"waiting {delay:.0f}s for the shared start at "
              f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))}", file=sys.stderr)
        time.sleep(delay)
    elif delay < -30:
        print(f"WARNING: shared start was {-delay:.0f}s ago; arms will not overlap cleanly",
              file=sys.stderr)


def tcp_stats():
    """Best-effort kernel retransmission counters, Linux or macOS."""
    if os.path.exists("/proc/net/snmp"):
        wanted = {"ActiveOpens", "AttemptFails", "RetransSegs"}
        out = {}
        with open("/proc/net/snmp") as handle:
            names = None
            for line in handle:
                if not line.startswith("Tcp:"):
                    continue
                fields = line.split()[1:]
                if names is None:
                    names = fields
                else:
                    out = {n: int(v) for n, v in zip(names, fields) if n in wanted}
        return out
    try:
        raw = subprocess.run(["netstat", "-s", "-p", "tcp"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        for key in ("connection requests", "retransmitted", "bad connection attempts",
                    "connection attempts", "timeouts"):
            if key in line and line.split()[0].isdigit():
                out.setdefault(line, int(line.split()[0]))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--path", default="/galaxy/api/version",
                        help="Set to '' for a handshake-only probe that never sends a request.")
    parser.add_argument("--rate", type=float, default=28.0,
                        help="Connections per second. The harness averages ~27.6.")
    parser.add_argument("--concurrency", type=int, default=60,
                        help="Matches the harness's --parallel-tests.")
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--connect-timeout", type=float, default=30.0)
    parser.add_argument("--read-timeout", type=float, default=30.0)
    parser.add_argument("--mode", choices=("poll", "paced"), default="poll",
                        help="poll: N closed-loop pollers, as the test client runs. "
                             "paced: a smooth --rate stream.")
    parser.add_argument("--poll-interval", type=float, default=0.25,
                        help="poll mode: the client's GALAXY_TEST_POLLING_DELTA.")
    parser.add_argument("--start-at", type=float, default=0,
                        help="Unix epoch to start at, so two arms overlap exactly.")
    parser.add_argument("--label", default="")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    if args.path == "":
        args.path = None

    counters = Counters()
    ticket = threading.Semaphore(0)
    wait_until(args.start_at)
    before = tcp_stats()
    started_at = time.time()

    target = polling_worker if args.mode == "poll" else paced_worker
    threads = [threading.Thread(target=target, args=(args, counters, ticket), daemon=True)
               for _ in range(args.concurrency)]
    for thread in threads:
        thread.start()
    shape = (f"{args.concurrency} pollers every {args.poll_interval}s"
             if args.mode == "poll" else f"~{args.rate}/s across {args.concurrency} workers")
    print(f"probing {args.host}:{args.port} for {args.duration:.0f}s, {shape}", file=sys.stderr)
    try:
        if args.mode == "paced":
            pacer(args.rate, ticket, args.duration)
        else:
            STOP.wait(args.duration)
            STOP.set()
    except KeyboardInterrupt:
        STOP.set()
    for thread in threads:
        thread.join(timeout=args.connect_timeout + 5)

    elapsed = time.time() - started_at
    after = tcp_stats()
    latencies = sorted(counters.connect_latencies)
    attempts = sum(v for k, v in counters.outcomes.items()
                   if k.startswith("connect_") or k in ("local_port_exhaustion",
                                                        "host_unreachable", "net_unreachable")
                   or k.startswith("oserror_") or k.startswith("other_"))
    ok = counters.outcomes.get("connect_ok", 0)
    failed = attempts - ok

    report = {
        "label": args.label,
        "host": args.host,
        "port": args.port,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
        "elapsed_seconds": round(elapsed, 1),
        "mode": args.mode,
        "poll_interval": args.poll_interval,
        "concurrency": args.concurrency,
        "requested_rate": args.rate,
        "achieved_rate": round(attempts / elapsed, 2) if elapsed else 0,
        "connect_attempts": attempts,
        "connect_ok": ok,
        "connect_failed": failed,
        "connect_failure_pct": round(100.0 * failed / attempts, 4) if attempts else 0,
        "outcomes": dict(sorted(counters.outcomes.items())),
        "connect_latency_ms": {
            "n": len(latencies),
            "min": round(1000 * latencies[0], 2) if latencies else None,
            "p50": round(1000 * statistics.median(latencies), 2) if latencies else None,
            "p99": round(1000 * latencies[int(0.99 * (len(latencies) - 1))], 2) if latencies else None,
            "max": round(1000 * latencies[-1], 2) if latencies else None,
        },
        "tcp_stats_before": before,
        "tcp_stats_after": after,
        "failure_samples": counters.failures[:40],
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.json_out:
        with open(args.json_out, "w") as handle:
            handle.write(text + "\n")


if __name__ == "__main__":
    main()

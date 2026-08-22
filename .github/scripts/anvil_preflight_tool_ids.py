"""Advisory pre-flight check on a pinned tool-id list.

Compares the ids the run is about to test against the server's own
`tests_summary` keys and reports the ones that will produce nothing, so a
stale pin shows up in the log instead of silently consuming a slot. Six data
managers sat in `.github/scheduled-tool-ids.txt` for months doing exactly
that - they were pinned without the `data_manager/` path segment the
installed tool ids actually carry.

Reports, never rewrites: the pinned list is a reviewed commit, not something
CI should mutate. Always exits 0 - this is information, not a gate.

Usage: anvil_preflight_tool_ids.py <pinned-ids-file> <testable-ids-file>
"""

import sys


def read_ids(path: str) -> list[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def unversioned(tool_id: str) -> str:
    return tool_id.rsplit("/", 1)[0]


def main(pinned_path: str, testable_path: str) -> int:
    pinned = read_ids(pinned_path)
    testable = set(read_ids(testable_path))
    # A pinned id may name a tool the server has at a *different* version;
    # that still runs, so it's worth distinguishing from one it has never
    # heard of. Unversioned ids (Galaxy built-ins like Grep1) match directly.
    known_tools = {unversioned(t) for t in testable} | testable

    exact, version_miss, not_testable = [], [], []
    for tool_id in pinned:
        # Galaxy built-ins (Grep1, wc_gnu, __SORTLIST__ ...) are keyed
        # unversioned, so there is no version to miss - they either exist or
        # they don't.
        if tool_id in testable or ("/" not in tool_id and tool_id in known_tools):
            exact.append(tool_id)
        elif unversioned(tool_id) in known_tools:
            version_miss.append(tool_id)
        else:
            not_testable.append(tool_id)

    print(
        f"pinned {len(pinned)} | testable as pinned {len(exact)} | "
        f"tool known at another version {len(version_miss)} | not testable {len(not_testable)}"
    )
    for tool_id in version_miss:
        print(f"  VERSION-MISS  {tool_id}")
    for tool_id in not_testable:
        print(f"  NOT-TESTABLE  {tool_id}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))

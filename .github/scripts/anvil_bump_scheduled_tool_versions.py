"""Rewrite .github/scheduled-tool-ids.txt to the newest installed tool versions.

The pinned list names exact tool versions, while the tool set reaching the
instance is rebuilt continuously upstream (usegalaxy-tools' cloud toolset ->
CVMFS -> cvmfs-cloud-clone's daily bundle). Those updates only ever add
revisions, so a pin never stops resolving - it just quietly keeps testing an
older version while the newly published one, the one that actually changed,
goes unexercised.

The inventory to bump against is the instance's own `tests_summary` keys, as
committed by anvil-test.yaml to reports/anvil/testable-tool-ids.txt. The
toolshed is deliberately not consulted: it leads the deployed bundle by the
whole publish chain, so its newest version is routinely one the instance does
not have yet.

Only the version segment of an existing pin is ever rewritten. Tools are
never added or removed - which tools are covered is a separate, deliberate
decision (see CLAUDE.md), and a pin whose tool has vanished from the instance
is a finding to report, not something to silently drop.

Every case the version ordering cannot decide confidently is skipped and
reported rather than guessed, so the failure mode is a missed bump rather
than a downgraded pin.

A pin naming a tool the instance has no tests for is reported too. It can
never produce a result - `tests_summary` is what the run draws from - so it
spends a slot in silence rather than failing. Given the optional installed-id
list it separates the two causes, which call for different fixes: NOT-INSTALLED
means the pin or the tool is gone, NO-TESTS means the wrapper itself ships no
test cases and belongs in .github/excluded-tool-ids.txt.

Usage: anvil_bump_scheduled_tool_versions.py <pinned-list> <testable-ids>
                                             [installed-ids] [--write]
Without --write, reports what would change and leaves the file alone.
"""

import collections
import re
import sys

from packaging.version import (
    InvalidVersion,
    Version,
)


def read_ids(path: str) -> list[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def split_version(tool_id: str) -> tuple[str, str]:
    unversioned, _, version = tool_id.rpartition("/")
    return unversioned, version


def natural_key(text: str) -> tuple[tuple[int, object], ...]:
    """Digit runs as integers, so galaxy10 sorts after galaxy3, not before.

    The leading discriminator keeps a numeric run from ever being compared
    against a textual one.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", text)
        if part
    )


def version_key(version: str) -> tuple[Version, tuple]:
    """Order two versions of the same tool.

    PEP 440 covers the base, which is what gets pre-release, post-release and
    dev markers right. It does *not* cover the `+galaxyN` wrapper revision:
    that is a PEP 440 local segment, compared as a string, which puts
    `+galaxy10` *before* `+galaxy3`. Galaxy's own
    galaxy.tool_util.version.parse_version inherits the same ordering. Hence
    the split - PEP 440 for the base, natural ordering for the suffix.

    Raises InvalidVersion for a base PEP 440 cannot parse; the caller skips
    the tool rather than falling back to a guess.
    """
    base, _, local = version.partition("+")
    return Version(base), natural_key(local)


def untestable_reason(tool_id: str, installed: set[str]) -> str:
    """Why a pin the instance cannot test is untestable, as far as we can tell."""
    if not installed:
        return "NO-TESTS-OR-MISSING - the instance reports no tests for it"
    unversioned = split_version(tool_id)[0] or tool_id
    if tool_id in installed or any(i.startswith(f"{unversioned}/") for i in installed):
        return "NO-TESTS - installed, but the wrapper ships no test cases"
    return "NOT-INSTALLED - absent from the instance entirely"


def main(
    pinned_path: str,
    testable_path: str,
    installed_path: str = "",
    write: bool = False,
) -> int:
    pinned = read_ids(pinned_path)
    testable = read_ids(testable_path)
    installed = set(read_ids(installed_path)) if installed_path else set()

    available = collections.defaultdict(set)
    for tool_id in testable:
        unversioned, version = split_version(tool_id)
        if unversioned:
            available[unversioned].add(version)

    pin_counts = collections.Counter(split_version(t)[0] for t in pinned if "/" in t)

    bumped: list[tuple[str, str, str]] = []
    skipped: list[tuple[str, str]] = []
    updated: list[str] = []

    testable_set = set(testable)

    for tool_id in pinned:
        # Galaxy built-ins (Grep1, cat1, __SORTLIST__ ...) are keyed
        # unversioned, so there is no version to bump - but they can still be
        # untestable, and silently passing over them is how `intermine` sat in
        # the list through 30 runs without once producing a result.
        if "/" not in tool_id:
            if tool_id not in testable_set:
                skipped.append((tool_id, untestable_reason(tool_id, installed)))
            updated.append(tool_id)
            continue

        unversioned, version = split_version(tool_id)
        candidates = available.get(unversioned)
        if not candidates:
            skipped.append((tool_id, untestable_reason(tool_id, installed)))
            updated.append(tool_id)
            continue
        if pin_counts[unversioned] > 1:
            skipped.append((tool_id, "tool pinned at more than one version - resolve by hand"))
            updated.append(tool_id)
            continue

        try:
            keyed = sorted(candidates, key=version_key)
        except InvalidVersion as exception:
            skipped.append((tool_id, f"version ordering undecidable ({exception})"))
            updated.append(tool_id)
            continue

        newest = keyed[-1]
        if newest == version:
            updated.append(tool_id)
        elif version_key(newest) > version_key(version):
            bumped.append((unversioned, version, newest))
            updated.append(f"{unversioned}/{newest}")
        else:
            # The instance offers nothing newer than the pin. Usually the pin
            # was set from a bundle that has since lost a revision; leaving it
            # keeps the run comparable and surfaces the discrepancy.
            skipped.append((tool_id, f"instance's newest is older ({newest})"))
            updated.append(tool_id)

    untestable = [(t, r) for t, r in skipped if r.startswith(("NO-TESTS", "NOT-INSTALLED"))]

    print(
        f"pinned {len(pinned)} | bumped {len(bumped)} | "
        f"unchanged {len(pinned) - len(bumped) - len(skipped)} | "
        f"skipped {len(skipped)} | untestable {len(untestable)}"
    )
    for unversioned, old, new in bumped:
        print(f"  BUMP  {unversioned.rsplit('/', 1)[-1]}: {old} -> {new}")
    for tool_id, reason in skipped:
        print(f"  SKIP  {tool_id}: {reason}")

    # Called out separately from the SKIP lines: a pin that cannot be tested
    # costs a slot every night and never fails, so it only surfaces if
    # something says so out loud.
    if untestable:
        print(
            f"\n{len(untestable)} pinned tool(s) cannot produce a result. "
            "Give each one a .github/excluded-tool-ids.txt entry, or correct "
            "the pin:"
        )
        for tool_id, reason in untestable:
            print(f"  UNTESTABLE  {tool_id}: {reason}")

    if write and bumped:
        with open(pinned_path, "w") as f:
            f.write("\n".join(updated) + "\n")
        print(f"Wrote {len(bumped)} version bump(s) to {pinned_path}")
    elif write:
        print("No version bumps to write")

    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--write"]
    if len(args) not in (2, 3):
        sys.exit(__doc__)
    sys.exit(main(*args, write="--write" in sys.argv[1:]))

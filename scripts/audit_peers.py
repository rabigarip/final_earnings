"""Audit the peer set ("competitors") of every ticker in the universe.

For each name the deck uses `company_master.peer_group` (hand-curated) or, when
that's empty, the auto `registry_peer_set`. This script reproduces that exact
choice and flags any name whose peers are mostly in a DIFFERENT industry
cluster than the subject (the "ZTE listed a fintech peer" failure mode), so
peer quality can be verified across the whole book — not one ticker at a time.

Usage:
    python -m scripts.audit_peers              # summary + flagged names
    python -m scripts.audit_peers --verbose    # list every name's peers
    python -m scripts.audit_peers --strict      # flag ANY off-cluster peer (not just majority)

Exit code is non-zero when flagged names exist, so it can gate CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="print every name's peer set")
    ap.add_argument("--strict", action="store_true",
                    help="flag a name if ANY peer is off-cluster (default: majority)")
    args = ap.parse_args()

    from src.storage.db import init_db, seed_companies, load_company
    from src.services.ticker_registry import (
        registry_peer_set, industry_cluster, _registry_index,
    )
    init_db()
    seed_companies()

    idx = _registry_index()
    def _ind(t):  # noqa
        return idx.get(t, {}).get("industry") or ""

    universe = [t for t, r in idx.items()
                if r.get("is_canonical", True) and r.get("active", True)]

    curated = auto = no_peers = unclusterable = perfect = 0
    flagged: list[tuple] = []
    for tk in sorted(universe):
        c = load_company(tk) or {}
        pg = c.get("peer_group")
        peers = pg or registry_peer_set(tk)
        source = "curated" if pg else "auto"
        if pg:
            curated += 1
        else:
            auto += 1
        if not peers:
            no_peers += 1
            flagged.append((tk, _ind(tk), source, "NO PEERS", []))
            continue
        subj_cl = industry_cluster(_ind(tk))
        if not subj_cl:
            unclusterable += 1
            if args.verbose:
                print(f"  [skip-uncluster] {tk} {_ind(tk)}")
            continue
        # Only cluster-assess AUTO sets. Curated peer_groups are hand-picked
        # and routinely use global comps that aren't in the registry (NTR for
        # SABIC, XOM for Aramco) — we can't read their industry, so clustering
        # would false-flag them. They're trusted by construction.
        if source == "curated":
            if args.verbose:
                print(f"  {tk:12s} [curated] {_ind(tk)[:22]:22} -> {', '.join(peers)}")
            continue
        off = [(p, _ind(p)) for p in peers
               if industry_cluster(_ind(p)) != subj_cl]
        if args.verbose:
            print(f"  {tk:12s} [{source}] {_ind(tk)[:22]:22} -> " +
                  ", ".join(f"{p}:{industry_cluster(_ind(p)) or '?'}" for p in peers))
        bad = (len(off) > 0) if args.strict else (len(off) * 2 > len(peers))
        if bad:
            flagged.append((tk, _ind(tk), source, f"{len(off)}/{len(peers)} off-cluster", off))
        elif not off:
            perfect += 1

    print(f"\nUniverse: {len(universe)}  |  curated peer_group: {curated}  |  auto: {auto}")
    print(f"clusterable & all-peers-in-cluster (perfect): {perfect}  |  "
          f"unclusterable industry (not assessed): {unclusterable}  |  no peers: {no_peers}")
    mode = "ANY off-cluster" if args.strict else "majority off-cluster"
    print(f"\nFLAGGED ({mode}): {len(flagged)}")
    for tk, ind, source, why, off in flagged:
        print(f"  {tk:12s} [{source}] {ind[:26]:26} {why}")
        for p, pi in off[:5]:
            print(f"        ↳ {p}: {pi}")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())

"""
vix_buckets — single source of truth for VIX bucket boundaries.

Buckets are defined ONLY in config/config.yaml under entry.vix_buckets, as a
dict keyed by "<lo>-<hi>" range strings (the existing config format — the
structure of config.yaml is unchanged):

    entry:
      vix_buckets:
        13-16: {min_premium: 0.20, rsi_upper_threshold: 50.0, ...}
        16-20: {...}

Boundary semantics (preserved from the original hardcoded ladders in
tick_processor and risk_manager):

  - buckets sorted by lo must be contiguous: each bucket's hi == next lo
  - every bucket is half-open [lo, hi) except the top one, which is
    closed [lo, hi]
  - VIX below the first lo or above the last hi -> no bucket (None)

Adding, removing, or re-ranging a bucket therefore requires only a config
edit; both call sites (tick_processor RSI gates, risk_manager entry params)
resolve through classify().

Fail-loud: parse_buckets() raises ValueError on malformed keys, inverted
ranges, overlaps, or gaps. It runs at import time against the live config so
a bad config kills the engine at startup, not at trade time. An empty or
missing vix_buckets dict is allowed (yields no buckets -> risk_manager
blocks all entries) so stripped-down test configs still load.
"""

from dataclasses import dataclass, field
from typing import Optional

from config import CONFIG


@dataclass(frozen=True)
class VixBucket:
    name: str            # config key, e.g. "16-20" — used in logs and config lookups
    lo: float
    hi: float
    inclusive_hi: bool   # True only for the top bucket
    params: dict = field(default_factory=dict)

    def contains(self, vix: float) -> bool:
        if self.inclusive_hi:
            return self.lo <= vix <= self.hi
        return self.lo <= vix < self.hi


def parse_buckets(raw: Optional[dict]) -> list[VixBucket]:
    """
    Parse an entry.vix_buckets config dict into a sorted list of VixBucket.

    Returns [] for an empty/missing dict. Raises ValueError on a key that is
    not "<lo>-<hi>", lo >= hi, or buckets that overlap or leave a gap.
    """
    if not raw:
        return []

    parsed: list[tuple[float, float, str, dict]] = []
    for key, params in raw.items():
        name = str(key)
        lo_s, sep, hi_s = name.partition("-")
        if not sep:
            raise ValueError(
                f"vix_buckets key {name!r}: expected '<lo>-<hi>' range, e.g. '16-20'"
            )
        try:
            lo, hi = float(lo_s), float(hi_s)
        except ValueError:
            raise ValueError(
                f"vix_buckets key {name!r}: bounds must be numeric, e.g. '16-20'"
            ) from None
        if lo >= hi:
            raise ValueError(f"vix_buckets key {name!r}: lower bound must be < upper bound")
        parsed.append((lo, hi, name, params or {}))

    parsed.sort(key=lambda t: t[0])
    for (_, prev_hi, prev_name, _), (next_lo, _, next_name, _) in zip(parsed, parsed[1:]):
        if prev_hi > next_lo:
            raise ValueError(f"vix_buckets {prev_name!r} and {next_name!r} overlap")
        if prev_hi < next_lo:
            raise ValueError(f"vix_buckets gap between {prev_name!r} and {next_name!r}")

    top = len(parsed) - 1
    return [
        VixBucket(name=name, lo=lo, hi=hi, inclusive_hi=(i == top), params=params)
        for i, (lo, hi, name, params) in enumerate(parsed)
    ]


def get_buckets() -> list[VixBucket]:
    """
    Parse buckets from the live config.

    Read at call time (not cached) so tests that patch CONFIG contents see
    their fixture buckets, mirroring how the call sites read CONFIG today.
    """
    entry = CONFIG.get("entry") or {}
    return parse_buckets(entry.get("vix_buckets"))


def classify(vix: float, buckets: Optional[list[VixBucket]] = None) -> Optional[VixBucket]:
    """
    Map a VIX value to its bucket, or None when VIX is outside all buckets
    (the no-trade zone). Pass `buckets` to avoid re-parsing in a loop.
    """
    if buckets is None:
        buckets = get_buckets()
    for bucket in buckets:
        if bucket.contains(vix):
            return bucket
    return None


# Fail loudly at startup on malformed config (empty is allowed — see module
# docstring). Import of this module happens during engine startup via
# tick_processor / risk_manager.
get_buckets()

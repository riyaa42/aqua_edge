"""Where on the building's pipes a leak is, from each flat's lowest and mean pressure. Used by the
gateway's building check (mass_balance.py) and supply rule (supply.py): it needs every flat's
summaries, so no node can do it alone. How it works and how well: README.md in this folder.
tree: gateway.topology.Topology.
"""


def cross_floor_drops(tree, p_sums, p_base, p_mean_sums, p_mean_base, interval=None):
    """Compute per-floor pressure drop (m) relative to clean baseline (prefers p_mean over p_min)."""
    use_mean = bool(p_mean_sums and p_mean_base)
    sums = p_mean_sums if use_mean else p_sums
    base = p_mean_base if use_mean else p_base
    if not sums or not base:
        return {}
    i = interval if interval is not None else max(sums)
    cur = sums.get(i, {})
    by_floor = {}
    for f, p_now in cur.items():
        section = tree.section_of(f)       # the junction feeding the flat ("floor")
        if f in base and section is not None:
            by_floor.setdefault(section, []).append(base[f] - p_now)
    return {fl: round(sum(vs) / len(vs), 3) for fl, vs in by_floor.items() if vs}


def localize_floor(tree, drops):
    """Confidence-aware localization using the downward headloss gradient along the pipes.
    -> (suspected section or None, sections to check, confidence). A section is the
    junction feeding some flats, named by its label.

    A leak at a junction increases flow (and thus headloss) in all pipe segments upstream
    of it, creating positive pressure-drop steps step[s] = drop[s] - drop[upstream of s]
    down to it, and ~0 step beyond it.
    """
    if not drops or max(drops.values()) < 0.05:
        return None, [], "unlocalised"
    if len(drops) == 1:
        only = next(iter(drops))
        return only, [only], "medium"

    above = {s: tree.upstream_sections(s) for s in drops}          # nearest first
    up = {s: next((a for a in above[s] if a in drops), None) for s in drops}
    steps = {s: round(drops[s] - drops[up[s]], 4) for s in drops if up[s] is not None}
    sig = [s for s in steps if steps[s] >= 0.022]
    deepest = [s for s in sig if not any(s in above[other] for other in sig)]
    if len(deepest) == 1:           # the furthest section with a significant headloss step
        suspected = deepest[0]
        return suspected, [suspected] + [s for s in drops if up[s] == suspected], "high"
    if not sig:
        # No significant step: either a large burst at a junction fed straight from the
        # tank (drop > 0.45 m) or a medium/small leak whose gradient is below burst noise.
        top = [s for s in drops if not above[s] and drops[s] >= 0.45]
        if len(top) == 1:
            return top[0], top + [s for s in drops if above[s][:1] == top], "high"
    # (steps on two separate branches also end up here: one leak cannot be both)
    ranked = sorted(drops.items(), key=lambda kv: -kv[1])
    return None, [s for s, _ in ranked[:2]], "unlocalised"

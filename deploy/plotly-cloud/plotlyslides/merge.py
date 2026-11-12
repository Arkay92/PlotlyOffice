"""Conservative three-way merging: merge independent edits, reject overlaps.

This is not a CRDT. Ambiguous overlapping replacements are surfaced to the user
instead of choosing a winner or silently dropping text.
"""
from difflib import SequenceMatcher


class MergeConflict(ValueError):
    pass


def edits(base, changed):
    if base == changed:
        return []
    prefix = 0
    limit = min(len(base), len(changed))
    while prefix < limit and base[prefix] == changed[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit-prefix and base[len(base)-suffix-1] == changed[len(changed)-suffix-1]:
        suffix += 1
    a, b = base[prefix:len(base)-suffix if suffix else None], changed[prefix:len(changed)-suffix if suffix else None]
    if max(len(a), len(b)) > 100000:
        return [(prefix, len(base)-suffix, b)]
    return [(prefix+i, prefix+j, b[k:l]) for op,i,j,k,l in SequenceMatcher(None,a,b,autojunk=True).get_opcodes() if op != 'equal']


def merge_text(base, current, incoming):
    if incoming == base or incoming == current:
        return current
    if current == base:
        return incoming
    remote, local = edits(base,current), edits(base,incoming)
    combined = list(remote)
    for change in local:
        if change in remote:
            continue
        start,end,text = change
        for other_start,other_end,other_text in remote:
            if start == end == other_start == other_end:
                # Simultaneous plain-text insertion: retain both in server order.
                if any(c in text+other_text for c in '<>'):
                    raise MergeConflict('Both editors changed formatting at the same position.')
                continue
            overlaps = max(start,other_start) < min(end,other_end)
            insertion_inside = (start == end and other_start < start < other_end) or (other_start == other_end and start < other_start < end)
            if overlaps or insertion_inside:
                raise MergeConflict('Another editor changed the same passage. Your draft has been preserved; review the latest version before retrying.')
        combined.append(change)
    combined.sort(key=lambda e:(e[0],e[1]))
    result, pos = [], 0
    for start,end,text in combined:
        result.extend((base[pos:start],text)); pos=end
    result.append(base[pos:])
    return ''.join(result)


def merge_value(base, current, incoming, label):
    if incoming == base or incoming == current:
        return current
    if current == base:
        return incoming
    raise MergeConflict(f'Another editor also changed {label}. Your draft has been preserved.')

def merge_tree(base, current, incoming, label='slide content'):
    """Merge independent object properties; ordered arrays remain atomic."""
    if incoming == base or incoming == current:
        return current
    if current == base:
        return incoming
    if all(isinstance(v, dict) for v in (base, current, incoming)):
        missing = object()
        result = {}
        for key in base.keys() | current.keys() | incoming.keys():
            value = merge_tree(base.get(key, missing), current.get(key, missing), incoming.get(key, missing), label + '/' + key)
            if value is not missing:
                result[key] = value
        return result
    raise MergeConflict(f'Another editor changed {label}. Your draft is preserved. Save a copy or load the latest version.')

"""Conservative recursive three-way merge for sparse workbook data."""


class MergeConflict(ValueError):
    pass


def merge_value(base, current, incoming, label="workbook content"):
    if incoming == base or incoming == current:
        return current
    if current == base:
        return incoming
    if all(isinstance(v, dict) for v in (base, current, incoming)):
        missing = object()
        result = {}
        for key in base.keys() | current.keys() | incoming.keys():
            value = merge_value(
                base.get(key, missing),
                current.get(key, missing),
                incoming.get(key, missing),
                f"{label}/{key}",
            )
            if value is not missing:
                result[key] = value
        return result
    raise MergeConflict(
        f"Another editor changed {label}. Your local draft is preserved; load the latest version or save a copy."
    )

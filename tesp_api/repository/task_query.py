from typing import Any, Dict, List, Optional

from tesp_api.repository.model.task import TesTaskState


def _tag_condition(key: str, value: Optional[str]) -> Dict[str, Any]:
    """
    Matches tasks whose tag ``key`` exists, or equals ``value`` when one is given. Tag values are always strings.
    """
    if "." not in key and not key.startswith("$"):
        return {f"tags.{key}": value if value else {"$type": "string"}}
    # A dotted or $-prefixed key would be read as a path or operator; $getField (MongoDB 5.0) takes it literally.
    tag = {"$getField": {"field": {"$literal": key}, "input": "$tags"}}
    return {"$expr": {"$eq": [tag, value] if value else [{"$type": tag}, "string"]}}


def list_tasks_query(name_prefix: Optional[str] = None,
                     state: Optional[TesTaskState] = None,
                     tag_keys: Optional[List[str]] = None,
                     tag_values: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    MongoDB filter for ListTasks. ``tag_keys`` and ``tag_values`` are paired by position (TES 1.1): a task matches if
    it has every key, with the paired value when that value is given; a missing or empty value matches any value.
    """
    tag_values = tag_values or []
    conditions = []
    if name_prefix:
        conditions.append({'name': {'$regex': f"^{name_prefix}"}})
    if state:
        conditions.append({'state': state})
    conditions.extend(_tag_condition(key, tag_values[i] if i < len(tag_values) else None)
                      for i, key in enumerate(tag_keys or []))
    if len(conditions) > 1:
        return {'$and': conditions}
    return conditions[0] if conditions else {}

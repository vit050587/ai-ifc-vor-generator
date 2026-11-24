"""Names for ARCH selection/grouping; IFC identity is preserved separately."""
import re


def type_name(name):
    return re.sub(r':\s*\d+\s*$', '', str(name or ''))


def semantic_names(value, instance_name):
    """Also normalize old saved scopes with instance names embedded in text."""
    if isinstance(value, str):
        return value.replace(instance_name, type_name(instance_name)) if instance_name else value
    if isinstance(value, list):
        return [semantic_names(v, instance_name) for v in value]
    if isinstance(value, dict):
        return {k: semantic_names(v, instance_name) for k, v in value.items()}
    return value

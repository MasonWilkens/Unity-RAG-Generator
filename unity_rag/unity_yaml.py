"""Unity YAML parsing without object construction or implicit scalar coercion."""
import re
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

HEADER = re.compile(r"^--- !u!(\d+) &(-?\d+)( stripped)?\s*$", re.MULTILINE)


def parse_unity(text, path):
    headers = list(HEADER.finditer(text))
    if not headers:
        raise ValueError("not_unity_text_serialization")
    objects = []
    ids = set()
    for index, match in enumerate(headers):
        file_id = match.group(2)
        if file_id in ids:
            raise ValueError("duplicate_file_id")
        ids.add(file_id)
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body_start = text.find("\n", match.start())
        if body_start < 0:
            raise ValueError("empty_unity_document")
        body = text[body_start + 1:end]
        offset = text[:body_start + 1].count("\n")
        # Reject aliases and bound nesting before composing a node tree.
        depth = count = 0
        for event in yaml.parse(body, Loader=yaml.BaseLoader):
            count += 1
            if isinstance(event, yaml.events.AliasEvent):
                raise ValueError("yaml_aliases_not_supported")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
            if depth > 64 or count > 200000:
                raise ValueError("yaml_complexity_limit")
        node = yaml.compose(body, Loader=yaml.BaseLoader)
        locations = {}
        def convert(current, parts=()):
            locations[".".join(parts)] = {
                "path": path, "start_line": current.start_mark.line + offset + 1,
                "end_line": current.end_mark.line + offset + 1}
            if isinstance(current, ScalarNode):
                return current.value
            if isinstance(current, SequenceNode):
                return [convert(child, (*parts, str(i))) for i, child in enumerate(current.value)]
            if isinstance(current, MappingNode):
                result = {}
                for key, child in current.value:
                    if not isinstance(key, ScalarNode) or key.value in result:
                        raise ValueError("duplicate_or_complex_yaml_key")
                    result[key.value] = convert(child, (*parts, key.value))
                return result
            raise ValueError("unsupported_yaml_node")
        value = convert(node) if node else None
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("invalid_unity_document_root")
        type_name, data = next(iter(value.items()))
        if not isinstance(data, dict):
            raise ValueError("invalid_unity_document_body")
        objects.append({
            "file_id": file_id, "class_id": int(match.group(1)), "type": type_name,
            "stripped": bool(match.group(3)), "data": data,
            "locations": {k[len(type_name) + 1:]: v for k, v in locations.items()
                          if k.startswith(type_name + ".")},
            "location": {"path": path, "start_line": text[:match.start()].count("\n") + 1,
                         "end_line": text[:end].count("\n") + (0 if text[:end].endswith("\n") else 1)}
        })
    return objects


def reference(value):
    return (isinstance(value, dict) and "fileID" in value
            and set(value) <= {"fileID", "guid", "type"})


def walk_values(value, parts=()):
    if reference(value):
        yield ".".join(parts), value, True
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from walk_values(child, (*parts, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_values(child, (*parts, str(index)))
    else:
        yield ".".join(parts), value, False

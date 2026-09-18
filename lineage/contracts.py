"""Output contracts and structural checks; never interpret ETL source."""

def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields),
            "additionalProperties": False}


def arr(items):
    return {"type": "array", "items": items}


def enum(*values):
    return {"type": "string", "enum": list(values)}


S = {"type": "string"}
I = {"type": "integer", "minimum": 1}
B = {"type": "boolean"}
EVIDENCE = obj(path=S, start_line=I, end_line=I, quote=S)
COLUMN = obj(name=S, ordinal=I, evidence=arr(EVIDENCE))
DISCOVERY = obj(
    target_schema=S, target_table=S, columns=arr(COLUMN),
    enumeration_complete=B, build_context=S, entrypoints=arr(EVIDENCE),
    alternatives=arr(S), unresolved=arr(S))
NODE = obj(
    id=S, schema=S, table=S, column=S, scope=S,
    source_type=enum("physical", "view", "temp", "cte", "dataframe", "file",
                     "literal", "runtime", "unresolved"),
    terminal=enum("expanded", "repo_boundary", "literal", "runtime", "unresolved", "cycle"),
    boundary_reason=S, search_notes=arr(S), evidence=arr(EVIDENCE))
KINDS = ("value", "join", "filter", "group", "window", "order", "control")
EDGE = obj(source_id=S, target_id=S, dependency_kind=enum(*KINDS),
           expression=S, business_rule=S, evidence=arr(EVIDENCE))
MAPPING = obj(source_id=S, dependency_kind=enum(*KINDS), expression=S,
              business_rule=S, evidence=arr(EVIDENCE))
TRACE = obj(target_column=S, root_id=S, nodes=arr(NODE), edges=arr(EDGE),
            mappings=arr(MAPPING), unresolved=arr(S))
REVIEW = obj(accepted=B, issues=arr(S), evidence=arr(EVIDENCE))


def validate(value, schema, path="$"):
    """Validate our deliberately small, closed JSON Schema vocabulary."""
    kind = schema["type"]
    valid = {"object": lambda: isinstance(value, dict),
             "array": lambda: isinstance(value, list),
             "string": lambda: isinstance(value, str),
             "integer": lambda: type(value) is int,
             "boolean": lambda: type(value) is bool}[kind]()
    if not valid:
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unexpected value {value!r}")
    if kind == "integer" and value < schema.get("minimum", value):
        raise ValueError(f"{path}: below minimum")
    if kind == "object":
        if set(value) != set(schema["properties"]):
            raise ValueError(f"{path}: keys differ from contract")
        for key, sub in schema["properties"].items():
            validate(value[key], sub, f"{path}.{key}")
    if kind == "array":
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]")

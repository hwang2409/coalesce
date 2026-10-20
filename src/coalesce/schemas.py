"""Public schemas for the callable MCP tool surface."""

from __future__ import annotations

QUANTITY = {
    "type": "object",
    "properties": {"value": {"type": "number"}, "unit": {"const": "mm"}},
    "required": ["value", "unit"],
    "additionalProperties": False,
}
ANGLE = {
    "type": "object",
    "properties": {
        "value": {"type": "number"},
        "unit": {"enum": ["deg", "rad"]},
    },
    "required": ["value", "unit"],
    "additionalProperties": False,
}
POINT_2D = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 2,
}
POINT_3D = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 3,
    "maxItems": 3,
}
RECTANGLE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "kind": {"const": "rectangle"},
        "center": POINT_2D,
        "width": QUANTITY,
        "height": QUANTITY,
        "rotation": ANGLE,
    },
    "required": ["id", "kind", "width", "height"],
    "additionalProperties": False,
}
CIRCLE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "kind": {"const": "circle"},
        "center": POINT_2D,
        "radius": QUANTITY,
        "rotation": ANGLE,
    },
    "required": ["id", "kind", "radius"],
    "additionalProperties": False,
}
SKETCH_ENTITY = {"oneOf": [RECTANGLE, CIRCLE]}
DIRECTION = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 3,
    "maxItems": 3,
}
REFERENCE_SOURCE = {
    "oneOf": [
        {"type": "string"},
        {
            "type": "object",
            "properties": {
                "kind": {"const": "sketch_segment"},
                "sketch": {"type": "string"},
                "entity": {"type": "string"},
                "segment": {"type": "string"},
            },
            "required": ["kind", "sketch", "entity", "segment"],
            "additionalProperties": False,
        },
        {"type": "null"},
    ]
}
REFERENCE_BODY = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "body_source"},
                "feature": {"type": "string"},
                "entity": {"type": "string"},
            },
            "required": ["kind", "feature", "entity"],
            "additionalProperties": False,
        },
        {"type": "null"},
    ]
}
REFERENCE_FILTER = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"enum": ["Normal", "MaxAlong"]},
                "direction": DIRECTION,
                "tolerance": {"type": "number"},
            },
            "required": ["kind", "direction"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "Position"},
                "direction": DIRECTION,
                "value": {"type": "number"},
                "tolerance": {"type": "number"},
            },
            "required": ["kind", "direction", "value"],
            "additionalProperties": False,
        },
    ]
}
REFERENCE = {
    "type": "object",
    "properties": {
        "feature": {"type": "string"},
        "role": {"type": "string"},
        "cardinality": {"enum": ["one", "one_or_more", "exactly", "any"]},
        "count": {"type": ["integer", "null"], "minimum": 0},
        "source": REFERENCE_SOURCE,
        "body": REFERENCE_BODY,
        "filters": {"type": "array", "items": REFERENCE_FILTER},
    },
    "required": ["feature", "role"],
    "additionalProperties": False,
}

MUTATION_PROPERTIES = {
    "expectedRevision": {"type": "integer", "minimum": 0},
    "requestId": {"type": "string", "minLength": 1},
}
SKETCH_PROPERTIES = {
    **MUTATION_PROPERTIES,
    "sketchId": {"type": "string", "minLength": 1},
}
SKETCH_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "create"},
        **SKETCH_PROPERTIES,
        "plane": {
            "type": "object",
            "properties": {
                "kind": {"const": "origin"},
                "name": {"enum": ["XY", "XZ", "YZ"]},
                "offset": QUANTITY,
            },
            "required": ["kind", "name"],
            "additionalProperties": False,
        },
        "entities": {"type": "array", "items": SKETCH_ENTITY, "minItems": 1},
    },
    "required": ["action", *SKETCH_PROPERTIES, "plane", "entities"],
    "additionalProperties": False,
}
SKETCH_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "update"},
        **SKETCH_PROPERTIES,
        "set": {
            "type": "object",
            "properties": {
                "position": POINT_3D,
                "entities": {"type": "array", "items": SKETCH_ENTITY, "minItems": 1},
            },
            "additionalProperties": False,
            "minProperties": 1,
        },
    },
    "required": ["action", *SKETCH_PROPERTIES, "set"],
    "additionalProperties": False,
}
SKETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "enum": ["create", "update"],
            "description": "Use create with plane and entities; use update with set.",
        },
        **SKETCH_PROPERTIES,
        "plane": {
            **SKETCH_CREATE_SCHEMA["properties"]["plane"],
            "description": "Required for create; not allowed for update.",
        },
        "entities": {
            **SKETCH_CREATE_SCHEMA["properties"]["entities"],
            "description": "Required for create; not allowed for update.",
        },
        "set": {
            **SKETCH_UPDATE_SCHEMA["properties"]["set"],
            "description": "Required for update; not allowed for create.",
        },
    },
    "required": ["action", *SKETCH_PROPERTIES],
    "additionalProperties": False,
}

PROFILE = {
    "type": "object",
    "properties": {
        "sketchId": {"type": "string"},
        "profileIds": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["sketchId", "profileIds"],
    "additionalProperties": False,
}
EXTRUDE_PARAMETERS = {
    "type": "object",
    "properties": {
        "profile": PROFILE,
        "distance": QUANTITY,
        "operation": {"const": "new_body"},
        "direction": {"const": "normal"},
    },
    "required": ["profile", "distance", "operation", "direction"],
    "additionalProperties": False,
}
FEATURE_TARGET = {
    "type": "object",
    "properties": {"featureId": {"type": "string", "minLength": 1}},
    "required": ["featureId"],
    "additionalProperties": False,
}
SKETCH_TOOL = {
    "type": "object",
    "properties": {"sketchId": {"type": "string", "minLength": 1}},
    "required": ["sketchId"],
    "additionalProperties": False,
}
CUT_DEPTH = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"kind": {"const": "through_all"}},
            "required": ["kind"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"kind": {"const": "distance"}, "distance": QUANTITY},
            "required": ["kind", "distance"],
            "additionalProperties": False,
        },
    ]
}
CUT_PARAMETERS = {
    "type": "object",
    "properties": {
        "target": FEATURE_TARGET,
        "tool": SKETCH_TOOL,
        "depth": CUT_DEPTH,
        "flip": {"type": "boolean"},
    },
    "required": ["target", "tool", "depth", "flip"],
    "additionalProperties": False,
}
HOLE_PARAMETERS = {
    "type": "object",
    "properties": {
        "target": FEATURE_TARGET,
        "diameter": QUANTITY,
        "depth": QUANTITY,
        "position": POINT_2D,
    },
    "required": ["target", "diameter", "position"],
    "additionalProperties": False,
}
FEATURE_PARAMETERS = {"oneOf": [EXTRUDE_PARAMETERS, CUT_PARAMETERS, HOLE_PARAMETERS]}
FEATURE_PROPERTIES = {
    **MUTATION_PROPERTIES,
    "featureId": {"type": "string", "minLength": 1},
    "kind": {"enum": ["extrude", "cut", "hole"]},
}
FEATURE_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "create"},
        **FEATURE_PROPERTIES,
        "parameters": FEATURE_PARAMETERS,
    },
    "required": ["action", *FEATURE_PROPERTIES, "parameters"],
    "additionalProperties": False,
}
FEATURE_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "update"},
        **FEATURE_PROPERTIES,
        "set": {
            "type": "object",
            "properties": {"distance": QUANTITY},
            "required": ["distance"],
            "additionalProperties": False,
        },
    },
    "required": ["action", *FEATURE_PROPERTIES, "set"],
    "additionalProperties": False,
}
FEATURE_ACTION_SCHEMAS = {
    ("create", kind): {
        **FEATURE_CREATE_SCHEMA,
        "properties": {
            **FEATURE_CREATE_SCHEMA["properties"],
            "kind": {"const": kind},
            "parameters": parameters,
        },
    }
    for kind, parameters in {
        "extrude": EXTRUDE_PARAMETERS,
        "cut": CUT_PARAMETERS,
        "hole": HOLE_PARAMETERS,
    }.items()
}
FEATURE_ACTION_SCHEMAS[("update", "extrude")] = {
    **FEATURE_UPDATE_SCHEMA,
    "properties": {
        **FEATURE_UPDATE_SCHEMA["properties"],
        "kind": {"const": "extrude"},
    },
}
FEATURE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "enum": ["create", "update"],
            "description": "Use create with parameters; use update with set.",
        },
        **FEATURE_PROPERTIES,
        "parameters": {
            **FEATURE_CREATE_SCHEMA["properties"]["parameters"],
            "description": "Required for create; not allowed for update.",
        },
        "set": {
            **FEATURE_UPDATE_SCHEMA["properties"]["set"],
            "description": "Required for update; not allowed for create.",
        },
    },
    "required": ["action", *FEATURE_PROPERTIES],
    "additionalProperties": False,
}
QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "revision": {"type": "integer", "minimum": 0},
        "select": {
            "type": "object",
            "properties": {
                "kind": {
                    "enum": [
                        "document",
                        "graph",
                        "revision",
                        "timeline",
                        "measurements",
                        "feature",
                        "reference",
                        "server",
                    ]
                },
                "id": {"type": "string", "minLength": 1},
                "reference": REFERENCE,
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    "required": ["select"],
    "additionalProperties": False,
}
QUERY_FEATURE_SCHEMA = {
    **QUERY_SCHEMA,
    "properties": {
        **QUERY_SCHEMA["properties"],
        "select": {
            **QUERY_SCHEMA["properties"]["select"],
            "required": ["kind", "id"],
        },
    },
}
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "evaluate"},
        "revision": {"type": "integer", "minimum": 0},
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"const": "valid_brep"},
                },
                "required": ["id", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

TOOL_SCHEMAS = {
    "sketch": SKETCH_SCHEMA,
    "feature": FEATURE_SCHEMA,
    "query": QUERY_SCHEMA,
    "check": CHECK_SCHEMA,
}

ACTION_SCHEMAS = {
    "sketch": {"create": SKETCH_CREATE_SCHEMA, "update": SKETCH_UPDATE_SCHEMA},
    "feature": {"create": FEATURE_CREATE_SCHEMA, "update": FEATURE_UPDATE_SCHEMA},
}

"""Public MCP schemas for the slice-one tool surface."""

from __future__ import annotations

from copy import deepcopy

QUANTITY = {
    "type": "object",
    "properties": {"value": {"type": "number"}, "unit": {"type": "string"}},
    "required": ["value", "unit"],
    "additionalProperties": False,
}
ANGLE = deepcopy(QUANTITY)
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

SKETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["create", "update"]},
        "expectedRevision": {"type": "integer", "minimum": 0},
        "requestId": {"type": "string", "minLength": 1},
        "sketchId": {"type": "string", "minLength": 1},
        "partId": {"type": "string", "minLength": 1},
        "plane": {
            "type": "object",
            "properties": {"kind": {"const": "origin"}, "name": {"const": "XY"}},
            "required": ["kind", "name"],
            "additionalProperties": False,
        },
        "entities": {"type": "array", "items": RECTANGLE, "minItems": 1},
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"const": "coincident"},
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                    },
                },
                "required": ["id", "kind", "entities"],
                "additionalProperties": False,
            },
        },
        "set": {
            "type": "object",
            "properties": {
                "position": POINT_3D,
                "rotation": POINT_3D,
                "entities": {"type": "array", "items": RECTANGLE, "minItems": 1},
            },
            "additionalProperties": False,
        },
    },
    "required": ["action", "expectedRevision", "requestId", "sketchId"],
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
FEATURE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["create", "update"]},
        "expectedRevision": {"type": "integer", "minimum": 0},
        "requestId": {"type": "string", "minLength": 1},
        "featureId": {"type": "string", "minLength": 1},
        "partId": {"type": "string", "minLength": 1},
        "kind": {"enum": ["extrude", "boolean", "hole", "fillet"]},
        "parameters": EXTRUDE_PARAMETERS,
        "set": {
            "type": "object",
            "properties": {"distance": QUANTITY},
            "additionalProperties": False,
        },
    },
    "required": ["action", "expectedRevision", "requestId", "featureId", "kind"],
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
                        "reference",
                    ]
                },
                "ids": {"type": "array", "items": {"type": "string"}},
                "reference": REFERENCE,
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    "required": ["select"],
    "additionalProperties": False,
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

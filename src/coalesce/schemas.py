"""Public schemas for the callable MCP tool surface."""

from __future__ import annotations

from .constrained_sketch import CONSTRAINED_SKETCH_SCHEMA
from .graph import MAX_HOLE_COUNT, MAX_OUTLINE_POINTS, MIN_OUTLINE_POINTS
from .requirements import SUPPORTED_REQUIREMENT_KINDS as EVALUATED_REQUIREMENT_KINDS

SUPPORTED_REQUIREMENT_KINDS = tuple(sorted(("valid_brep", *EVALUATED_REQUIREMENT_KINDS)))

QUANTITY = {
    "type": "object",
    "properties": {"value": {"type": "number"}, "unit": {"const": "mm"}},
    "required": ["value", "unit"],
    "additionalProperties": False,
}
POSITIVE_QUANTITY = {
    **QUANTITY,
    "properties": {
        **QUANTITY["properties"],
        "value": {"type": "number", "exclusiveMinimum": 0},
    },
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
    "oneOf": [
        {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 2,
            "maxItems": 2,
        },
        {
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": "^[A-Za-z0-9_]+$"},
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
            "required": ["id", "x", "y"],
            "additionalProperties": False,
        },
    ]
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


def _outline_entity(kind: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "kind": {"const": kind},
            "points": {
                "type": "array",
                "items": POINT_2D,
                "minItems": MIN_OUTLINE_POINTS,
                "maxItems": MAX_OUTLINE_POINTS,
                "description": "Use explicit point ids when an outline may be edited or referenced; array-point identity follows list position.",
            },
            "closed": {"const": True},
        },
        "required": ["kind", "points", "closed"],
        "additionalProperties": False,
    }


POLYLINE = _outline_entity("polyline")
SPLINE = _outline_entity("spline")
SKETCH_ENTITY = {"oneOf": [RECTANGLE, CIRCLE, POLYLINE, SPLINE]}
CONSTRAINED_SKETCH = {
    **CONSTRAINED_SKETCH_SCHEMA,
    "description": (
        "Use dimensions for editable values. Fixing every point can report fully_constrained "
        "with zero dof but no editable driving dimensions"
    ),
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
        {
            "type": "object",
            "properties": {
                "kind": {"const": "hole_surface"},
                "feature": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "surface": {"enum": ["hole_wall", "hole_floor"]},
            },
            "required": ["kind", "feature", "name", "surface"],
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
    "expectedRevision": {
        "type": "integer",
        "minimum": 0,
        "description": "Chain each returned revision",
    },
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
        "constrained": CONSTRAINED_SKETCH,
    },
    "required": ["action", *SKETCH_PROPERTIES, "plane"],
    "oneOf": [{"required": ["entities"]}, {"required": ["constrained"]}],
    "additionalProperties": False,
}
SKETCH_DELETE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "delete"},
        **SKETCH_PROPERTIES,
        "cascade": {"type": "boolean", "default": False},
    },
    "required": ["action", *SKETCH_PROPERTIES],
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
                "constrained": CONSTRAINED_SKETCH,
                "dimensionValues": {
                    "type": "object",
                    "description": "Update driving dimensions by stable id.",
                    "additionalProperties": {"type": "number"},
                    "minProperties": 1,
                },
            },
            "additionalProperties": False,
            "minProperties": 1,
            "not": {
                "anyOf": [
                    {"required": ["entities", "constrained"]},
                    {"required": ["dimensionValues", "position"]},
                    {"required": ["dimensionValues", "entities"]},
                    {"required": ["dimensionValues", "constrained"]},
                ]
            },
        },
    },
    "required": ["action", *SKETCH_PROPERTIES, "set"],
    "additionalProperties": False,
}
SKETCH_EXAMPLES = [
    {
        "action": "create",
        "expectedRevision": 0,
        "requestId": "create-profile",
        "sketchId": "profile",
        "plane": {"kind": "origin", "name": "XY"},
        "constrained": {
            "kind": "constrained",
            "points": [
                {"id": "p0", "x": 0, "y": 0},
                {"id": "p1", "x": 40, "y": 0},
                {"id": "p2", "x": 40, "y": 30},
                {"id": "p3", "x": 0, "y": 30},
                {"id": "c", "x": 20, "y": 15},
            ],
            "lines": [
                {"id": "e0", "start": "p0", "end": "p1"},
                {"id": "e1", "start": "p1", "end": "p2"},
                {"id": "e2", "start": "p2", "end": "p3"},
                {"id": "e3", "start": "p3", "end": "p0"},
            ],
            "circles": [{"id": "opening", "center": "c", "radius": 4}],
            "dimensions": [
                {"id": "width", "kind": "distance", "entities": ["p0", "p1"], "value": 40},
                {"id": "height", "kind": "distance", "entities": ["p0", "p3"], "value": 30},
                {"id": "center-left", "kind": "distance", "entities": ["p0", "c"], "value": 25},
                {"id": "center-right", "kind": "distance", "entities": ["p1", "c"], "value": 25},
                {"id": "opening-radius", "kind": "radius", "entities": ["opening"], "value": 4},
            ],
            "constraints": [
                {"id": "anchor", "kind": "fix", "entities": ["p0"]},
                {"id": "bottom", "kind": "horizontal", "entities": ["e0"]},
                {"id": "right", "kind": "vertical", "entities": ["e1"]},
                {"id": "top", "kind": "horizontal", "entities": ["e2"]},
                {"id": "left", "kind": "vertical", "entities": ["e3"]},
            ],
        },
    },
    {
        "action": "update",
        "expectedRevision": 2,
        "requestId": "resize-opening",
        "sketchId": "profile",
        "set": {"dimensionValues": {"opening-radius": 4}},
    },
]

SKETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "enum": ["create", "update", "delete"],
            "description": "Create, update, or delete a sketch. Delete fails when features use it unless cascade is true.",
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
        "constrained": CONSTRAINED_SKETCH,
        "set": {
            **SKETCH_UPDATE_SCHEMA["properties"]["set"],
            "description": "Required for update; not allowed for create or delete.",
        },
        "cascade": {"type": "boolean", "default": False},
    },
    "required": ["action", *SKETCH_PROPERTIES],
    "additionalProperties": False,
    "examples": SKETCH_EXAMPLES,
}

PROFILE = {
    "type": "object",
    "properties": {
        "sketchId": {"type": "string"},
        "profileIds": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "uniqueItems": True,
            "description": "optional: provide a unique subset of known profile ids; omit to use all closed loops in graph order; constrained sketches resolve to [sketchId] (inner loops become holes); revolve requires selecting one outer loop only",
        },
    },
    "required": ["sketchId"],
    "additionalProperties": False,
}
REVOLVE_PARAMETERS = {
    "type": "object",
    "properties": {
        "profile": PROFILE,
        "axis": {"enum": ["x", "y"]},
        "angle": {"const": {"value": 360, "unit": "deg"}},
        "operation": {"const": "new_body"},
    },
    "required": ["profile", "axis", "angle", "operation"],
    "additionalProperties": False,
}
EXTRUDE_PARAMETERS = {
    "type": "object",
    "properties": {
        "profile": PROFILE,
        "distance": QUANTITY,
        "operation": {"enum": ["new_body", "join"]},
        "target": {
            "type": "object",
            "properties": {"featureId": {"type": "string", "minLength": 1}},
            "required": ["featureId"],
            "additionalProperties": False,
        },
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
HOLE_POSITION = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Stable caller-chosen identity; defaults to h1..hn in request order.",
        },
        "point": POINT_2D,
    },
    "required": ["point"],
    "additionalProperties": False,
}
EDGE_TREATMENT_PROPERTIES = {
    "target": FEATURE_TARGET,
    "edges": {"type": "array", "items": REFERENCE, "minItems": 1},
}
FILLET_PARAMETERS = {
    "type": "object",
    "properties": {**EDGE_TREATMENT_PROPERTIES, "radius": POSITIVE_QUANTITY},
    "required": ["target", "edges", "radius"],
    "additionalProperties": False,
}
CHAMFER_PARAMETERS = {
    "type": "object",
    "properties": {**EDGE_TREATMENT_PROPERTIES, "distance": POSITIVE_QUANTITY},
    "required": ["target", "edges", "distance"],
    "additionalProperties": False,
}
HOLE_COUNT = {
    "type": "integer",
    "description": f"Member count from 1 through {MAX_HOLE_COUNT}.",
}
HOLE_PATTERN_COMMON = {
    "count": HOLE_COUNT,
    "namePrefix": {"type": "string", "minLength": 1},
}
CIRCULAR_HOLE_PATTERN = {
    "type": "object",
    "properties": {
        "kind": {"const": "circular"},
        "center": POINT_2D,
        "radius": QUANTITY,
        "startAngle": ANGLE,
        **HOLE_PATTERN_COMMON,
    },
    "required": ["kind", "center", "radius", "count", "startAngle", "namePrefix"],
    "additionalProperties": False,
}
LINEAR_HOLE_PATTERN = {
    "type": "object",
    "properties": {
        "kind": {"const": "linear"},
        "start": POINT_2D,
        "direction": POINT_2D,
        "pitch": QUANTITY,
        **HOLE_PATTERN_COMMON,
    },
    "required": ["kind", "start", "direction", "pitch", "count", "namePrefix"],
    "additionalProperties": False,
}
HOLE_PATTERN = {"oneOf": [CIRCULAR_HOLE_PATTERN, LINEAR_HOLE_PATTERN]}
HOLE_PARAMETERS = {
    "type": "object",
    "properties": {
        "target": FEATURE_TARGET,
        "diameter": QUANTITY,
        "depth": QUANTITY,
        "position": POINT_2D,
        "positions": {
            "type": "array",
            "items": HOLE_POSITION,
            "minItems": 1,
            "maxItems": MAX_HOLE_COUNT,
            "description": "Named XY positions in mm; names are caller identities and default to h1..hn.",
        },
        "pattern": HOLE_PATTERN,
    },
    "required": ["target", "diameter"],
    "oneOf": [
        {"required": ["position"]},
        {"required": ["positions"]},
        {"required": ["pattern"]},
    ],
    "additionalProperties": False,
}
PCB_BASE_PARAMETERS = {
    "type": "object",
    "properties": {
        "board": FEATURE_TARGET,
        "wallThickness": POSITIVE_QUANTITY,
        "floorThickness": POSITIVE_QUANTITY,
        "clearance": POSITIVE_QUANTITY,
        "wallHeightAboveBoard": POSITIVE_QUANTITY,
        "boardSeatHeight": POSITIVE_QUANTITY,
    },
    "required": [
        "board",
        "wallThickness",
        "floorThickness",
        "clearance",
        "wallHeightAboveBoard",
        "boardSeatHeight",
    ],
    "additionalProperties": False,
}
STANDOFF_SELECTION = {
    "type": "object",
    "properties": {
        "mode": {"enum": ["explicit", "all_mounting"]},
        "holeIds": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
        },
    },
    "required": ["mode"],
    "oneOf": [
        {
            "properties": {"mode": {"const": "explicit"}},
            "required": ["holeIds"],
        },
        {
            "properties": {"mode": {"const": "all_mounting"}},
            "not": {"required": ["holeIds"]},
        },
    ],
    "additionalProperties": False,
}
PCB_LIP = {
    "type": "object",
    "properties": {"depth": POSITIVE_QUANTITY, "width": POSITIVE_QUANTITY},
    "required": ["depth", "width"],
    "additionalProperties": False,
}
PCB_LID_PARAMETERS = {
    "type": "object",
    "properties": {
        "base": FEATURE_TARGET,
        "thickness": POSITIVE_QUANTITY,
        "fitGap": POSITIVE_QUANTITY,
        "lip": PCB_LIP,
    },
    "required": ["base", "thickness", "fitGap"],
    "additionalProperties": False,
}
PORT_ENVELOPE = {
    "type": "object",
    "properties": {
        "source": {"enum": ["courtyard", "explicit"]},
        "width": POSITIVE_QUANTITY,
        "height": POSITIVE_QUANTITY,
        "bottomOffset": QUANTITY,
    },
    "required": ["source", "height", "bottomOffset"],
    "oneOf": [
        {
            "properties": {"source": {"const": "courtyard"}},
            "not": {"required": ["width"]},
        },
        {
            "properties": {"source": {"const": "explicit"}},
            "required": ["width"],
        },
    ],
    "additionalProperties": False,
}
PORT_CUTOUT_PARAMETERS = {
    "type": "object",
    "properties": {
        "base": FEATURE_TARGET,
        "footprint": {"type": "string", "minLength": 1},
        "wall": {"enum": ["auto", "left", "right", "front", "back"]},
        "clearance": POSITIVE_QUANTITY,
        "envelope": PORT_ENVELOPE,
    },
    "required": ["base", "footprint", "wall", "clearance", "envelope"],
    "additionalProperties": False,
}
SCREW_POINT = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "point": POINT_2D,
    },
    "required": ["name", "point"],
    "additionalProperties": False,
}
SCREW_CLOSURE_PARAMETERS = {
    "type": "object",
    "properties": {
        "base": FEATURE_TARGET,
        "lid": FEATURE_TARGET,
        "points": {
            "type": "array",
            "items": SCREW_POINT,
            "minItems": 2,
            "maxItems": 8,
        },
        "basePilotDiameter": POSITIVE_QUANTITY,
        "bossDiameter": POSITIVE_QUANTITY,
        "lidClearanceDiameter": POSITIVE_QUANTITY,
    },
    "required": [
        "base",
        "lid",
        "points",
        "basePilotDiameter",
        "bossDiameter",
        "lidClearanceDiameter",
    ],
    "additionalProperties": False,
}
STANDOFFS_PARAMETERS = {
    "type": "object",
    "properties": {
        "target": FEATURE_TARGET,
        "board": FEATURE_TARGET,
        "selection": STANDOFF_SELECTION,
        "bossDiameter": POSITIVE_QUANTITY,
        "pilotDiameter": POSITIVE_QUANTITY,
    },
    "required": ["target", "board", "selection", "bossDiameter", "pilotDiameter"],
    "additionalProperties": False,
}
FEATURE_KIND_SCHEMAS = {
    "extrude": EXTRUDE_PARAMETERS,
    "revolve": REVOLVE_PARAMETERS,
    "cut": CUT_PARAMETERS,
    "hole": HOLE_PARAMETERS,
    "fillet": FILLET_PARAMETERS,
    "chamfer": CHAMFER_PARAMETERS,
    "pcb_base": PCB_BASE_PARAMETERS,
    "standoffs": STANDOFFS_PARAMETERS,
    "pcb_lid": PCB_LID_PARAMETERS,
    "port_cutout": PORT_CUTOUT_PARAMETERS,
    "screw_closure": SCREW_CLOSURE_PARAMETERS,
}
FEATURE_PARAMETERS = {"oneOf": list(FEATURE_KIND_SCHEMAS.values())}
FEATURE_PROPERTIES = {
    **MUTATION_PROPERTIES,
    "featureId": {"type": "string", "minLength": 1},
    "kind": {"enum": list(FEATURE_KIND_SCHEMAS)},
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
EXTRUDE_UPDATE_SET = {
    "type": "object",
    "properties": {"distance": POSITIVE_QUANTITY},
    "required": ["distance"],
    "additionalProperties": False,
}
FILLET_UPDATE_SET = {
    "type": "object",
    "properties": {"radius": POSITIVE_QUANTITY},
    "required": ["radius"],
    "additionalProperties": False,
}
CHAMFER_UPDATE_SET = {
    "type": "object",
    "properties": {"distance": POSITIVE_QUANTITY},
    "required": ["distance"],
    "additionalProperties": False,
}
TARGET_UPDATE_SET = {
    "type": "object",
    "properties": {"target": FEATURE_TARGET},
    "required": ["target"],
    "additionalProperties": False,
}
CIRCULAR_HOLE_PATTERN_UPDATE = {
    "type": "object",
    "properties": CIRCULAR_HOLE_PATTERN["properties"],
    "required": ["kind"],
    "minProperties": 2,
    "additionalProperties": False,
}
LINEAR_HOLE_PATTERN_UPDATE = {
    "type": "object",
    "properties": LINEAR_HOLE_PATTERN["properties"],
    "required": ["kind"],
    "minProperties": 2,
    "additionalProperties": False,
}
HOLE_UPDATE_SET = {
    "type": "object",
    "properties": {
        "target": FEATURE_TARGET,
        "pattern": {"oneOf": [CIRCULAR_HOLE_PATTERN_UPDATE, LINEAR_HOLE_PATTERN_UPDATE]},
    },
    "minProperties": 1,
    "additionalProperties": False,
}
PCB_BASE_UPDATE_SET = {
    "type": "object",
    "properties": {
        "wallThickness": POSITIVE_QUANTITY,
        "floorThickness": POSITIVE_QUANTITY,
        "clearance": POSITIVE_QUANTITY,
        "wallHeightAboveBoard": POSITIVE_QUANTITY,
        "boardSeatHeight": POSITIVE_QUANTITY,
    },
    "minProperties": 1,
    "additionalProperties": False,
}
STANDOFFS_UPDATE_SET = {
    "type": "object",
    "properties": {
        "bossDiameter": POSITIVE_QUANTITY,
        "pilotDiameter": POSITIVE_QUANTITY,
    },
    "minProperties": 1,
    "additionalProperties": False,
}
PORT_CUTOUT_ROOT_UPDATE_SET = {
    "type": "object",
    "properties": {
        "footprint": {"type": "string", "minLength": 1},
        "wall": {"enum": ["auto", "left", "right", "front", "back"]},
        "envelope": PORT_ENVELOPE,
    },
    "minProperties": 1,
    "additionalProperties": False,
}
PORT_CUTOUT_UPDATE_SET = {
    "type": "object",
    "properties": {
        **PORT_CUTOUT_ROOT_UPDATE_SET["properties"],
        "clearance": POSITIVE_QUANTITY,
    },
    "minProperties": 1,
    "additionalProperties": False,
}
SCREW_CLOSURE_UPDATE_SET = {
    "type": "object",
    "properties": {
        "points": SCREW_CLOSURE_PARAMETERS["properties"]["points"],
        "basePilotDiameter": POSITIVE_QUANTITY,
        "bossDiameter": POSITIVE_QUANTITY,
        "lidClearanceDiameter": POSITIVE_QUANTITY,
    },
    "minProperties": 1,
    "additionalProperties": False,
}
ENCLOSURE_BOSS_UPDATE_SET = {
    "type": "object",
    "properties": {
        **STANDOFFS_UPDATE_SET["properties"],
        **SCREW_CLOSURE_UPDATE_SET["properties"],
    },
    "minProperties": 1,
    "additionalProperties": False,
}
PCB_LID_UPDATE_SET = {
    "type": "object",
    "properties": {
        "thickness": POSITIVE_QUANTITY,
        "fitGap": POSITIVE_QUANTITY,
        "lip": PCB_LIP,
        "outlineSource": FEATURE_TARGET,
    },
    "minProperties": 1,
    "additionalProperties": False,
}
ASSEMBLY_TRANSFORM = {
    "type": "object",
    "properties": {
        "translation": {
            "type": "object",
            "properties": {
                "value": {
                    "type": "array",
                    "prefixItems": [{"type": "number"}] * 3,
                    "items": False,
                    "minItems": 3,
                    "maxItems": 3,
                },
                "unit": {"const": "mm"},
            },
            "required": ["value", "unit"],
            "additionalProperties": False,
        },
        "rotation": {
            "type": "object",
            "properties": {
                "axis": {
                    "type": "array",
                    "prefixItems": [{"type": "number"}] * 3,
                    "items": False,
                    "minItems": 3,
                    "maxItems": 3,
                },
                "angle": ANGLE,
            },
            "required": ["axis", "angle"],
            "additionalProperties": False,
        },
    },
    "required": ["translation", "rotation"],
    "additionalProperties": False,
}
ASSEMBLY_OCCURRENCE_PROPERTIES = {
    **MUTATION_PROPERTIES,
    "kind": {"const": "assembly_occurrence"},
    "occurrenceId": {"type": "string", "minLength": 1, "maxLength": 128},
}
ASSEMBLY_OCCURRENCE_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "create"},
        **ASSEMBLY_OCCURRENCE_PROPERTIES,
        "parameters": {
            "type": "object",
            "properties": {
                "partId": {"type": "string", "minLength": 1},
                "transform": ASSEMBLY_TRANSFORM,
            },
            "required": ["partId", "transform"],
            "additionalProperties": False,
        },
    },
    "required": ["action", "kind", *MUTATION_PROPERTIES, "occurrenceId", "parameters"],
    "additionalProperties": False,
}
ASSEMBLY_OCCURRENCE_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "update"},
        **ASSEMBLY_OCCURRENCE_PROPERTIES,
        "set": {
            "type": "object",
            "properties": {"transform": ASSEMBLY_TRANSFORM},
            "required": ["transform"],
            "additionalProperties": False,
        },
    },
    "required": ["action", "kind", *MUTATION_PROPERTIES, "occurrenceId", "set"],
    "additionalProperties": False,
}
ASSEMBLY_OCCURRENCE_DELETE_SCHEMA = {
    "type": "object",
    "properties": {"action": {"const": "delete"}, **ASSEMBLY_OCCURRENCE_PROPERTIES},
    "required": ["action", "kind", *MUTATION_PROPERTIES, "occurrenceId"],
    "additionalProperties": False,
}

FEATURE_DELETE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "delete"},
        **MUTATION_PROPERTIES,
        "featureId": FEATURE_PROPERTIES["featureId"],
        "cascade": {"type": "boolean", "default": False},
    },
    "required": ["action", *MUTATION_PROPERTIES, "featureId"],
    "additionalProperties": False,
}
FEATURE_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "update"},
        **FEATURE_PROPERTIES,
        "set": {
            "oneOf": [
                EXTRUDE_UPDATE_SET,
                HOLE_UPDATE_SET,
                FILLET_UPDATE_SET,
                PCB_BASE_UPDATE_SET,
                ENCLOSURE_BOSS_UPDATE_SET,
                PCB_LID_UPDATE_SET,
                PORT_CUTOUT_ROOT_UPDATE_SET,
            ]
        },
    },
    "required": ["action", *MUTATION_PROPERTIES, "featureId", "set"],
    "additionalProperties": False,
}
FEATURE_ACTION_SCHEMAS = {
    ("delete", None): FEATURE_DELETE_SCHEMA,
    ("create", "assembly_occurrence"): ASSEMBLY_OCCURRENCE_CREATE_SCHEMA,
    ("update", "assembly_occurrence"): ASSEMBLY_OCCURRENCE_UPDATE_SCHEMA,
    ("delete", "assembly_occurrence"): ASSEMBLY_OCCURRENCE_DELETE_SCHEMA,
    **{
        ("create", kind): {
            **FEATURE_CREATE_SCHEMA,
            "properties": {
                **FEATURE_CREATE_SCHEMA["properties"],
                "kind": {"const": kind},
                "parameters": parameters,
            },
        }
        for kind, parameters in FEATURE_KIND_SCHEMAS.items()
    },
}
for kind, changes in {
    "extrude": EXTRUDE_UPDATE_SET,
    "cut": TARGET_UPDATE_SET,
    "hole": HOLE_UPDATE_SET,
    "fillet": FILLET_UPDATE_SET,
    "chamfer": CHAMFER_UPDATE_SET,
    "pcb_base": PCB_BASE_UPDATE_SET,
    "standoffs": STANDOFFS_UPDATE_SET,
    "pcb_lid": PCB_LID_UPDATE_SET,
    "port_cutout": PORT_CUTOUT_UPDATE_SET,
    "screw_closure": SCREW_CLOSURE_UPDATE_SET,
}.items():
    FEATURE_ACTION_SCHEMAS[("update", kind)] = {
        **FEATURE_UPDATE_SCHEMA,
        "properties": {
            **FEATURE_UPDATE_SCHEMA["properties"],
            "kind": {"const": kind},
            "set": changes,
        },
    }
FEATURE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "enum": ["create", "update", "delete"],
            "description": "Use create with parameters, update to change a feature, or delete to remove it. Delete fails when dependents exist unless cascade is true.",
        },
        **FEATURE_PROPERTIES,
        "kind": {"enum": [*FEATURE_KIND_SCHEMAS, "assembly_occurrence"]},
        "occurrenceId": ASSEMBLY_OCCURRENCE_PROPERTIES["occurrenceId"],
        "parameters": {
            **FEATURE_CREATE_SCHEMA["properties"]["parameters"],
            "description": "Required for create; not allowed for update.",
        },
        "set": {
            **FEATURE_UPDATE_SCHEMA["properties"]["set"],
            "description": "Required for update; not allowed for create or delete. Accepts extrude distance, cut or hole target.featureId, hole pattern intent, fillet radius, chamfer distance, and enclosure dimensions.",
        },
        "cascade": {"type": "boolean", "default": False},
    },
    "required": ["action", *MUTATION_PROPERTIES],
    "additionalProperties": False,
    "examples": [
        {
            "action": "create",
            "expectedRevision": 1,
            "requestId": "extrude-profile",
            "featureId": "plate",
            "kind": "extrude",
            "parameters": {
                "profile": {"sketchId": "profile", "profileIds": ["profile"]},
                "distance": {"value": 5, "unit": "mm"},
                "operation": "new_body",
                "direction": "normal",
            },
        }
    ],
}
IMPORT_PROPERTIES = {
    **MUTATION_PROPERTIES,
    "assetId": {"type": "string", "minLength": 1},
    "format": {"const": "kicad_pcb"},
}
IMPORT_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "create"},
        **IMPORT_PROPERTIES,
        "path": {
            "type": "string",
            "minLength": 1,
            "description": "Explicit absolute path to one local regular file, at most 16 MiB.",
        },
    },
    "required": ["action", *IMPORT_PROPERTIES, "path"],
    "additionalProperties": False,
}
IMPORT_DELETE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "delete"},
        **MUTATION_PROPERTIES,
        "assetId": IMPORT_PROPERTIES["assetId"],
        "cascade": {"type": "boolean", "default": False},
    },
    "required": ["action", *MUTATION_PROPERTIES, "assetId"],
    "additionalProperties": False,
}
IMPORT_REFRESH_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "refresh"},
        **MUTATION_PROPERTIES,
        "assetId": IMPORT_PROPERTIES["assetId"],
        "format": {
            "type": "string",
            "minLength": 1,
            "description": "Optional; inherits the imported asset format and must match it.",
        },
        "path": {
            **IMPORT_CREATE_SCHEMA["properties"]["path"],
            "description": "Optional explicit absolute path to the replacement local regular file, at most 16 MiB.",
        },
    },
    "required": ["action", *MUTATION_PROPERTIES, "assetId"],
    "additionalProperties": False,
}
IMPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["create", "refresh", "delete"]},
        **IMPORT_PROPERTIES,
        "path": {
            **IMPORT_CREATE_SCHEMA["properties"]["path"],
            "description": "Required for create; optional replacement path for refresh. If omitted, refresh re-reads the persisted resolved path.",
        },
        "cascade": {"type": "boolean", "default": False},
    },
    "required": ["action", *MUTATION_PROPERTIES, "assetId"],
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
                        "enclosure",
                        "feature",
                        "reference",
                        "edge_roles",
                        "server",
                        "assembly",
                    ]
                },
                "id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Feature id required for feature or edge_roles queries.",
                },
                "base": {
                    "type": "string",
                    "minLength": 1,
                    "description": "pcb_base id required for enclosure inspection.",
                },
                "role": {"enum": ["manufacturing", "reference"]},
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
EXPORT_PART_IDS = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "minItems": 1,
    "uniqueItems": True,
}

CHECK_ID = {"type": "string", "minLength": 1}
CHECK_HOLE_IDS = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "minItems": 1,
    "uniqueItems": True,
}
CHECK_OCCURRENCE_IDS = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "minItems": 2,
    "maxItems": 16,
    "uniqueItems": True,
}
CHECK_NON_NEGATIVE_LENGTH = {
    **QUANTITY,
    "properties": {
        **QUANTITY["properties"],
        "value": {"type": "number", "minimum": 0},
    },
}
CHECK_NON_NEGATIVE_VOLUME = {
    "type": "object",
    "properties": {
        "value": {"type": "number", "minimum": 0},
        "unit": {"const": "mm^3"},
    },
    "required": ["value", "unit"],
    "additionalProperties": False,
}
CHECK_FIT_THRESHOLD = {
    "type": "object",
    "properties": {"min": QUANTITY, "max": QUANTITY},
    "required": ["min", "max"],
    "additionalProperties": False,
}
CHECK_REQUIREMENT = {
    "type": "object",
    "properties": {"kind": {"enum": list(SUPPORTED_REQUIREMENT_KINDS)}},
    "required": ["kind"],
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "id": CHECK_ID,
                "kind": {
                    "enum": [
                        "valid_brep",
                        "lid_interference",
                        "lid_outline_match",
                        "clean_deliverables",
                        "clean_manufacturing_bodies",
                    ]
                },
            },
            "required": ["id", "kind"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "id": CHECK_ID,
                "kind": {"const": "assembly_interference"},
                "occurrenceIds": CHECK_OCCURRENCE_IDS,
                "maximumOverlapVolume": CHECK_NON_NEGATIVE_VOLUME,
                "minimumClearance": CHECK_NON_NEGATIVE_LENGTH,
            },
            "required": ["id", "kind"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "id": CHECK_ID,
                "kind": {"const": "screw_closure"},
                "minimumWall": POSITIVE_QUANTITY,
            },
            "required": ["id", "kind", "minimumWall"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "id": CHECK_ID,
                "kind": {"enum": ["mounting_holes", "standoff_coverage"]},
                "holeIds": CHECK_HOLE_IDS,
            },
            "required": ["id", "kind"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "id": CHECK_ID,
                "kind": {
                    "enum": [
                        "wall_thickness",
                        "floor_thickness",
                        "engagement_depth",
                        "lid_engagement",
                        "port_clearance",
                        "connector_clearance",
                    ]
                },
                "threshold": QUANTITY,
            },
            "required": ["id", "kind", "threshold"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "id": CHECK_ID,
                "kind": {"const": "board_clearance"},
                "threshold": QUANTITY,
                "side": {"enum": ["left", "right", "front", "back", "all"]},
            },
            "required": ["id", "kind", "threshold"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "id": CHECK_ID,
                "kind": {"enum": ["fit_gap", "lid_fit_gap"]},
                "threshold": CHECK_FIT_THRESHOLD,
            },
            "required": ["id", "kind", "threshold"],
            "additionalProperties": False,
        },
    ],
}
EXPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["preflight", "publish"]},
        "mode": {"enum": ["compound", "separate"]},
        "expectedRevision": {"type": "integer", "minimum": 0},
        "path": {"type": "string", "minLength": 1},
        "partIds": EXPORT_PART_IDS,
        "base": {"type": "string", "minLength": 1},
        "requirements": {
            "type": "array",
            "items": CHECK_REQUIREMENT,
            "minItems": 1,
        },
    },
    "required": ["expectedRevision"],
    "additionalProperties": False,
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"const": "evaluate"},
        "revision": {"type": "integer", "minimum": 0},
        "base": {"type": "string", "minLength": 1},
        "requirements": {
            "type": "array",
            "items": CHECK_REQUIREMENT,
            "minItems": 1,
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

TOOL_SCHEMAS = {
    "import": IMPORT_SCHEMA,
    "sketch": SKETCH_SCHEMA,
    "feature": FEATURE_SCHEMA,
    "query": QUERY_SCHEMA,
    "check": CHECK_SCHEMA,
    "export": EXPORT_SCHEMA,
}

ACTION_SCHEMAS = {
    "import": {
        "create": IMPORT_CREATE_SCHEMA,
        "refresh": IMPORT_REFRESH_SCHEMA,
        "delete": IMPORT_DELETE_SCHEMA,
    },
    "sketch": {
        "create": SKETCH_CREATE_SCHEMA,
        "update": SKETCH_UPDATE_SCHEMA,
        "delete": SKETCH_DELETE_SCHEMA,
    },
    "feature": {
        "create": FEATURE_CREATE_SCHEMA,
        "update": FEATURE_UPDATE_SCHEMA,
        "delete": FEATURE_DELETE_SCHEMA,
    },
}

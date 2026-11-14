from robot_agent_runtime.schemas import describe_bounds


def test_bounds_are_restated_without_changing_the_schema() -> None:
    schema = {"type": "object", "properties": {
        "target": {"type": "string", "minLength": 1, "description": "地点"},
        "delay_s": {"type": "number", "minimum": 0},
        "message": {"type": "string"},
    }}

    annotated = describe_bounds(schema)

    assert annotated["properties"]["target"]["description"] == "地点 [minLength=1]"
    assert annotated["properties"]["delay_s"]["description"] == "[minimum=0]"
    assert "description" not in annotated["properties"]["message"]
    assert schema["properties"]["target"]["description"] == "地点"


def test_bounds_are_restated_inside_definitions_and_array_items() -> None:
    schema = {
        "$defs": {"Step": {"type": "object", "properties": {
            "skill": {"type": "string", "minLength": 1, "pattern": r"\S"},
        }}},
        "type": "object",
        "properties": {"steps": {
            "type": "array", "minItems": 1,
            "items": {"type": "string", "maxLength": 8},
        }},
    }

    annotated = describe_bounds(schema)

    assert annotated["$defs"]["Step"]["properties"]["skill"]["description"] == (
        r"[minLength=1, pattern=\S]"
    )
    assert annotated["properties"]["steps"]["description"] == "[minItems=1]"
    assert annotated["properties"]["steps"]["items"]["description"] == "[maxLength=8]"


def test_a_schema_without_bounds_is_copied_unchanged() -> None:
    schema = {"type": "object", "properties": {"message": {"type": "string"}}}

    annotated = describe_bounds(schema)

    assert annotated == schema
    assert annotated is not schema

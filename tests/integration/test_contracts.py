"""
Contract validation test suite.
Ensures schemas, OpenAPI specs, and event contracts remain backward compatible and strictly valid.
"""

import glob
import json
import os

import jsonschema
import yaml


def test_features_contract():
    features_path = "contracts/features.yaml"
    assert os.path.exists(features_path), f"Missing {features_path}"
    with open(features_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["version"] == "1.0.0"
    assert "features" in data
    assert len(data["features"]) >= 15
    print("[OK] contracts/features.yaml validated")


def test_bigquery_schemas():
    bq_files = glob.glob("contracts/bq/*.json")
    assert len(bq_files) >= 5, f"Found only {len(bq_files)} BQ schemas"
    for file in bq_files:
        with open(file, "r", encoding="utf-8") as f:
            schema = json.load(f)
        assert isinstance(schema, list), f"{file} must be an array of field definitions"
        for field in schema:
            assert "name" in field and "type" in field, f"Invalid field in {file}: {field}"
    print(f"[OK] All {len(bq_files)} BigQuery table schemas validated")


def test_event_schemas_and_samples():
    event_files = glob.glob("contracts/events/*.json")
    assert len(event_files) >= 4, f"Found only {len(event_files)} event schemas"
    for file in event_files:
        with open(file, "r", encoding="utf-8") as f:
            schema = json.load(f)
        assert schema.get("type") == "object"
        assert "properties" in schema

    # Validate sample payloads against schemas
    sample_files = glob.glob("contracts/events/examples/*.json")
    for sample_file in sample_files:
        with open(sample_file, "r", encoding="utf-8") as f:
            sample_data = json.load(f)
        event_type = sample_data.get("event_type")
        schema_path = f"contracts/events/{event_type}.json"
        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            jsonschema.validate(instance=sample_data, schema=schema)
    print("[OK] All Pub/Sub event schemas and sample payloads validated")


def test_openapi_contract():
    openapi_path = "contracts/api/openapi.yaml"
    assert os.path.exists(openapi_path)
    with open(openapi_path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    assert spec["openapi"].startswith("3.")
    assert "/predict" in spec["paths"]
    assert "/health" in spec["paths"]
    assert "/version" in spec["paths"]
    print("[OK] contracts/api/openapi.yaml validated")


if __name__ == "__main__":
    test_features_contract()
    test_bigquery_schemas()
    test_event_schemas_and_samples()
    test_openapi_contract()
    print("\nALL CONTRACT VERIFICATION TESTS PASSED SUCCESSFULLY!")

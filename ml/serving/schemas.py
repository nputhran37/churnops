"""
Pydantic Request/Response Models for Prediction Service (Person 2)
Matches contracts/api/openapi.yaml and contracts/features.yaml
"""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class CustomerFeatures(BaseModel):
    customer_id: str = Field(..., example="CUST-104928AB")
    gender: Optional[Literal["Male", "Female"]] = "Male"
    senior_citizen: Optional[Literal[0, 1]] = 0
    partner: Optional[Literal["Yes", "No"]] = "No"
    dependents: Optional[Literal["Yes", "No"]] = "No"
    tenure: int = Field(..., ge=0, le=100, example=24)
    phone_service: Optional[Literal["Yes", "No"]] = "Yes"
    multiple_lines: Optional[Literal["Yes", "No", "No phone service"]] = "No"
    internet_service: Optional[Literal["DSL", "Fiber optic", "No"]] = "Fiber optic"
    online_security: Optional[Literal["Yes", "No", "No internet service"]] = "No"
    online_backup: Optional[Literal["Yes", "No", "No internet service"]] = "Yes"
    device_protection: Optional[Literal["Yes", "No", "No internet service"]] = "No"
    tech_support: Optional[Literal["Yes", "No", "No internet service"]] = "No"
    streaming_tv: Optional[Literal["Yes", "No", "No internet service"]] = "Yes"
    streaming_movies: Optional[Literal["Yes", "No", "No internet service"]] = "Yes"
    contract: Literal["Month-to-month", "One year", "Two year"] = Field(..., example="Month-to-month")
    paperless_billing: Optional[Literal["Yes", "No"]] = "Yes"
    payment_method: Optional[
        Literal[
            "Electronic check",
            "Mailed check",
            "Bank transfer (automatic)",
            "Credit card (automatic)",
        ]
    ] = "Electronic check"
    monthly_charges: float = Field(..., ge=10.0, le=200.0, example=85.50)
    total_charges: float = Field(..., ge=0.0, le=15000.0, example=2052.0)


class PredictRequest(BaseModel):
    trace_id: Optional[str] = Field(None, example="tr-20261002-demo-001")
    instances: List[CustomerFeatures]


class SinglePredictionResult(BaseModel):
    customer_id: str
    churn_probability: float
    churn_prediction: int


class PredictResponse(BaseModel):
    model_version: str
    predictions: List[SinglePredictionResult]
    latency_ms: float


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    model_version: str
    timestamp: str


class VersionResponse(BaseModel):
    model_version: str
    model_alias: str
    git_sha: str
    service_version: str

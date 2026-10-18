"""
ChurnOps shared data-plane library (Person 1).

One place for everything the generator, ingest job and drift service must agree on:
the feature contract, the raw -> features_daily transform, structured logging,
event publishing and the warehouse/storage abstractions.

Importable both locally (`data/` on sys.path) and inside each service container
(`COPY data/common ./common`), so the hand-off logic is never duplicated.
"""

__all__ = [
    "contracts",
    "events",
    "features",
    "logging_utils",
    "storage",
    "warehouse",
]

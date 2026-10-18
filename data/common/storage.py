"""
Object-storage abstraction for the data plane.

Locally (and in CI) everything is written to plain directories so the whole loop
runs with zero GCP credentials; when CHURNOPS_USE_GCS=1 the exact same calls go to
the buckets described in contracts/storage.md. Callers never branch on environment.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Any

import pandas as pd

# Role -> (bucket suffix per contracts/storage.md, local default root)
BUCKET_ROLES: dict[str, tuple[str, str]] = {
    "landing": ("landing", os.path.join("data", "landing")),
    "quarantine": ("quarantine", os.path.join("data", "quarantine")),
    "artifacts": ("artifacts", "artifacts"),
    "reports": ("reports", "reports"),
}


def use_gcs() -> bool:
    return os.getenv("CHURNOPS_USE_GCS", "0").lower() in ("1", "true", "yes")


def project_id() -> str:
    return os.getenv("CHURNOPS_PROJECT_ID", "churnops-dev")


@dataclass
class Storage:
    """A single bucket (or its local stand-in)."""

    role: str
    local_root: str
    bucket: str
    remote: bool

    @classmethod
    def for_role(cls, role: str, local_root: str | None = None) -> Storage:
        if role not in BUCKET_ROLES:
            raise ValueError(f"Unknown storage role '{role}'. Expected one of {sorted(BUCKET_ROLES)}")
        suffix, default_root = BUCKET_ROLES[role]
        env_override = os.getenv(f"CHURNOPS_{role.upper()}_DIR")
        return cls(
            role=role,
            local_root=local_root or env_override or default_root,
            bucket=f"{project_id()}-{suffix}",
            remote=use_gcs(),
        )

    # ------------------------------------------------------------------
    def uri(self, path: str) -> str:
        """Canonical gs:// URI for an object, used in events and report rows."""
        return f"gs://{self.bucket}/{path.replace(os.sep, '/').lstrip('/')}"

    def local_path(self, path: str) -> str:
        return os.path.join(self.local_root, path.replace("/", os.sep))

    def _client(self):  # pragma: no cover - requires GCP
        from google.cloud import storage as gcs

        return gcs.Client(project=project_id())

    # ------------------------------------------------------------------
    def write_bytes(self, path: str, payload: bytes, content_type: str = "application/octet-stream") -> str:
        if self.remote:  # pragma: no cover - requires GCP
            blob = self._client().bucket(self.bucket).blob(path)
            blob.upload_from_string(payload, content_type=content_type)
            return self.uri(path)

        target = self.local_path(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(payload)
        return target

    def write_text(self, path: str, text: str, content_type: str = "text/plain") -> str:
        return self.write_bytes(path, text.encode("utf-8"), content_type)

    def write_json(self, path: str, payload: Any) -> str:
        return self.write_text(path, json.dumps(payload, indent=2, default=str), "application/json")

    def write_parquet(self, path: str, df: pd.DataFrame) -> str:
        if self.remote:  # pragma: no cover - requires GCP
            import io

            buffer = io.BytesIO()
            df.to_parquet(buffer, index=False)
            return self.write_bytes(path, buffer.getvalue(), "application/octet-stream")

        target = self.local_path(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        df.to_parquet(target, index=False)
        return target

    def copy_in(self, source_file: str, path: str) -> str:
        """Copy a local file (e.g. a failing batch) into this bucket."""
        if self.remote:  # pragma: no cover - requires GCP
            with open(source_file, "rb") as handle:
                return self.write_bytes(path, handle.read())
        target = self.local_path(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source_file, target)
        return target

    def exists(self, path: str) -> bool:
        if self.remote:  # pragma: no cover - requires GCP
            return self._client().bucket(self.bucket).blob(path).exists()
        return os.path.exists(self.local_path(path))


def read_tabular(path: str) -> pd.DataFrame:
    """Read a parquet/csv batch from a local path or a gs:// URI."""
    if path.startswith("gs://"):  # pragma: no cover - requires GCP
        return pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path)

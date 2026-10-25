from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv('DATABASE_URL', 'postgresql+psycopg://credit:credit-local@localhost:5432/credit'))
    jwt_secret: bytes | str = field(default_factory=lambda: os.getenv('JWT_SECRET', ''))
    jwt_issuer: str = field(default_factory=lambda: os.getenv('JWT_ISSUER', 'credit-backend'))
    jwt_audience: str = field(default_factory=lambda: os.getenv('JWT_AUDIENCE', 'credit-api'))
    s3_endpoint: str = field(default_factory=lambda: os.getenv('S3_ENDPOINT', 'http://localhost:4566'))
    s3_bucket: str = field(default_factory=lambda: os.getenv('S3_BUCKET', 'credit-documents'))
    s3_region: str = field(default_factory=lambda: os.getenv('S3_REGION', 'us-east-1'))
    s3_access_key: str = field(default_factory=lambda: os.getenv('S3_ACCESS_KEY', 'test'))
    s3_secret_key: str = field(default_factory=lambda: os.getenv('S3_SECRET_KEY', 'test'))
    mock_url: str = field(default_factory=lambda: os.getenv('MOCK_URL', 'http://localhost:8001'))
    adapter_timeout: float = field(default_factory=lambda: float(os.getenv('ADAPTER_TIMEOUT', '5')))
    lease_seconds: float = field(default_factory=lambda: float(os.getenv('LEASE_SECONDS', '30')))
    max_attempts: int = field(default_factory=lambda: int(os.getenv('MAX_ATTEMPTS', '3')))
    retry_delays: tuple[float, ...] = field(default_factory=lambda: tuple(float(x) for x in os.getenv('RETRY_DELAYS', '1,5').split(',')))
    poll_interval: float = field(default_factory=lambda: float(os.getenv('POLL_INTERVAL', '0.5')))
    credit_policy_version: str = field(default_factory=lambda: os.getenv('CREDIT_POLICY_VERSION', 'demo-manual-v1'))
    openapi_path: Path = field(default_factory=lambda: Path(os.getenv('OPENAPI_PATH', str(Path(__file__).resolve().parents[3] / 'docs/openapi.yaml'))))

    def __post_init__(self):
        if isinstance(self.jwt_secret, str):
            try:
                self.jwt_secret = base64.b64decode(self.jwt_secret, validate=True)
            except Exception as exc:
                raise ValueError('JWT_SECRET must be base64 encoded') from exc
        if len(self.jwt_secret) < 32:
            raise ValueError('JWT_SECRET must contain at least 32 random bytes (base64 encoded)')
        if self.adapter_timeout <= 0 or self.lease_seconds <= self.adapter_timeout:
            raise ValueError('LEASE_SECONDS must exceed positive ADAPTER_TIMEOUT')
        if self.max_attempts < 1 or self.poll_interval <= 0 or not self.retry_delays or any(x < 0 for x in self.retry_delays):
            raise ValueError('Invalid worker retry configuration')

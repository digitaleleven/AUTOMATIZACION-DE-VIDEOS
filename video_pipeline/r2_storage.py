"""Private Cloudflare R2 storage for completed MATRIX MOTION MP4 outputs."""

from __future__ import annotations

from pathlib import Path
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


EXPECTED_BUCKET = "matrix-motion-videos"
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class R2StorageError(RuntimeError):
    """Safe-to-display R2 error without credentials or endpoint details."""


def configuration_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    endpoint = env.get("R2_ENDPOINT_URL", "")
    parsed = urlsplit(endpoint) if endpoint else None
    endpoint_valid = bool(
        parsed
        and parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )
    credentials = bool(env.get("R2_ACCESS_KEY_ID") and env.get("R2_SECRET_ACCESS_KEY"))
    bucket_matches = env.get("R2_BUCKET_NAME") == EXPECTED_BUCKET
    return {
        "ready": credentials and endpoint_valid and bucket_matches,
        "credentials_configured": credentials,
        "endpoint_valid": endpoint_valid,
        "expected_bucket_configured": bucket_matches,
        "bucket": EXPECTED_BUCKET,
    }


def upload_mp4(
    output_path: str | Path,
    job_id: str,
    *,
    client: Any = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Upload and verify an MP4 using server-side Secrets; never make it public."""
    env = os.environ if environ is None else environ
    status = configuration_status(env)
    if not status["ready"]:
        raise R2StorageError("Cloudflare R2 no está configurado para el bucket esperado.")
    if not _JOB_ID.fullmatch(job_id or ""):
        raise ValueError("Identificador de trabajo no válido para el almacenamiento.")
    source = Path(output_path).expanduser().resolve()
    if source.suffix.lower() != ".mp4" or not source.is_file() or source.stat().st_size <= 0:
        raise R2StorageError("El resultado local no es un MP4 válido para subir a R2.")

    safe_name = _SAFE_NAME.sub("-", source.name).strip("-.")[:100] or "matrix-motion.mp4"
    object_key = f"renders/{job_id}/{safe_name}"
    try:
        if client is None:
            import boto3
            from boto3.s3.transfer import TransferConfig
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=env["R2_ENDPOINT_URL"],
                aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 1, "mode": "standard"},
                    max_pool_connections=2,
                ),
            )
            transfer_config = TransferConfig(
                multipart_threshold=16 * 1024 * 1024,
                multipart_chunksize=16 * 1024 * 1024,
                max_concurrency=2,
                use_threads=True,
            )
        else:
            transfer_config = None

        kwargs = {
            "ExtraArgs": {
                "ContentType": "video/mp4",
                "Metadata": {"matrix-motion-job": job_id},
            }
        }
        if transfer_config is not None:
            kwargs["Config"] = transfer_config
        client.upload_file(str(source), EXPECTED_BUCKET, object_key, **kwargs)
        head = client.head_object(Bucket=EXPECTED_BUCKET, Key=object_key)
        if int(head.get("ContentLength", -1)) != source.stat().st_size:
            raise R2StorageError("R2 recibió un tamaño distinto al MP4 local.")
    except R2StorageError:
        raise
    except Exception as exc:
        # Provider exception text can contain endpoint/account details; don't expose it.
        error_code = None
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error_code = (response.get("Error") or {}).get("Code")
        raise R2StorageError(
            "No se pudo subir o verificar el MP4 en R2."
            + (f" Código del proveedor: {str(error_code)[:40]}." if error_code else "")
        ) from None

    return {
        "provider": "cloudflare_r2",
        "bucket": EXPECTED_BUCKET,
        "object_key": object_key,
        "size_bytes": source.stat().st_size,
        "verified": True,
        "public": False,
    }

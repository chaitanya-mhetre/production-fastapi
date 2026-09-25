"""Direct-to-S3 uploads with presigned POST policies.

The API never touches file bytes: it signs a short-lived policy that only allows one key, one
content type and a bounded size, and the client uploads straight to S3. That keeps large bodies
off uvicorn workers and Nginx, and S3 enforces the limits, not our code.
"""

import mimetypes
import uuid
from typing import Annotated, Any, Literal

import boto3
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from slotwise.api.deps import SettingsDep, require, tenant_of
from slotwise.config import Settings
from slotwise.security.permissions import Permission as P
from slotwise.security.principal import Principal

router = APIRouter(prefix="/v1", tags=["uploads"])

ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}


class PresignIn(BaseModel):
    kind: Literal["staff_avatar", "tenant_logo"]
    content_type: str = Field(pattern=r"^image/(png|jpeg|webp)$")


class PresignOut(BaseModel):
    url: str
    fields: dict[str, Any]
    key: str
    max_bytes: int
    expires_in: int


def s3_client(settings: Settings) -> Any:
    return boto3.client("s3", region_name=settings.s3_region, endpoint_url=settings.s3_endpoint_url)


@router.post("/uploads:presign")
async def presign_upload(
    body: PresignIn,
    principal: Annotated[Principal, Depends(require(P.UPLOADS_WRITE))],
    settings: SettingsDep,
) -> PresignOut:
    ext = mimetypes.guess_extension(body.content_type) or ".bin"
    # The tenant id prefix keeps tenants' objects apart and lets an IAM policy or lifecycle
    # rule target one tenant's files.
    key = f"{tenant_of(principal)}/{body.kind}/{uuid.uuid4()}{ext}"
    expires = 300
    post = s3_client(settings).generate_presigned_post(
        Bucket=settings.s3_bucket,
        Key=key,
        Fields={"Content-Type": body.content_type},
        Conditions=[
            {"Content-Type": body.content_type},
            ["content-length-range", 1, settings.upload_max_bytes],
        ],
        ExpiresIn=expires,
    )
    return PresignOut(
        url=post["url"],
        fields=post["fields"],
        key=key,
        max_bytes=settings.upload_max_bytes,
        expires_in=expires,
    )

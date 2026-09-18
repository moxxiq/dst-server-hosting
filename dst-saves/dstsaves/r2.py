"""Cloudflare R2 via the S3 API (boto3). Keys: clusters/<cluster>/backups/<zip name>."""
from __future__ import annotations

from pathlib import Path

import boto3
from botocore.config import Config

from .backup import AUTO_TAGS, parse_name
from .config import Settings


class R2:
    def __init__(self, s: Settings):
        self.bucket = s.r2_bucket
        self.prefix = f"clusters/{s.cluster_name}/backups/"
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{s.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=s.r2_access_key_id,
            aws_secret_access_key=s.r2_secret_access_key,
            region_name="auto",
            config=Config(
                # R2 rejects the newer default of always sending CRC checksums.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 4, "mode": "standard"},
            ),
        )

    def list(self) -> list[dict]:
        out = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                info = parse_name(obj["Key"][len(self.prefix):])
                if info:
                    out.append({**info, "size": obj["Size"]})
        return sorted(out, key=lambda d: d["name"])

    def upload(self, path: Path) -> str:
        key = self.prefix + path.name
        self.client.upload_file(str(path), self.bucket, key)
        return key

    def download(self, name: str, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".part")
        self.client.download_file(self.bucket, self.prefix + name, str(tmp))
        tmp.replace(dest)

    def delete(self, name: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.prefix + name)

    def prune(self, keep: int, protect: str | None = None) -> list[str]:
        """Delete all but the newest `keep` automatic zips. manual/pre-activate are never
        touched, and neither is `protect` (the object that was just uploaded), so a
        re-upload of an old zip is the only case that leaves keep+1 in R2."""
        auto = [e for e in self.list() if e["tag"] in AUTO_TAGS]
        doomed = auto[:-keep] if keep > 0 and len(auto) > keep else []
        doomed = [e for e in doomed if e["name"] != protect]
        for e in doomed:
            self.delete(e["name"])
        return [e["name"] for e in doomed]

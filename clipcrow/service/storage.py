"""Object storage: Cloudflare R2 (or any S3-compatible store) as the durable copy of finished work.

Local disk stays the working copy (rendering needs local files). Finished jobs are mirrored to the bucket in the
background; clips can then be delivered by short-lived presigned URLs (offloading bandwidth), local copies can be
evicted after a while, and deleting a job or an account removes the bucket copies first.

R2 specifics handled here:
  * endpoint ``https://<account id>.r2.cloudflarestorage.com`` (``R2_ENDPOINT`` overrides it, e.g. the EU jurisdiction),
    region ``auto``, SigV4, path-style addressing;
  * newer botocore sends CRC32 checksums by default, which R2 rejects on some calls: they are sent only when required;
  * R2 has no object ACLs and no public URLs here: the bucket stays private, access is by presigned GET only.
Not tried against live R2 from the build environment (no credentials there): the tests run the same code against an
S3-compatible server (moto) and check the client's R2 settings.

Keys are built only from a validated job id and an allow-listed relative name, never from user text.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .jobs import JOB_ID

log = logging.getLogger("clipcrow.service")

REL = re.compile(r"^(out/(c\d{1,3}\.mp4(\.ai\.json)?|c\d{1,3}\.cover\.png|edl\.json|plan-report\.json|transcript\.json)|source\.[a-z0-9]{2,5})\Z")
PREFIX = re.compile(r"^[A-Za-z0-9._/-]{0,60}\Z")
CONTENT_TYPES = {".mp4": "video/mp4", ".json": "application/json", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
                 ".webm": "video/webm", ".png": "image/png", ".m4v": "video/x-m4v"}


class StorageError(RuntimeError):
    """The bucket refused or could not be reached. The message is safe to show a person."""


class S3Storage:
    def __init__(self, bucket: str, *, endpoint: str, access_key: str, secret_key: str, prefix: str = "clipcrow",
                 region: str = "auto", client=None):
        if not (bucket and endpoint and access_key and secret_key):
            raise ValueError("object storage needs a bucket, an endpoint and an access key pair")
        if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1"):
            raise ValueError("the storage endpoint must be https")
        if not PREFIX.match(prefix) or ".." in prefix:
            raise ValueError("the storage prefix may only use letters, digits and . _ - /")
        self.bucket, self.endpoint, self.region = bucket, endpoint.rstrip("/"), region
        self.prefix = prefix.strip("/")
        self._keys = (access_key, secret_key)
        self._client = client

    @classmethod
    def from_env(cls) -> Optional["S3Storage"]:
        bucket = os.environ.get("R2_BUCKET", "")
        key, secret = os.environ.get("R2_ACCESS_KEY_ID", ""), os.environ.get("R2_SECRET_ACCESS_KEY", "")
        endpoint = os.environ.get("R2_ENDPOINT") or (f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
                                                     if os.environ.get("R2_ACCOUNT_ID") else "")
        if not (bucket and key and secret and endpoint):
            return None
        return cls(bucket, endpoint=endpoint, access_key=key, secret_key=secret, prefix=os.environ.get("R2_PREFIX", "clipcrow"))

    @property
    def host(self) -> str:
        return urlsplit(self.endpoint).netloc

    @property
    def origin(self) -> str:
        u = urlsplit(self.endpoint)
        return f"{u.scheme}://{u.netloc}"

    # --- client ---------------------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            opts = dict(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=10, read_timeout=120,
                        retries={"max_attempts": 5, "mode": "standard"})
            try:                                  # botocore >= 1.36: do not send checksums R2 rejects unless a call needs them
                cfg = Config(**opts, request_checksum_calculation="when_required", response_checksum_validation="when_required")
            except TypeError:
                cfg = Config(**opts)
            self._client = boto3.client("s3", endpoint_url=self.endpoint, region_name=self.region, config=cfg,
                                        aws_access_key_id=self._keys[0], aws_secret_access_key=self._keys[1])
        return self._client

    # --- keys -----------------------------------------------------------------------------------
    def job_prefix(self, job_id: str) -> str:
        if not JOB_ID.match(job_id):
            raise ValueError("bad job id")
        return f"{self.prefix}/jobs/{job_id}/" if self.prefix else f"jobs/{job_id}/"

    def key(self, job_id: str, rel: str) -> str:
        if not REL.match(rel):
            raise ValueError("bad object name")
        return self.job_prefix(job_id) + rel

    @staticmethod
    def _fail(what: str, e: Exception) -> StorageError:
        log.warning("object storage: %s failed: %s", what, type(e).__name__)      # never log keys or the endpoint's answer
        return StorageError("The file storage could not be reached just now." if "Connect" in type(e).__name__ or "Timeout" in type(e).__name__
                            else "The file storage refused the request.")

    # --- operations -----------------------------------------------------------------------------
    def put_file(self, job_id: str, rel: str, path: Path) -> None:
        ctype = CONTENT_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")
        try:
            self.client.upload_file(str(path), self.bucket, self.key(job_id, rel), ExtraArgs={"ContentType": ctype})
        except Exception as e:                                              # botocore raises many types; all become one
            raise self._fail("upload", e) from None

    # --- direct upload from the browser (S3 multipart with presigned part URLs) ------------------------
    # The browser PUTs the file's parts straight to the bucket, so the bytes never pass through the app's own domain (and
    # whatever proxy sits in front of it, with its request-size limit). Every part URL signs its exact Content-Length, so
    # a person cannot use a URL to store more than the size they announced.

    def create_multipart(self, job_id: str, rel: str) -> str:
        ctype = CONTENT_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")
        try:
            return self.client.create_multipart_upload(Bucket=self.bucket, Key=self.key(job_id, rel), ContentType=ctype)["UploadId"]
        except Exception as e:
            raise self._fail("start upload", e) from None

    def presign_part(self, job_id: str, rel: str, upload_id: str, number: int, length: int, *, expires: int = 3600) -> str:
        try:
            return self.client.generate_presigned_url(
                "upload_part", ExpiresIn=max(60, min(expires, 6 * 3600)),
                Params={"Bucket": self.bucket, "Key": self.key(job_id, rel), "UploadId": upload_id,
                        "PartNumber": number, "ContentLength": length})
        except Exception as e:
            raise self._fail("presign part", e) from None

    def complete_multipart(self, job_id: str, rel: str, upload_id: str, parts: list[tuple[int, str]]) -> None:
        try:
            self.client.complete_multipart_upload(
                Bucket=self.bucket, Key=self.key(job_id, rel), UploadId=upload_id,
                MultipartUpload={"Parts": [{"PartNumber": n, "ETag": e} for n, e in sorted(parts)]})
        except Exception as e:
            raise self._fail("finish upload", e) from None

    def abort_multipart(self, job_id: str, rel: str, upload_id: str) -> None:
        try:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=self.key(job_id, rel), UploadId=upload_id)
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") in ("NoSuchUpload", "404"):
                return                                           # already gone: that is what we wanted
            raise self._fail("abort upload", e) from None

    def size(self, job_id: str, rel: str) -> Optional[int]:
        try:
            return int(self.client.head_object(Bucket=self.bucket, Key=self.key(job_id, rel))["ContentLength"])
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise self._fail("head", e) from None

    # --- the bucket's CORS rule (the browser needs it to PUT parts and to read each part's ETag) ------------
    def cors_rule(self, origins: list[str]) -> dict:
        return {"CORSRules": [{"AllowedOrigins": origins, "AllowedMethods": ["PUT", "GET", "HEAD"], "AllowedHeaders": ["*"],
                               "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3600}]}

    def set_cors(self, origins: list[str]) -> None:
        for o in origins:
            u = urlsplit(o)
            if u.scheme not in ("https", "http") or not u.netloc or u.path not in ("", "/") or "*" in o:
                raise ValueError(f"{o!r} is not an origin like https://app.example.com")
        try:
            self.client.put_bucket_cors(Bucket=self.bucket, CORSConfiguration=self.cors_rule([o.rstrip("/") for o in origins]))
        except Exception as e:
            raise self._fail("set CORS", e) from None

    def get_cors(self) -> list[dict]:
        try:
            return self.client.get_bucket_cors(Bucket=self.bucket).get("CORSRules", [])
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") in ("NoSuchCORSConfiguration", "404"):
                return []
            raise self._fail("read CORS", e) from None

    def get_file(self, job_id: str, rel: str, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".part")
        try:
            self.client.download_file(self.bucket, self.key(job_id, rel), str(tmp))
            tmp.replace(dest)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise self._fail("download", e) from None

    def has(self, job_id: str, rel: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(job_id, rel))
            return True
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise self._fail("head", e) from None

    def presign_get(self, job_id: str, rel: str, *, expires: int = 600, filename: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": self.key(job_id, rel)}
        if filename:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:80] or "clip.mp4"
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        try:
            return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=max(60, min(expires, 3600)))
        except Exception as e:
            raise self._fail("presign", e) from None

    def delete(self, job_id: str, rel: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.key(job_id, rel))
        except Exception as e:
            raise self._fail("delete", e) from None

    def delete_job(self, job_id: str) -> int:
        """Remove everything stored for a job. Raises StorageError if anything could not be removed."""
        prefix, n = self.job_prefix(job_id), 0
        try:
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if not objs:
                    continue
                res = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objs, "Quiet": True})
                if res.get("Errors"):
                    raise StorageError("The file storage could not remove everything.")
                n += len(objs)
        except StorageError:
            raise
        except Exception as e:
            raise self._fail("delete job", e) from None
        return n


def signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


class Mirror:
    """Keeps the bucket copy of each finished job up to date, in the background. A file is uploaded when its
    signature (size and modification time) differs from what was last uploaded, so a re-rendered clip goes up again and
    nothing goes up twice. A failed upload is simply retried by the next pass (``reconcile`` is called by the sweeper)."""

    def __init__(self, storage: S3Storage, store, *, backup_source: bool = False, workers: int = 2):
        from concurrent.futures import ThreadPoolExecutor
        self.storage, self.store, self.backup_source = storage, store, backup_source
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="clipcrow-mirror")

    def submit(self, job_id: str) -> None:
        try:
            self._pool.submit(self._safe, job_id)
        except RuntimeError:                                 # shutting down: the next start reconciles
            pass

    def close(self) -> None:
        """Let uploads already started finish (a deploy's grace period bounds how long this can take)."""
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _safe(self, job_id: str) -> None:
        try:
            self.backup(job_id)
        except Exception:                                    # a mirror problem must never touch a job
            log.exception("mirroring job %s failed", job_id)

    def wanted(self, job) -> list[str]:
        rels = ["out/edl.json", "out/plan-report.json", "out/transcript.json"]
        for c in job.clips:
            rels += [f"out/{c.id}.mp4", f"out/{c.id}.mp4.ai.json", f"out/{c.id}.cover.png"]
        src = self.store.source_path(job.id)
        if self.backup_source and src is not None:
            rels.append(src.name)
        return rels

    def backup(self, job_id: str) -> int:
        try:
            job = self.store.get(job_id)
        except KeyError:
            return 0                                          # deleted meanwhile
        if job.status != "done":
            return 0
        sent = 0
        for rel in self.wanted(job):
            path = self.store.dir(job_id) / rel
            if not path.is_file() or not REL.match(rel):
                continue
            sig = signature(path)
            if job.remote.get(rel) == sig:
                continue
            try:
                self.storage.put_file(job_id, rel, path)
            except StorageError:
                log.warning("mirror: %s/%s not uploaded yet, will retry", job_id, rel)
                continue
            try:
                self.store.update(job_id, lambda j, r=rel, s=sig: j.remote.__setitem__(r, s))
            except KeyError:
                return sent                                  # the job was deleted while uploading
            sent += 1
        return sent

    def reconcile(self) -> int:
        """Queue every finished job that is missing from, or out of date in, the bucket."""
        n = 0
        for job in self.store.list():
            if job.status == "done":
                self.submit(job.id)
                n += 1
        return n

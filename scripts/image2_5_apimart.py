#!/usr/bin/env python3
"""APImart GPT-Image-2.5 Ext candidate adapter, standard library only.

No implicit generation, fallback, network retry, or publication.

Input JSON contract (unknown fields are rejected):
  {"schema_version": 1, "model": "gpt-image-2.5-ext",
   "version": "sunburst", "prompt": "...", "resolution": "2K",
   "size": "3:4", "n": 1, "ref_paths": ["relative/to/input.jpg"],
   "image_urls": ["https://... or data:image/png;base64,..."]}
Defaults: schema_version=1, exact Ext model, sunburst, 2K, auto, n=1.
Local reference paths resolve relative to the input JSON, not the current cwd.
PNG/JPEG references and outputs are verified by signature/container/dimensions.
URLs/data URLs are never printed or persisted; reference hashes replace them.

CLI:
  ... submit --input INPUT.json --output NEW_DIR --dry-run  # offline; no key
  ... submit --input INPUT.json --output NEW_DIR [--env-file PRIVATE.env]
  ... resume --run-dir DIR [--timeout 45 --interval 5] [--env-file PRIVATE.env]
  ... recover-submit --run-dir DIR --input SAME_INPUT.json [--env-file ...]
submit returns after acceptance. resume never submits a new generation. A lost
submit response requires explicit recover-submit, same payload and saved UUID.
Credentials: APIMART_API_KEY in environment, or a mode-0600 env file outside
the knowledge root. The only default file is
~/.config/shengjiang/weekend-activity-marketing/apimart.env; --env-file overrides
that location. No directory scanning; never read the historical
sample's .env. Only APIMART_API_KEY is read, with no shell evaluation.

Official schemas, checked 2026-10-02:
https://docs.apimart.ai/en/api-reference/images/gpt-image-2.5-ext/generation.md
https://docs.apimart.ai/en/api-reference/tasks/status.md
Offline tests are not WorkBuddy execution or a real image-generation test.
Exit codes: 0=accepted/dry-run or complete delivery, 2=error, 3=pending,
4=remote task completed but fewer images delivered than requested; never auto
submit another paid generation to replace a missing image.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

API_ROOT = "https://api.apimart.ai"
ENDPOINT = API_ROOT + "/v1/images/generations"
MODEL = "gpt-image-2.5-ext"
RESPONSE_VERSION = "2026-07-27"
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
SIZES = {"auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9"}
ALLOWED_FIELDS = {"schema_version", "model", "version", "prompt", "resolution", "size", "n", "ref_paths", "image_urls"}
TERMINAL_FAILURES = {"failed", "cancelled", "canceled"}
PENDING = {"created", "pending", "submitted", "processing", "running", "queued"}
SAFE_API_CODES = {"invalid_request_error", "authentication_error", "invalid_api_key", "insufficient_balance", "rate_limit_exceeded", "rate_limit_error", "server_error", "internal_error", "not_found", "forbidden", "task_failed"}
SKILL_ROOT = Path(__file__).resolve().parents[1]


def find_knowledge_root(script_path: Path | None = None) -> Path | None:
    """Only a verified knowledge layout is a root, not an arbitrary ancestor."""
    for ancestor in (script_path or Path(__file__)).resolve().parents:
        if (ancestor / "INDEX.md").is_file() and (ancestor / "system" / "PROCEDURES.md").is_file():
            return ancestor
    return None


KNOWLEDGE_ROOT = find_knowledge_root()


class AdapterError(Exception):
    """Only machine-safe diagnostic fields, never remote error prose/URLs."""
    def __init__(self, code: str, http_status: int | None = None, request_id: str | None = None):
        super().__init__(code)
        self.code, self.http_status, self.request_id = code, http_status, request_id

    def diagnostic(self) -> dict[str, Any]:
        return {k: v for k, v in {"error_code": self.code, "http_status": self.http_status, "request_id": self.request_id}.items() if v is not None}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def safe_id(value: Any, secret: str = "") -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) and (not secret or secret not in value):
        return value
    return None


def image_info(blob: bytes) -> dict[str, Any]:
    """Container/signature and dimensions check; not a visual/semantic review."""
    if not blob or len(blob) > MAX_IMAGE_BYTES:
        raise AdapterError("invalid_image_size")
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        pos = 8
        dimensions = None
        found_data = found_end = False
        while pos + 12 <= len(blob):
            length = struct.unpack(">I", blob[pos:pos + 4])[0]
            typ = blob[pos + 4:pos + 8]
            end = pos + 12 + length
            if end > len(blob):
                raise AdapterError("truncated_png")
            data = blob[pos + 8:pos + 8 + length]
            crc = struct.unpack(">I", blob[end - 4:end])[0]
            if zlib.crc32(typ + data) & 0xffffffff != crc:
                raise AdapterError("invalid_png_crc")
            if pos == 8 and (typ != b"IHDR" or length != 13):
                raise AdapterError("invalid_png_header")
            if typ == b"IHDR":
                dimensions = struct.unpack(">II", data[:8])
            elif typ == b"IDAT":
                found_data = True
            elif typ == b"IEND":
                found_end = length == 0 and end == len(blob)
                break
            pos = end
        if not dimensions or not all(0 < v <= 32768 for v in dimensions) or not found_data or not found_end:
            raise AdapterError("invalid_png_container")
        return {"media_type": "image/png", "extension": ".png", "width": dimensions[0], "height": dimensions[1]}
    if blob.startswith(b"\xff\xd8") and blob.endswith(b"\xff\xd9"):
        pos = 2
        while pos < len(blob) - 2:
            if blob[pos] != 0xff:
                raise AdapterError("invalid_jpeg_container")
            while pos < len(blob) and blob[pos] == 0xff:
                pos += 1
            if pos >= len(blob):
                break
            marker = blob[pos]
            pos += 1
            if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
                continue
            if pos + 2 > len(blob):
                break
            size = struct.unpack(">H", blob[pos:pos + 2])[0]
            if size < 2 or pos + size > len(blob):
                raise AdapterError("truncated_jpeg")
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                if size < 8:
                    break
                height, width = struct.unpack(">HH", blob[pos + 3:pos + 7])
                if not 0 < width <= 32768 or not 0 < height <= 32768:
                    break
                return {"media_type": "image/jpeg", "extension": ".jpg", "width": width, "height": height}
            if marker == 0xda:
                break
            pos += size
        raise AdapterError("invalid_jpeg_dimensions")
    raise AdapterError("unsupported_image_signature")


def checked_url(value: str) -> str:
    p = urllib.parse.urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise AdapterError("invalid_https_image_url")
    try:
        addr = ipaddress.ip_address(p.hostname)
        if not addr.is_global:
            raise AdapterError("private_image_url")
    except ValueError:
        pass
    return value


def load_input(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AdapterError("invalid_input_json") from None
    if not isinstance(cfg, dict) or set(cfg) - ALLOWED_FIELDS:
        raise AdapterError("unknown_input_fields")
    if cfg.get("schema_version", 1) != 1 or isinstance(cfg.get("schema_version", 1), bool):
        raise AdapterError("invalid_schema_version")
    if cfg.get("model", MODEL) != MODEL:
        raise AdapterError("model_must_be_gpt_image_2_5_ext")
    prompt = cfg.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AdapterError("empty_prompt")
    version, resolution, size, count = cfg.get("version", "sunburst"), cfg.get("resolution", "2K"), cfg.get("size", "auto"), cfg.get("n", 1)
    if version not in ("flare", "sunburst") or resolution not in ("1K", "2K", "4K") or size not in SIZES:
        raise AdapterError("invalid_model_parameters")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
        raise AdapterError("invalid_image_count")
    local, remote = cfg.get("ref_paths", []), cfg.get("image_urls", [])
    if not isinstance(local, list) or not isinstance(remote, list) or any(not isinstance(x, str) or not x for x in local + remote) or len(local) + len(remote) > 16:
        raise AdapterError("invalid_reference_list")
    references, metadata = [], []
    for value in local:
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = path.resolve().parent / p
        try:
            if p.stat().st_size > MAX_IMAGE_BYTES:
                raise AdapterError("reference_too_large")
            blob = p.read_bytes()
        except OSError:
            raise AdapterError("reference_unreadable") from None
        info = image_info(blob)
        references.append("data:" + info["media_type"] + ";base64," + base64.b64encode(blob).decode("ascii"))
        metadata.append({"kind": "local", "path": str(p.resolve()), "sha256": digest(blob), "bytes": len(blob), **{k: v for k, v in info.items() if k != "extension"}})
    for value in remote:
        if value.startswith("data:"):
            match = re.fullmatch(r"data:(image/(?:png|jpeg));base64,([A-Za-z0-9+/=\r\n]+)", value)
            if not match:
                raise AdapterError("invalid_reference_data_url")
            try:
                blob = base64.b64decode(re.sub(r"\s", "", match.group(2)), validate=True)
            except (ValueError, binascii.Error):
                raise AdapterError("invalid_reference_base64") from None
            info = image_info(blob)
            if info["media_type"] != match.group(1):
                raise AdapterError("reference_media_type_mismatch")
            metadata.append({"kind": "data_url", "sha256": digest(blob), "bytes": len(blob), **{k: v for k, v in info.items() if k != "extension"}})
        else:
            checked_url(value)
            metadata.append({"kind": "https_url", "url_sha256": digest(value.encode())})
        references.append(value)
    payload = {"model": MODEL, "version": version, "prompt": prompt, "resolution": resolution, "size": size, "n": count}
    if references:
        payload["image_urls"] = references
    summary = {"model": MODEL, "version": version, "resolution": resolution, "size": size, "n": count, "prompt_chars": len(prompt), "prompt_sha256": digest(prompt.encode()), "reference_count": len(references), "references": metadata, "payload_sha256": digest(encoded_json(payload))}
    return payload, summary


def default_env_path() -> Path:
    return Path.home() / ".config" / "shengjiang" / "weekend-activity-marketing" / "apimart.env"


def load_key(env_file: Path | None) -> str:
    value = os.environ.get("APIMART_API_KEY", "").strip()
    if env_file is None and not value:
        candidate = default_env_path()
        if not candidate.is_file():
            raise AdapterError("missing_apimart_api_key")
        env_file = candidate
    if env_file is not None:
        p = env_file.expanduser().resolve()
        try:
            # In a copied Skill (~/.agents/skills/...), the user's whole home
            # is not a knowledge root. Protect just the package in that case.
            p.relative_to((KNOWLEDGE_ROOT or SKILL_ROOT).resolve())
        except ValueError:
            pass
        else:
            raise AdapterError("env_file_must_be_outside_knowledge_root")
        try:
            if os.name != "nt" and p.stat().st_mode & 0o077:
                raise AdapterError("env_file_permissions_must_be_0600")
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            raise AdapterError("env_file_unreadable") from None
        file_value = ""
        for line in lines:
            match = re.fullmatch(r"\s*(?:export\s+)?APIMART_API_KEY\s*=\s*(.*?)\s*", line)
            if match:
                file_value = match.group(1)
                if len(file_value) >= 2 and file_value[0] == file_value[-1] and file_value[0] in "\"'":
                    file_value = file_value[1:-1]
        value = value or file_value
    if not value or any(c in value for c in "\r\n"):
        raise AdapterError("missing_apimart_api_key")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: Any, headers: Any, newurl: str) -> None:
        raise AdapterError("api_redirect_rejected", code)


class SafeImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: Any, headers: Any, newurl: str) -> Any:
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def response_id(obj: Any, headers: Any, key: str) -> str | None:
    value = obj.get("request_id") if isinstance(obj, dict) else None
    return safe_id(value, key) or safe_id(headers.get("X-Request-ID"), key)


def api_json(method: str, url: str, key: str, payload: dict[str, Any] | None = None, idempotency: str | None = None) -> tuple[dict[str, Any], str | None]:
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-APIMart-Response-Version": RESPONSE_VERSION}
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    request = urllib.request.Request(url, data=encoded_json(payload) if payload is not None else None, headers=headers, method=method)
    # Deliberately one attempt; no network retry or provider/model fallback.
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=45 if method == "GET" else 90) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
            status, response_headers = response.status, response.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_JSON_BYTES + 1)
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = {}
        code = obj.get("error", {}).get("code") if isinstance(obj, dict) and isinstance(obj.get("error"), dict) else None
        raise AdapterError(code if code in SAFE_API_CODES else "api_http_error", exc.code, response_id(obj, exc.headers, key)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AdapterError("api_network_error") from None
    if len(raw) > MAX_JSON_BYTES:
        raise AdapterError("api_response_too_large", status)
    try:
        obj = json.loads(raw)
    except ValueError:
        raise AdapterError("api_non_json_response", status) from None
    if not isinstance(obj, dict):
        raise AdapterError("api_invalid_response", status)
    rid = response_id(obj, response_headers, key)
    if obj.get("error") or isinstance(obj.get("code"), int) and obj["code"] >= 400:
        error = obj.get("error", {})
        code = error.get("code") if isinstance(error, dict) else None
        raise AdapterError(code if code in SAFE_API_CODES else "api_error", status, rid)
    return obj, rid


def download(url: str) -> bytes:
    checked_url(url)
    # No Authorization header goes to a media URL, and no retry.
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "WeekendActivityImageAdapter/1.0"})
        with urllib.request.build_opener(SafeImageRedirect()).open(request, timeout=45) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError):
        raise AdapterError("image_download_error") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise AdapterError("image_download_too_large")
    return data


def save_json(path: Path, obj: Any) -> None:
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def run_lock(directory: Path):
    # OS advisory locks release after a crash, so resume does not need stale-lock deletion.
    with (directory / ".operation.lock").open("a+b") as f:
        try:
            if os.name == "nt":
                import msvcrt
                if f.tell() == 0:
                    f.write(b"0"); f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AdapterError("run_already_in_use") from None
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_state(directory: Path) -> dict[str, Any]:
    try:
        obj = json.loads((directory / "run.json").read_text())
    except (OSError, ValueError):
        raise AdapterError("invalid_run_state") from None
    if not isinstance(obj, dict) or obj.get("model") != MODEL or not safe_id(obj.get("idempotency_key")):
        raise AdapterError("invalid_run_state")
    return obj


def record_error(directory: Path, state: dict[str, Any], exc: AdapterError) -> None:
    state["last_error"] = exc.diagnostic()
    save_json(directory / "run.json", state)


def submission(directory: Path, state: dict[str, Any], payload: dict[str, Any], key: str) -> dict[str, Any]:
    state["status"] = "submission_uncertain"
    state["submit_attempts"] = state.get("submit_attempts", 0) + 1
    save_json(directory / "run.json", state)  # persisted UUID/digest BEFORE the potentially billed call
    try:
        response, rid = api_json("POST", ENDPOINT, key, payload, state["idempotency_key"])
        items = response.get("data")
        if isinstance(items, dict):
            task_id = safe_id(items.get("id"), key) or safe_id(items.get("task_id"), key)
        elif isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            task_id = safe_id(items[0].get("task_id"), key) or safe_id(items[0].get("id"), key)
        else:
            task_id = None
        if not task_id:
            raise AdapterError("accepted_response_missing_task_id", request_id=rid)
        state.update({"status": "submitted", "task_id": task_id, "submit_request_id": rid})
        state.pop("last_error", None)
        save_json(directory / "run.json", state)
        return {"status": "submitted", "task_id": task_id, "run_dir": str(directory)}
    except AdapterError as exc:
        record_error(directory, state, exc)
        raise


def submit(input_path: Path, directory: Path, env_file: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    payload, summary = load_input(input_path)
    if directory.exists():
        raise AdapterError("output_directory_already_exists")
    if not directory.parent.is_dir():
        raise AdapterError("output_parent_missing")
    if dry_run:
        return {"status": "dry_run", "network_requests": 0, "endpoint": ENDPOINT, "parameters": summary, "output_dir": str(directory)}
    key = load_key(env_file)  # no credential -> no directory and no network
    try:
        directory.mkdir(mode=0o700)
    except OSError:
        raise AdapterError("output_directory_creation_failed") from None
    state = {"schema_version": 1, "model": MODEL, "status": "prepared", "idempotency_key": str(uuid.uuid4()), "parameters": summary, "images": [], "submit_attempts": 0, "acceptance_is_not_image_completion": True}
    with run_lock(directory):
        return submission(directory, state, payload, key)


def recover_submit(input_path: Path, directory: Path, env_file: Path | None = None) -> dict[str, Any]:
    payload, summary = load_input(input_path)
    key = load_key(env_file)
    with run_lock(directory):
        state = read_state(directory)
        if state.get("task_id"):
            raise AdapterError("task_exists_use_resume")
        if state.get("status") != "submission_uncertain" or summary["payload_sha256"] != state["parameters"]["payload_sha256"]:
            raise AdapterError("recovery_requires_identical_payload")
        return submission(directory, state, payload, key)


def returned_urls(task: dict[str, Any]) -> list[str]:
    result = task.get("result")
    images = result.get("images") if isinstance(result, dict) else None
    urls = []
    if not isinstance(images, list):
        raise AdapterError("completed_task_has_no_images")
    for image in images:
        value = image.get("url") if isinstance(image, dict) else None
        values = value if isinstance(value, list) else [value]
        for url in values:
            if isinstance(url, str):
                checked_url(url); urls.append(url)
    if not urls:
        raise AdapterError("completed_task_has_no_images")
    return urls


def safe_task_metadata(task: dict[str, Any]) -> dict[str, Any]:
    fields = ("cost", "credits_cost", "actual_time", "estimated_time", "created", "completed", "progress")
    result = {k: task[k] for k in fields if isinstance(task.get(k), (int, float)) and not isinstance(task[k], bool)}
    usage = task.get("usage")
    if isinstance(usage, dict):
        result["usage"] = {k: v for k, v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens", "cost"} and isinstance(v, (int, float)) and not isinstance(v, bool)}
    return result


def delivery_metadata(state: dict[str, Any]) -> dict[str, Any]:
    requested = state["parameters"]["n"]
    delivered = len(state.get("images", []))
    return {"requested_images": requested, "delivered_images": delivered,
            "delivery_complete": delivered == requested,
            "partial_delivery": delivered < requested,
            "missing_images": max(0, requested - delivered)}


def finalize(directory: Path, state: dict[str, Any], task: dict[str, Any], request_id: str | None) -> dict[str, Any]:
    urls = returned_urls(task)
    if len(urls) > state["parameters"]["n"]:
        raise AdapterError("unexpected_delivered_image_count")
    existing = {x["index"]: x for x in state.get("images", [])}
    for index, url in enumerate(urls, 1):
        if index in existing:
            entry = existing[index]
            path = directory / entry["file"]
            try:
                blob = path.read_bytes()
            except OSError:
                raise AdapterError("saved_image_missing") from None
            info = image_info(blob)
            if digest(blob) != entry["sha256"] or info["width"] != entry["width"] or info["height"] != entry["height"]:
                raise AdapterError("saved_image_hash_mismatch")
            continue
        blob = download(url)
        info = image_info(blob)
        path = directory / ("image-" + str(index).zfill(3) + info["extension"])
        try:
            with path.open("xb") as f:
                f.write(blob); f.flush(); os.fsync(f.fileno())
        except FileExistsError:
            raise AdapterError("image_file_already_exists") from None
        except OSError:
            raise AdapterError("image_write_error") from None
        entry = {"index": index, "file": path.name, "sha256": digest(blob), "bytes": len(blob), **{k: v for k, v in info.items() if k != "extension"}}
        state.setdefault("images", []).append(entry)
        save_json(directory / "run.json", state)
    state.update({"status": "completed", "remote_status": "completed", "last_poll_request_id": request_id, "task_metadata": safe_task_metadata(task), **delivery_metadata(state), "visual_review": "not_performed", "workbuddy_runtime_test": "not_performed"})
    state.pop("last_error", None)
    save_json(directory / "manifest.json", state)
    save_json(directory / "run.json", state)
    return {"status": "completed" if state["delivery_complete"] else "partial_delivery", "remote_status": "completed", **delivery_metadata(state), "task_id": state["task_id"], "manifest": str(directory / "manifest.json"), "images": [str(directory / x["file"]) for x in state["images"]], "visual_review": "not_performed"}


def resume(directory: Path, env_file: Path | None = None, timeout: int = 45, interval: int = 5) -> dict[str, Any]:
    if not 0 <= timeout <= 3600 or not 1 <= interval <= 60:
        raise AdapterError("invalid_poll_timing")
    key = load_key(env_file)
    with run_lock(directory):
        state = read_state(directory)
        task_id = safe_id(state.get("task_id"))
        if not task_id:
            raise AdapterError("no_task_id_use_recover_submit")
        if state.get("status") in TERMINAL_FAILURES:
            raise AdapterError("task_terminal_failure")
        if state.get("status") == "completed":
            # No network or new submission for an already completed local operation.
            for entry in state.get("images", []):
                p = directory / entry["file"]
                if not p.is_file() or digest(p.read_bytes()) != entry["sha256"]:
                    raise AdapterError("saved_image_hash_mismatch")
            if not state.get("images") or not (directory / "manifest.json").is_file():
                raise AdapterError("completed_state_missing_artifacts")
            counts = delivery_metadata(state)
            return {"status": "completed" if counts["delivery_complete"] else "partial_delivery", "remote_status": "completed", **counts, "task_id": task_id, "manifest": str(directory / "manifest.json"), "network_requests": 0}
        deadline = time.monotonic() + timeout
        try:
            while True:
                response, rid = api_json("GET", API_ROOT + "/v1/tasks/" + task_id, key)
                task = response.get("data")
                if not isinstance(task, dict):
                    raise AdapterError("invalid_task_response", request_id=rid)
                status = task.get("status")
                state.update({"last_poll_request_id": rid, "remote_status": status if status in PENDING | TERMINAL_FAILURES | {"completed"} else "unknown"})
                if status == "completed":
                    return finalize(directory, state, task, rid)
                if status in TERMINAL_FAILURES:
                    state["status"] = status
                    state["task_metadata"] = safe_task_metadata(task)
                    save_json(directory / "run.json", state)
                    raise AdapterError("task_terminal_failure", request_id=rid)
                if status not in PENDING:
                    raise AdapterError("unknown_task_status", request_id=rid)
                state["status"] = "waiting"
                save_json(directory / "run.json", state)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"status": "pending", "task_id": task_id, "run_dir": str(directory), "next_action": "resume", "new_submissions": 0}
                time.sleep(min(interval, remaining))
        except AdapterError as exc:
            record_error(directory, state, exc)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("submit", help="one explicit generation submission; not completion")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--env-file", type=Path)
    p = sub.add_parser("resume", help="query and download the existing task; no POST")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--interval", type=int, default=5)
    p = sub.add_parser("recover-submit", help="explicitly recover lost acceptance with saved UUID and exact input")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            result = submit(args.input, args.output, args.env_file, args.dry_run)
        elif args.command == "resume":
            result = resume(args.run_dir, args.env_file, args.timeout, args.interval)
        else:
            result = recover_submit(args.input, args.run_dir, args.env_file)
        print(json.dumps(result, ensure_ascii=False))
        return {"pending": 3, "partial_delivery": 4}.get(result["status"], 0)
    except AdapterError as exc:
        print(json.dumps(exc.diagnostic(), ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        # No tracebacks or raw remote strings, including URLs and credentials.
        print(json.dumps({"error_code": "local_operation_error"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

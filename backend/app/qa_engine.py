"""Invoke a confined, locally installed OCI image with no network or host mounts."""

import json
import re
import struct
import subprocess
from dataclasses import dataclass
from uuid import uuid4

from app.workbook_source import MAX_OUTPUT_BYTES

IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}")
IMAGE_ID = re.compile(r"(?:sha256:)?[0-9a-f]{64}")


class EngineUnavailable(Exception):
    pass


@dataclass(frozen=True)
class EngineResult:
    data: bytes
    engine: str


@dataclass(frozen=True)
class QAEngine:
    runtime: str
    image: str | None

    def run(self, operation: str, data: bytes) -> EngineResult:
        if not self.image or not IMAGE_NAME.fullmatch(self.image):
            raise EngineUnavailable("A local isolated workbook QA image is not configured.")
        name = f"invoice-qa-{uuid4().hex}"
        try:
            image_id = subprocess.run(
                [self.runtime, "image", "inspect", "--format", "{{.Id}}", self.image],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()
            if not IMAGE_ID.fullmatch(image_id):
                raise EngineUnavailable("The isolated workbook QA image has no recognized immutable identity.")
            result = subprocess.run([
                self.runtime, "run", "--rm", "--pull=never", "--network=none", "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=128m", "--memory=768m", "--memory-swap=768m",
                "--cpus=1", "--pids-limit=96", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--user=65534:65534", "--env=HOME=/tmp", "--env=TMPDIR=/tmp", "--log-driver=none",
                "--name", name, "-i", image_id, operation,
            ], input=data, capture_output=True, timeout=48, check=True)
        except (OSError, subprocess.SubprocessError):
            raise EngineUnavailable("The isolated workbook QA worker failed or exceeded its limit.") from None
        finally:
            try:
                subprocess.run([self.runtime, "rm", "-f", name], capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        decoded = decode_result(result.stdout)
        return EngineResult(decoded.data, f"{decoded.engine} [sha256:{image_id.removeprefix('sha256:')}]")


def decode_result(data: bytes) -> EngineResult:
    try:
        size, = struct.unpack("!I", data[:4])
        if not 0 < size <= 512 or not 0 < len(data) - 4 - size <= MAX_OUTPUT_BYTES:
            raise ValueError
        meta = json.loads(data[4:4 + size])
        version = meta["engine"]
        if not isinstance(version, str) or not version or len(version) > 240:
            raise ValueError
    except (ValueError, KeyError, TypeError, struct.error):
        raise EngineUnavailable("The workbook QA worker returned an invalid result.") from None
    return EngineResult(data[4 + size:], version)

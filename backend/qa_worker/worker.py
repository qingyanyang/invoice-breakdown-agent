"""Single-operation LibreOffice worker. The parent supplies the OCI isolation."""

import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

LIMIT = 16 * 1024 * 1024
PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
</item></oor:items>
"""
PDF_FILTER = 'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'


def execute(operation):
    data = sys.stdin.buffer.read(LIMIT + 1)
    if operation not in {"calculate", "render", "measure"} or not data or len(data) > LIMIT:
        raise ValueError("Invalid worker request.")
    with TemporaryDirectory(prefix="invoice-qa-") as directory:
        root = Path(directory)
        source, result, profile = root / "input", root / "output", root / "profile"
        for path in (source, result, profile / "user"):
            path.mkdir(parents=True)
        (profile / "user" / "registrymodifications.xcu").write_text(PROFILE, encoding="utf-8")
        path = source / "review.xlsx"
        path.write_bytes(data)
        if operation == "calculate":
            target_format, extension = "xlsx:Calc Office Open XML", "xlsx"
        else:
            target_format, extension = PDF_FILTER, "pdf"
        command = "soffice"
        env = {**os.environ, "HOME": directory, "TMPDIR": directory, "SAL_USE_VCLPLUGIN": "svp"}
        if sys.platform.startswith("linux"):
            env.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        options = {"timeout": 38, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "check": True}
        version = subprocess.run([command, "--version"], **options).stdout.decode(errors="replace").strip()
        subprocess.run([
            command, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--nologo",
            "--nodefault", "--norestore", "--language=en-US", "--convert-to", target_format, "--outdir", str(result), str(path),
        ], **options)
        output = result / ("review." + extension)
        if not output.is_file() or not 0 < output.stat().st_size <= LIMIT:
            raise ValueError("The engine did not write a bounded output.")
        metadata = json.dumps({"engine": version[:240]}).encode()
        sys.stdout.buffer.write(struct.pack("!I", len(metadata)) + metadata + output.read_bytes())


if __name__ == "__main__":
    try:
        execute(sys.argv[1])
    except (IndexError, OSError, ValueError, subprocess.SubprocessError):
        print("The workbook worker could not complete the requested operation.", file=sys.stderr)
        sys.exit(1)

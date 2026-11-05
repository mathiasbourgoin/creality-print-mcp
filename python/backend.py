"""Application adapter. MCP framing and validation live in the OCaml mcp-kit host.

Stock Creality wire shapes are derived from the official CrealityPrint source;
Orca REST operations follow MaxEllis/orcaslicer-mcp's documented client.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid


def state_dir():
    p = Path(os.environ.get("CREALITY_MCP_STATE", Path.home() / ".local/state/creality-print-mcp")).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def file_path(value):
    p = Path(value).expanduser().resolve(strict=True)
    if not p.is_file():
        raise ValueError("Expected a regular file")
    return p


def redact(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in ("password", "token", "apikey", "api_key", "secret", "print_host", "printhost")) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


def check_changes(changes):
    for key in changes:
        if (key == "post_process" or "gcode" in key.lower() or key in {"printer_model", "printer_technology", "filename_format", "print_host", "host_type", "printer_agent"}
                or key.startswith("printhost_") or any(s in key.lower() for s in ("password", "token", "apikey", "api_key"))):
            raise ValueError(f"Edit {key} manually in the slicer; this tool edits print settings only")


def roots():
    if os.environ.get("CREALITY_PROFILE_ROOTS"):
        return [Path(p).expanduser().resolve() for p in os.environ["CREALITY_PROFILE_ROOTS"].split(os.pathsep)]
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return sorted((config / "Creality/Creality Print").glob("*"), reverse=True) + [config / "OrcaSlicer"]


def presets():
    result = []
    # Earlier roots win. User presets override system presets within a root.
    for root in roots():
        paths = sorted(root.rglob("*.json"), key=lambda p: ("user" not in p.parts, str(p))) if root.is_dir() else []
        for p in paths:
            kind = next((k for k in ("machine", "process", "filament") if k in p.parts), None)
            if kind:
                try:
                    data = json.loads(p.read_text())
                    result.append({"name": data.get("name", p.stem), "kind": kind, "path": str(p), "root": str(root), "data": data})
                except (ValueError, OSError):
                    continue
    return result


def resolve_preset(path, index=None, seen=()):
    path = file_path(path)
    if path in seen:
        raise ValueError(f"Preset inheritance cycle at {path.name}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Preset must contain a JSON object")
    parent = data.get("inherits")
    base = {}
    if parent:
        index = presets() if index is None else index
        kind = next((k for k in ("machine", "process", "filament") if k in path.parts), None)
        candidates = [x for x in index if (x["name"] == parent or Path(x["path"]).stem == parent) and (kind is None or x["kind"] == kind)]
        # Resolve using the same installation first, then other configured roots.
        candidates.sort(key=lambda x: not path.is_relative_to(Path(x["root"])))
        if not candidates:
            raise ValueError(f"Missing inherited preset {parent!r}; configure CREALITY_PROFILE_ROOTS")
        base = resolve_preset(candidates[0]["path"], index, (*seen, path))
    base.update(data)
    base.pop("inherits", None)
    return base


def binary(backend):
    env, candidates = ("CREALITY_PRINT_BIN", ["creality-print", "CrealityPrint"]) if backend == "creality" else ("ORCA_SLICER_BIN", ["orca-slicer-mcp", "orca-slicer", "OrcaSlicer"])
    value = os.environ.get(env)
    if value:
        return str(file_path(value)) if "/" in value else (shutil.which(value) or value)
    return next((shutil.which(c) for c in candidates if shutil.which(c)), None)


def default_backend():
    return "orca" if binary("orca") else "creality"


def cli_help(exe):
    with tempfile.TemporaryDirectory(prefix="creality-cli-probe-") as cwd:
        proc = subprocess.run([exe, "--help"], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    text = proc.stdout.decode(errors="replace")
    if not re.search(r"--slice\b", text):
        raise ValueError("Slicer has no usable CLI --slice support: " + text[:1500])
    return text


def slice_model(a):
    backend = a.get("backend", default_backend())
    exe = binary(backend)
    if not exe:
        raise ValueError(f"No {backend} slicer installed; configure the binary path")
    model = file_path(a["model"])
    if model.suffix.lower() not in {".stl", ".obj", ".3mf", ".step", ".stp"}:
        raise ValueError("Expected STL, OBJ, 3MF or STEP")
    if not a["filaments"]:
        raise ValueError("At least one filament preset is required")
    help_text = cli_help(exe)
    job_id = uuid.uuid4().hex
    folder = state_dir() / "jobs" / job_id
    folder.mkdir(parents=True)
    index = presets()
    paths = []
    for i, source in enumerate([a["machine"], a["process"], *a["filaments"]]):
        out = folder / f"preset-{i}.json"
        resolved = resolve_preset(source, index)
        # An unattended slice must not invoke profile post-processing commands.
        resolved.pop("post_process", None)
        write_json(out, resolved)
        paths.append(str(out))
    def flag(name):
        hyphen = "--" + name.replace("_", "-")
        underscore = "--" + name
        if hyphen in help_text:
            return hyphen
        if underscore in help_text:
            return underscore
        raise ValueError(f"Unsupported slicer option: {name}")
    cmd = [exe, "--slice", str(a.get("plate", 0)), "--outputdir", str(folder / "output"), flag("load_settings"), ";".join(paths[:2]), flag("load_filaments"), ";".join(paths[2:])]
    if "--datadir" in help_text:
        # Some packaged CLI builds otherwise attempt to create /log.
        datadir = folder / "slicer-data"
        datadir.mkdir()
        cmd += ["--datadir", str(datadir)]
    for key in ("arrange", "orient"):
        if key in a:
            cmd += [flag(key), str(int(a[key]))]
    if a.get("allow_mix_temp", False):
        cmd.append(flag("allow_mix_temp"))
    for key, cli_key in (("scale", "scale"), ("rotate_z", "rotate")):
        if key in a:
            cmd += [flag(cli_key), str(a[key])]
    cmd.append(str(model))
    manifest = {"job_id": job_id, "state": "queued", "command": cmd, "timeout_seconds": a.get("timeout_seconds", 1800), "created_at": time.time()}
    write_json(folder / "job.json", manifest)
    with open(folder / "worker.log", "ab") as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(folder)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
        # Reap the worker in long-lived test/embedded callers; the short-lived
        # production helper can exit while this detached worker keeps slicing.
        threading.Thread(target=child.wait, daemon=True).start()
    return {"job_id": job_id, "state": "queued", "poll_tool": "slice_job"}


def worker(folder):
    path = folder / "job.json"
    job = json.loads(path.read_text())
    job.update(state="running", worker_pid=os.getpid())
    write_json(path, job)
    try:
        # GNU timeout owns the entire slicer process group, including shell helpers.
        with open(folder / "slice.log", "wb") as log:
            p = subprocess.run(["timeout", "--kill-after=5", str(job["timeout_seconds"]), *job["command"]], cwd=folder, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        outputs = sorted((folder / "output").rglob("*"))
        artifacts = [str(p) for p in outputs if p.is_file()]
        gcodes = [p for p in artifacts if p.endswith(".gcode") and Path(p).stat().st_size > 0]
        job.update(state="succeeded" if p.returncode == 0 and gcodes else "failed", exit_code=p.returncode, artifacts=artifacts)
        if not gcodes:
            job["error"] = "No nonempty .gcode generated; inspect slice.log (a GUI launch is not a successful slice)"
    except Exception as exc:
        job.update(state="failed", error=str(exc))
    job["finished_at"] = time.time()
    write_json(path, job)


def slice_job(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid job_id")
    folder = state_dir() / "jobs" / job_id
    job = json.loads((folder / "job.json").read_text())
    if job["state"] == "running":
        try:
            os.kill(job["worker_pid"], 0)
        except ProcessLookupError:
            job.update(state="interrupted", error="Worker stopped; check artifacts and logs")
    log = folder / "slice.log"
    if log.exists():
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size - 12000))
            job["log_tail"] = f.read().decode(errors="replace")
    return job


def inspect_gcode(value):
    p = file_path(value)
    if p.suffix.lower() != ".gcode" or not p.stat().st_size:
        raise ValueError("Expected a nonempty plain .gcode file")
    with p.open("rb") as f:
        head = f.read(65536).decode(errors="replace")
        f.seek(max(0, p.stat().st_size - 131072))
        tail = f.read().decode(errors="replace")
    comments = [s for s in head.splitlines() if s.startswith(";")]
    estimates = {}
    for line in (head + "\n" + tail).splitlines():
        if line.startswith(";") and "=" in line and any(term in line for term in ("estimated printing time", "total filament used", "total layer number")):
            key, value = line.lstrip("; ").split("=", 1)
            estimates[key.strip()] = value.strip()
    return {"path": str(p), "size_bytes": p.stat().st_size, "estimates": estimates, "header_comments": comments[:180]}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("API redirect refused")


def http(url, method="GET", data=None, headers=None, timeout=30, destination=None):
    # LAN/loopback control never goes through a configured HTTP proxy.
    request = Request(url, data=data, headers=headers or {}, method=method)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            if destination is not None:
                partial = destination.with_suffix(".part")
                try:
                    size = 0
                    with partial.open("wb") as stream:
                        while chunk := response.read(1024 * 1024):
                            size += len(chunk)
                            if size > 1024 * 1024 * 1024:
                                raise ValueError("G-code export exceeds 1 GiB")
                            stream.write(chunk)
                    partial.replace(destination)
                finally:
                    partial.unlink(missing_ok=True)
                return b""
            payload = response.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024:
                raise ValueError("Response exceeds 32 MiB")
            return payload
    except HTTPError as exc:
        body = exc.read(4096).decode(errors="replace")
        try:
            body = json.dumps(redact(json.loads(body)))
        except ValueError:
            body = "Response was not JSON"
        raise ValueError(f"API HTTP {exc.code}: {body}") from None


def host():
    h = os.environ.get("CREALITY_PRINTER_HOST", "")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", h):
        raise ValueError("Set CREALITY_PRINTER_HOST to the Hi IP/hostname, without scheme or port")
    return h


def remote_path(path):
    prefixes = ("/usr/data/printer_data/gcodes/", "/mnt/UDISK/printer_data/gcodes/")
    if not any(path.startswith(p) for p in prefixes) or not re.fullmatch(r"[A-Za-z0-9_-]+\.gcode", path.rsplit("/", 1)[-1]):
        raise ValueError("Use the exact remote_path returned by upload_gcode")
    if "/../" in path or len(path.split("/")) != 6:
        raise ValueError("Invalid remote print path")
    return path


async def collect(ws, seconds=3, until=None):
    end = asyncio.get_running_loop().time() + seconds
    state = {}
    while asyncio.get_running_loop().time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), end - asyncio.get_running_loop().time())
        except asyncio.TimeoutError:
            break
        if raw == "ok":
            continue
        value = json.loads(raw)
        if isinstance(value, dict):
            state.update(value.get("params", value) if isinstance(value.get("params", value), dict) else {})
        if until and until(state):
            break
    return state


async def printer(action, a):
    from websockets.asyncio.client import connect
    url = f"ws://{host()}:{int(os.environ.get('CREALITY_WS_PORT', '9999'))}"
    async with connect(url, open_timeout=5, close_timeout=2, max_size=4 * 1024 * 1024, proxy=None) as ws:
        await ws.send(json.dumps({"method": "get", "params": {"reqGcodeFile": 1, "reqGcodeList": 1, "boxsInfo": 1}}))
        status = await collect(ws, 3)
        if action == "printer_status":
            if not status:
                raise ValueError("Printer connected but sent no telemetry")
            files = status.get("retGcodeFileInfo2")
            if isinstance(files, list):
                status["retGcodeFileInfo2"] = files[:a.get("file_limit", 20)]
            return {"host": host(), "file_count": len(files) if isinstance(files, list) else None, "telemetry": redact(status)}
        if action == "set_cfs_material":
            if status.get("deviceState") != 0:
                raise ValueError("Printer is not confirmed idle; CFS metadata was not changed")
            box_id, material_id = a["box_id"], a["material_id"]
            boxes = status.get("boxsInfo", {}).get("materialBoxs", [])
            slots = [m for box in boxes if box.get("id") == box_id and box.get("type") == 0
                     for m in box.get("materials", []) if m.get("id") == material_id]
            if len(slots) != 1 or slots[0].get("state") != 1:
                raise ValueError("CFS slot is not confirmed present")
            old = slots[0]
            if old.get("editStatus") != 1:
                raise ValueError("CFS slot is not manually editable")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", a["color"]):
                raise ValueError("Expected color #RRGGBB")
            if not 100 <= a["min_temp"] <= a["max_temp"] <= 350:
                raise ValueError("Invalid material temperature range")
            if a["material_type"] not in {"PLA", "PETG", "ABS", "ASA", "PET", "HIPS", "PVA", "BVOH"}:
                raise ValueError("Unsupported material type")
            for key in ("vendor", "name"):
                if not isinstance(a[key], str) or not 1 <= len(a[key]) <= 80 or any(ord(c) < 32 for c in a[key]):
                    raise ValueError("Invalid material label")
            material = {"boxId": box_id, "id": material_id, "rfid": old.get("rfid", ""),
                        "type": a["material_type"], "vendor": a["vendor"], "name": a["name"],
                        "color": "#0" + a["color"][1:].upper(),
                        "minTemp": a["min_temp"] + 1e-8, "maxTemp": a["max_temp"] + 1e-8,
                        "pressure": old.get("pressure", 0)}
            # Official SetMaterials shape. Metadata only: no feed, heat or motion.
            await ws.send(json.dumps({"method": "set", "params": {"modifyMaterial": material}}))
            after = await collect(ws, 3)
            return {"command_sent_once": True, "requested": material,
                    "previous": old, "observed_boxsInfo": after.get("boxsInfo"),
                    "next_tool": "printer_status"}
        if action == "start_print":
            if status.get("deviceState") != 0:
                raise ValueError(f"Printer is not confirmed idle (deviceState={status.get('deviceState')!r})")
            path = remote_path(a["remote_path"])
            calibration = int(a.get("calibration", True))
            if a.get("use_cfs", False):
                mapping = a.get("mapping")
                if not mapping:
                    raise ValueError("CFS start requires an explicit mapping; inspect boxsInfo first")
                # Match the official frontend: the CFS path uses the UDISK alias.
                path = "/mnt/UDISK/printer_data/gcodes/" + path.rsplit("/", 1)[-1]
                await ws.send(json.dumps({"method": "set", "params": {"colorMatch": {"path": path, "list": mapping}}}))
                # The vendor client sends these in order on the same socket;
                # it does not define a mapping acknowledgement.
                params = {"multiColorPrint": {"gcode": path, "enableSelfTest": calibration}}
            else:
                if a.get("mapping"):
                    raise ValueError("Set use_cfs=true when supplying a filament mapping")
                params = {"opGcodeFile": "printprt:" + path, "enableSelfTest": calibration}
        else:
            params = {"pause_print": {"pause": 1}, "resume_print": {"pause": 0}, "cancel_print": {"stop": 1}}[action]
        # Send exactly once. A dropped connection can mean an applied command.
        await ws.send(json.dumps({"method": "set", "params": params}))
        try:
            after = await collect(ws, 6, lambda s: action == "start_print" and s.get("deviceState") == 1)
        except Exception:
            after = {}  # A lost acknowledgement must not be reported as a failed start.
        return {"command_sent": True, "state_observed": after.get("deviceState"),
                "start_observed": action == "start_print" and after.get("deviceState") == 1,
                "telemetry": redact(after), "retry_policy": "Read printer_status before retrying; command transmission is not proof of execution."}


def upload_gcode(a):
    inspect_gcode(a["path"])
    p = file_path(a["path"])
    digest = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    # Conservative ASCII filename avoids multipart and firmware parsing ambiguity.
    name = re.sub(r"[^A-Za-z0-9_-]", "_", p.stem)[:64] + "_" + digest.hexdigest()[:12] + ".gcode"
    port = int(os.environ.get("CREALITY_HTTP_PORT", "80"))
    # curl streams multipart rather than loading large G-code into memory.
    result = subprocess.run(["curl", "--silent", "--show-error", "--fail-with-body", "--noproxy", "*", "--connect-timeout", "5", "--max-time", "120", "--form", f'file=@"{str(p).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}";filename={name}', f"http://{host()}:{port}/upload/{name}"], stdin=subprocess.DEVNULL, capture_output=True, timeout=130)
    if result.returncode or "OK" not in result.stdout.decode(errors="replace").upper():
        raise ValueError("Upload failed or lacked the firmware OK acknowledgement; inspect printer files before retrying")
    prefix = os.environ.get("CREALITY_GCODE_DIR", "/mnt/UDISK/printer_data/gcodes")
    path = remote_path(prefix + "/" + name)
    receipt = {"remote_path": path, "sha256": digest.hexdigest(), "uploaded": True, "print_started": False}
    receipts = state_dir() / "uploads"
    receipts.mkdir(exist_ok=True)
    write_json(receipts / (name + ".json"), receipt)
    return receipt


def camera_config(configure=False):
    base = f"http://{host()}:{int(os.environ.get('CREALITY_MOONRAKER_PORT', '7125'))}"
    def request(path, data=None):
        return json.loads(http(base + path, "POST" if data is not None else "GET",
                               json.dumps(data).encode() if data is not None else None,
                               {"Content-Type": "application/json"}))['result']
    cameras = request('/server/webcams/list')['webcams']
    if not configure:
        return {"webcams": cameras}
    stream = f"http://{host()}:8000/webcam/?action=stream"
    page = http(stream).decode(errors='replace')
    if 'RTCPeerConnection' not in page:
        raise ValueError('Expected the Hi WebRTC camera page on port 8000; camera configuration unchanged')
    existing = next((c for c in cameras if c.get('stream_url') == stream), None)
    if existing and existing.get('service') == 'iframe' and existing.get('enabled', True):
        return {"changed": False, "webcam": existing}
    payload = {"name": "Creality Hi", "location": "printer", "service": "iframe",
               "enabled": True, "stream_url": stream, "snapshot_url": "",
               "aspect_ratio": "16:9", "target_fps": 15}
    if existing:
        if existing.get('uid'):
            payload['uid'] = existing['uid']
        else:
            payload['name'] = existing['name']  # Older Moonraker identifies cameras by name.
    result = request('/server/webcams/item', payload)
    return {"changed": True, "webcam": result.get('webcam', result), "next_step": "Refresh the Device tab; the camera uses Fluidd's HTTP Page service."}


def orca_token():
    token = os.environ.get("ORCA_API_TOKEN", "").strip()
    if not token:
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "OrcaSlicer/OrcaSlicer.conf"
        if config.is_file():
            token = json.loads(config.read_text()).get("app", {}).get("remote_api_token", "")
    return token


def orca_request(method, path, data=None, raw=False, destination=None):
    token = orca_token()
    if not token:
        raise ValueError("Set ORCA_API_TOKEN from the running OrcaSlicer fork's Preferences > Remote API")
    base = os.environ.get("ORCA_API_URL", "http://127.0.0.1:13130").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.path not in {"", "/"}:
        raise ValueError("ORCA_API_URL must be a loopback HTTP(S) origin")
    payload = http(base + path, method, json.dumps(data).encode() if data is not None else None,
                   {"X-Api-Token": token, "Content-Type": "application/json"}, timeout=130 if path == "/api/v1/model" else 30, destination=destination)
    return payload if raw else redact(json.loads(payload))


def launch_orca(a):
    exe = binary("orca")
    if not exe:
        raise ValueError("Install orcaslicer-mcp-bin or set ORCA_SLICER_BIN to the MCP fork")
    try:
        return {"already_running": True, "status": orca_request("GET", "/api/v1/status")}
    except Exception:
        pass
    # Avoid overwriting configuration owned by a running GUI.
    for p in Path("/proc").iterdir():
        if p.name.isdecimal():
            try:
                name = (p / "comm").read_text().strip().lower()
                if name in {"orca-slicer", "orcaslicer"}:
                    raise ValueError("Orca is already running without a reachable API. Enable Remote API in its Preferences or close it before launch_orca.")
            except (OSError, ProcessLookupError):
                continue
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "OrcaSlicer/OrcaSlicer.conf"
    if a.get("enable_api", True) and config.exists():
        data = json.loads(config.read_text())
        backup = state_dir() / ("orca-config-backup-" + uuid.uuid4().hex + ".json")
        backup.write_bytes(config.read_bytes())
        backup.chmod(0o600)
        app = data.setdefault("app", {})
        app.update(remote_api_enabled=True, remote_api_bind_lan=False)
        write_json(config, data)
        config.chmod(0o600)
    env = dict(os.environ)
    # Desktop-launched agents do not always inherit the compositor's environment.
    # Copy only display-routing variables from the user's Plasma session.
    for p in Path("/proc").iterdir():
        if p.name.isdecimal():
            try:
                if (p / "comm").read_text().strip() == "plasmashell" and p.stat().st_uid == os.getuid():
                    for entry in (p / "environ").read_bytes().split(b"\0"):
                        key, sep, value = entry.partition(b"=")
                        if sep and key.decode() in {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE"}:
                            env.setdefault(key.decode(), value.decode())
                    break
            except OSError:
                continue
    env.setdefault("GDK_BACKEND", "x11")
    log = state_dir() / "orca-gui.log"
    with log.open("ab") as stream:
        child = subprocess.Popen([exe], env=env, cwd=state_dir(), stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True, close_fds=True)
        threading.Thread(target=child.wait, daemon=True).start()
    # Returning before GUI startup finishes lets the client show progress.
    return {"launched": True, "pid": child.pid, "log": str(log), "next_tool": "orca_status"}


def orca(action, a):
    gets = {"orca_status": "status", "orca_presets": "presets", "orca_objects": "objects", "orca_config": "config", "orca_slice_status": "slice/status", "orca_job_status": "jobs/status"}
    if action in gets:
        result = orca_request("GET", "/api/v1/" + gets[action])
        if action == "orca_objects":
            for obj in result.get("objects", []):
                transform = obj.get("transform", {})
                if "rotation" in transform:
                    transform["rotation"] = [math.degrees(x) for x in transform["rotation"]]
            result["rotation_unit"] = "degrees"
        return result
    posts = {"orca_arrange": "arrange", "orca_orient": "orient", "orca_slice": "slice", "orca_cancel_slice": "slice/cancel"}
    if action in posts:
        return orca_request("POST", "/api/v1/" + posts[action])
    if action == "orca_load_model":
        return orca_request("POST", "/api/v1/model", {"path": str(file_path(a["path"]))})
    if action == "orca_select_preset":
        return orca_request("PUT", "/api/v1/preset", a)
    if action == "orca_set_config":
        check_changes(a["changes"])
        return orca_request("PUT", "/api/v1/config", a["changes"])
    if action == "orca_transform":
        result = orca_request("POST", f"/api/v1/objects/{int(a['object_id'])}/transform", a["transform"])
        if "rotation" in result.get("transform", {}):
            result["transform"]["rotation"] = [math.degrees(x) for x in result["transform"]["rotation"]]
            result["rotation_unit"] = "degrees"
        return result
    if action == "orca_delete_object":
        return orca_request("DELETE", f"/api/v1/objects/{int(a['object_id'])}")
    if action == "orca_save_gcode":
        folder = state_dir() / "exports"
        folder.mkdir(exist_ok=True)
        path = folder / (uuid.uuid4().hex + ".gcode")
        orca_request("GET", "/api/v1/gcode", raw=True, destination=path)
        return inspect_gcode(str(path))
    if action == "orca_render_plate":
        view, angle = a.get("view", "editor"), a.get("angle", "iso")
        data = orca_request("GET", f"/api/v1/plate/render?view={view}&angle={angle}&width=1280&height=900", raw=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Orca did not return a PNG render")
        folder = state_dir() / "renders"
        folder.mkdir(exist_ok=True)
        path = folder / (uuid.uuid4().hex + ".png")
        path.write_bytes(data)
        return {"path": str(path), "view": view, "angle": angle, "image_base64": base64.b64encode(data).decode()}
    raise ValueError("Unknown Orca operation")


def dispatch(name, a):
    if name in {"printer_cameras", "configure_printer_camera"}:
        return camera_config(configure=name == "configure_printer_camera")
    if name == "launch_orca":
        return launch_orca(a)
    if name == "capabilities":
        return {"creality_binary": binary("creality"), "orca_binary": binary("orca"), "profile_roots": [str(p) for p in roots() if p.is_dir()],
                "default_backend": default_backend(), "printer_configured": bool(os.environ.get("CREALITY_PRINTER_HOST")), "orca_api_configured": bool(orca_token()), "state_dir": str(state_dir())}
    if name == "list_presets":
        matches = [x for x in presets() if (not a.get("kind") or x["kind"] == a["kind"]) and a.get("query", "").casefold() in x["name"].casefold()]
        return {"total": len(matches), "presets": [{k: v for k, v in x.items() if k not in {"data", "root"}} for x in matches[:a.get("limit", 50)]]}
    if name == "read_preset":
        return {"settings": redact(resolve_preset(a["path"]))}
    if name == "configure_preset":
        check_changes(a["overrides"])
        data = resolve_preset(a["path"])
        data.update(a["overrides"])
        data.pop("post_process", None)
        folder = state_dir() / "presets"
        folder.mkdir(exist_ok=True)
        path = folder / (uuid.uuid4().hex + ".json")
        write_json(path, data)
        return {"path": str(path), "settings": redact(data)}
    if name == "slice_model":
        return slice_model(a)
    if name == "slice_job":
        return slice_job(a["job_id"])
    if name == "inspect_gcode":
        return inspect_gcode(a["path"])
    if name == "upload_gcode":
        return upload_gcode(a)
    if name in {"printer_status", "set_cfs_material", "start_print", "pause_print", "resume_print", "cancel_print"}:
        return asyncio.run(printer(name, a))
    if name.startswith("orca_"):
        return orca(name, a)
    raise ValueError("Unknown tool")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]))
    else:
        try:
            request = json.load(sys.stdin)
            print(json.dumps(dispatch(request["tool"], request["arguments"])))
        except Exception as exc:
            # Network exceptions can contain URLs; never emit Orca API tokens.
            message = str(exc)
            token = os.environ.get("ORCA_API_TOKEN", "")
            if token:
                message = message.replace(token, "[redacted]")
            print(json.dumps({"error": message}))

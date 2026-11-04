import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import backend as b


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"CREALITY_MCP_STATE": str(self.root / "state"), "CREALITY_PROFILE_ROOTS": str(self.root / "profiles")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def preset(self, name, data):
        p = self.root / "profiles/machine" / (name + ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
        return str(p)

    def test_inheritance_override_and_cycle(self):
        self.preset("base", {"name": "base", "layer_height": "0.2", "printhost_apikey": "secret"})
        child = self.preset("child", {"name": "child", "inherits": "base", "wall_loops": "3"})
        r = b.dispatch("configure_preset", {"path": child, "overrides": {"layer_height": "0.12"}})
        data = json.loads(Path(r["path"]).read_text())
        self.assertEqual(data["layer_height"], "0.12")
        self.assertEqual(data["wall_loops"], "3")
        self.assertNotIn("inherits", data)
        self.assertEqual(r["settings"]["printhost_apikey"], "[redacted]")
        self.preset("base", {"name": "base", "inherits": "child"})
        with self.assertRaisesRegex(ValueError, "cycle"):
            b.resolve_preset(child)

    def test_job_ids_and_remote_paths(self):
        for path in ["/tmp/a.gcode", "/usr/data/printer_data/gcodes/../a.gcode", "/usr/data/printer_data/gcodes/a;stop.gcode"]:
            with self.assertRaises(ValueError):
                b.remote_path(path)
        with self.assertRaises(ValueError):
            b.slice_job("../../outside")

    def test_script_and_connection_edits_refused(self):
        for key in ["post_process", "machine_start_gcode", "print_host", "printhost_apikey"]:
            with self.assertRaises(ValueError):
                b.check_changes({key: "x"})

    def test_orca_rotation_units_match_transform_input(self):
        wire = {"objects": [{"transform": {"rotation": [math.pi / 2, 0, -math.pi]}}]}
        with patch.object(b, "orca_request", return_value=wire):
            result = b.orca("orca_objects", {})
        self.assertEqual(result["rotation_unit"], "degrees")
        self.assertEqual(result["objects"][0]["transform"]["rotation"], [90, 0, -180])
        with patch.object(b, "orca_request", return_value={"transform": {"rotation": [math.pi, 0, 0]}}) as call:
            result = b.orca("orca_transform", {"object_id": 65, "transform": {"rotate": [90, 0, 0]}})
        self.assertEqual(call.call_args.args[2], {"rotate": [90, 0, 0]})
        self.assertEqual(result["transform"]["rotation"], [180, 0, 0])

    def test_camera_registration_and_legacy_idempotence(self):
        stream = "http://127.0.0.1:8000/webcam/?action=stream"
        camera = {"name": "Creality Hi", "service": "iframe", "stream_url": stream}
        # The tested stock firmware omits uid and enabled in webcam responses.
        with patch.dict(os.environ, {"CREALITY_PRINTER_HOST": "127.0.0.1"}):
            with patch.object(b, "http", side_effect=[
                json.dumps({"result": {"webcams": [{"name": "Other", "stream_url": "http://other/"}]}}).encode(),
                b"<script>new RTCPeerConnection()</script>",
                json.dumps({"result": {"webcam": camera}}).encode(),
            ]) as request:
                result = b.camera_config(configure=True)
                self.assertTrue(result["changed"])
                payload = json.loads(request.call_args.args[2])
                self.assertEqual(payload["service"], "iframe")
                self.assertEqual(payload["stream_url"], stream)
                self.assertEqual(payload["snapshot_url"], "")
                self.assertEqual(result["webcam"], camera)
            with patch.object(b, "http", side_effect=[
                json.dumps({"result": {"webcams": [camera]}}).encode(),
                b"<script>new RTCPeerConnection()</script>",
            ]) as request:
                self.assertFalse(b.camera_config(configure=True)["changed"])
                self.assertEqual(request.call_count, 2)  # No duplicate POST.

    def test_async_slice_requires_artifacts(self):
        fake = self.root / "slicer"
        fake.write_text('#!/usr/bin/env python3\nimport sys,pathlib\nif "--help" in sys.argv: print("--slice --load-settings --load-filaments --arrange"); sys.exit(0)\np=pathlib.Path(sys.argv[sys.argv.index("--outputdir")+1]); p.mkdir(); (p/"cube.gcode").write_text("; estimate: 1m\\nG1 X0\\n")\n')
        fake.chmod(0o755)
        preset = self.preset("simple", {"name": "simple"})
        model = self.root / "model with spaces.stl"
        model.write_text("test model")
        with patch.dict(os.environ, {"CREALITY_PRINT_BIN": str(fake)}):
            r = b.slice_model({"backend": "creality", "model": str(model), "machine": preset, "process": preset, "filaments": [preset], "arrange": True})
        for _ in range(100):
            job = b.slice_job(r["job_id"])
            if job["state"] not in {"queued", "running"}:
                break
            time.sleep(.02)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(any(x.endswith("cube.gcode") for x in job["artifacts"]))
        fake.write_text('#!/usr/bin/env python3\nimport sys\nif "--help" in sys.argv: print("--slice --load-settings --load-filaments")\n')
        with patch.dict(os.environ, {"CREALITY_PRINT_BIN": str(fake)}):
            r = b.slice_model({"backend": "creality", "model": str(model), "machine": preset, "process": preset, "filaments": [preset]})
        for _ in range(100):
            job = b.slice_job(r["job_id"])
            if job["state"] not in {"queued", "running"}:
                break
            time.sleep(.02)
        self.assertEqual(job["state"], "failed")

    def test_http_upload_never_starts_and_orca_auth(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({"token_seen": self.headers.get("X-Api-Token"), "layer_height": "0.2"}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            p = self.root / 'cube "quoted".gcode'
            p.write_text("; estimated printing time = 1m\nG1 X1\n")
            with patch.dict(os.environ, {"CREALITY_PRINTER_HOST": "127.0.0.1", "CREALITY_HTTP_PORT": str(server.server_port), "ORCA_API_URL": f"http://127.0.0.1:{server.server_port}", "ORCA_API_TOKEN": "test-secret"}):
                result = b.upload_gcode({"path": str(p)})
                self.assertFalse(result["print_started"])
                self.assertEqual(len(requests), 1)
                self.assertTrue(requests[0][0].startswith("/upload/"))
                self.assertIn(b"G1 X1", requests[0][1])
                self.assertEqual(b.orca("orca_config", {})["token_seen"], "[redacted]")
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_gcode_export_streams_past_json_response_limit(self):
        size = 33 * 1024 * 1024
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                chunk = b"; streaming export test\n".ljust(1024 * 1024, b" ")
                for _ in range(33):
                    self.wfile.write(chunk)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"ORCA_API_URL": f"http://127.0.0.1:{server.server_port}", "ORCA_API_TOKEN": "test-secret"}):
                result = b.orca("orca_save_gcode", {})
            self.assertEqual(result["size_bytes"], size)
            self.assertEqual(Path(result["path"]).stat().st_size, size)
            self.assertFalse(list(Path(result["path"]).parent.glob("*.part")))
        finally:
            server.shutdown(); server.server_close(); thread.join()


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def run_printer(self, state, action, args=None, heartbeat=False):
        from websockets.asyncio.server import serve
        frames = []
        async def peer(ws):
            try:
                frames.append(json.loads(await ws.recv()))
                if heartbeat:
                    await ws.send("ok")
                await ws.send(json.dumps({"deviceState": state, "nozzleTemp": 25}))
                async for raw in ws:
                    frame = json.loads(raw)
                    frames.append(frame)
                    if "colorMatch" not in frame["params"]:
                        await ws.send(json.dumps({"deviceState": 1}))
            except Exception:
                pass
        async with serve(peer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with patch.dict(os.environ, {"CREALITY_PRINTER_HOST": "127.0.0.1", "CREALITY_WS_PORT": str(port)}):
                result = await b.printer(action, args or {})
        return result, frames

    async def test_stock_start_is_sent_once_and_observed(self):
        r, frames = await self.run_printer(0, "start_print", {"remote_path": "/usr/data/printer_data/gcodes/cube.gcode"}, heartbeat=True)
        self.assertTrue(r["start_observed"])
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[1]["params"], {"opGcodeFile": "printprt:/usr/data/printer_data/gcodes/cube.gcode", "enableSelfTest": 1})

    async def test_busy_printer_refuses_start(self):
        with self.assertRaisesRegex(ValueError, "not confirmed idle"):
            await self.run_printer(1, "start_print", {"remote_path": "/usr/data/printer_data/gcodes/cube.gcode"})

    async def test_cfs_mapping_then_multicolor_start(self):
        mapping = [{"id": "T1A", "type": "PLA", "color": "#FFFFFF", "boxId": 0, "materialId": 0}]
        r, frames = await self.run_printer(0, "start_print", {"remote_path": "/usr/data/printer_data/gcodes/cube.gcode", "use_cfs": True, "mapping": mapping})
        self.assertTrue(r["start_observed"])
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[1]["params"]["colorMatch"]["list"], mapping)
        self.assertEqual(frames[2]["params"]["multiColorPrint"]["gcode"], "/mnt/UDISK/printer_data/gcodes/cube.gcode")

    async def test_cfs_without_mapping_refused(self):
        with self.assertRaisesRegex(ValueError, "explicit mapping"):
            await self.run_printer(0, "start_print", {"remote_path": "/usr/data/printer_data/gcodes/cube.gcode", "use_cfs": True})


class McpTests(unittest.TestCase):
    def test_real_stdio_handshake_schema_and_dispatch(self):
        root = Path(__file__).resolve().parents[1]
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "capabilities", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "start_print", "arguments": {"unexpected": True}}},
        ]
        p = subprocess.run([str(root / "scripts/run")], input="\n".join(map(json.dumps, frames)) + "\n", text=True, capture_output=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        replies = {x["id"]: x for x in map(json.loads, p.stdout.splitlines())}
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "creality-print-mcp")
        self.assertGreater(len(replies[2]["result"]["tools"]), 20)
        self.assertFalse(replies[3]["result"]["isError"])
        self.assertTrue("error" in replies[4] or replies[4].get("result", {}).get("isError"))


if __name__ == "__main__":
    unittest.main()

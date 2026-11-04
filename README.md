# Creality Print MCP

Prepare models, configure slicing and send prints to a stock Creality Hi through
an OCaml MCP server built with **mcp-kit 0.2.0**. OrcaSlicer handles slicing;
the server connects directly to the Hi for status, CFS data, upload and print
control. Orca does not need to discover the printer.

The CLI workflow works without an open slicer window. The optional Orca Remote
API workflow edits a running slicer so you can see the agent's changes.

## Run locally

Requires Linux, an opam environment with `mcp-kit` 0.2.0, Python 3.11+, `curl`,
GNU `timeout`, and Creality Print or OrcaSlicer. The server uses the maintained
[mcp-kit repository](https://gitlab.com/nomadic-labs/ai-harness/ocaml-mcp); it does not
implement MCP or JSON-RPC itself.

```sh
opam install . --deps-only
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
dune build @all
cp .env.example .env  # only when .env does not already exist
./scripts/run
```

`scripts/run` reads `.env` and serves MCP over stdin/stdout. It writes no
startup text to stdout. Add this entry to a client that accepts `mcpServers`:

```json
{
  "mcpServers": {
    "creality-print": {
      "command": "/home/mathias/dev/creality-print-mcp/scripts/run"
    }
  }
}
```

This checkout's ignored `.env` contains the Hi's local IP. Tokens belong in
that ignored file or in the client's environment, never in committed config.

## Prepare a print without the GUI

1. Call `capabilities`, then `list_presets` with `query: "Creality Hi"`.
   Choose a machine, process and filament appropriate to the nozzle and material.
   `read_preset` resolves inherited settings. `configure_preset` writes a
   separate resolved JSON preset under the MCP workspace when changes are needed.
2. Call `slice_model` with absolute paths in `model`, `machine`, `process` and
   `filaments` (an array). `backend` accepts `orca` or `creality`; the default
   is Orca when installed. Optional controls are `plate` (0 = all), `arrange`,
   `orient`, `scale`, `rotate_z` and `timeout_seconds` (default 1800).
3. Poll `slice_job` with the returned `job_id` until `succeeded` or `failed`.
   Success requires an exit code of zero and a nonempty `.gcode` artifact.
   `inspect_gcode` reads header information and time/filament estimates.
4. Call `printer_status` to check the Hi and its `boxsInfo` CFS inventory.
   `file_limit` controls how many printer files are returned (default 20).
5. Call `upload_gcode` with the artifact's `path`. It returns `remote_path`
   and a SHA-256 receipt. Upload does **not** start printing.
6. When the user requests a physical print, call `start_print` with that
   `remote_path`. The printer must report `deviceState: 0` (idle). Calibration
   defaults to enabled. For CFS printing, set `use_cfs: true` and provide a
   `mapping` array. Use the actual box/material IDs and color strings returned
   by `printer_status`, rather than guessing from the visible slot number.

Illustrative CFS mapping for the first model filament, mapped to box 1, slot 0:

```json
{
  "id": "T1A",
  "type": "PLA",
  "color": "#0ba552a",
  "boxId": 1,
  "materialId": 0
}
```

The `id` denotes the **model filament**, not the destination slot. Match its
order to the filament presets used for slicing. With `use_cfs: false`, the
server uses the normal print command for external-spool printing.

`start_observed: true` means telemetry reported printing after the command.
A sent command with no observed transition remains unconfirmed: read
`printer_status` before deciding whether to retry. The server sends each start
command once. `pause_print`, `resume_print` and `cancel_print` request the
corresponding operation; check telemetry for the resulting state.

## Work in the Orca GUI

The [AUR `orcaslicer-mcp-bin` package](https://aur.archlinux.org/packages/orcaslicer-mcp-bin)
provides MaxEllis's fork with a local REST API. It is a slicer build, distinct
from the [Python MCP server](https://github.com/MaxEllis/orcaslicer-mcp).
This project uses that build's REST API with mcp-kit as the MCP host.
The upstream Python server is not a runtime dependency.

Launch `orca-slicer-mcp`, finish printer-profile setup, then enable
**Preferences → Remote API → Enable Remote API**. Set `ORCA_API_TOKEN` in `.env`
to its token. The API address defaults to `http://127.0.0.1:13130` and this
adapter accepts loopback origins only. CLI slicing needs neither the token nor
an open GUI. The fork shares the regular OrcaSlicer configuration directory.

Call `orca_status` and `orca_presets`, then select the printer, filament and
print presets with `orca_select_preset` (`type`: `printer`, `filament`, `print`).
Selecting a preset can discard unsaved overrides, so select all presets before
applying changes. `orca_load_model`, `orca_objects`, `orca_transform`,
`orca_arrange` and `orca_orient` prepare the model. Rotations use degrees.
`orca_config` reads settings; `orca_set_config` changes them. `orca_slice`
starts slicing; poll `orca_slice_status`, then use `orca_save_gcode` to save
the resulting file for the inspection/upload/start workflow above.

An Orca connection failure does not imply a Hi connection failure. `orca_*`
tools contact the local slicer; `printer_status` and print controls contact
`CREALITY_PRINTER_HOST` over the stock Creality protocol, not Moonraker.

## Configuration reference

| Variable | Default / purpose |
|---|---|
| `CREALITY_PRINTER_HOST` | Required for the printer; IP/hostname without a scheme or port |
| `CREALITY_WS_PORT` | `9999`, stock Creality telemetry and commands |
| `CREALITY_HTTP_PORT` | `80`, stock Creality upload |
| `CREALITY_GCODE_DIR` | `/mnt/UDISK/printer_data/gcodes`, remote upload path |
| `CREALITY_PRINT_BIN` | Searches `creality-print`, `CrealityPrint` |
| `ORCA_SLICER_BIN` | Searches `orca-slicer-mcp`, `orca-slicer`, `OrcaSlicer` |
| `CREALITY_PROFILE_ROOTS` | Colon-separated roots; default Creality version directories and `~/.config/OrcaSlicer` |
| `CREALITY_MCP_STATE` | `~/.local/state/creality-print-mcp`; jobs, resolved presets, exports and upload receipts |
| `ORCA_API_URL` | `http://127.0.0.1:13130` |
| `ORCA_API_TOKEN` | Required only for GUI API tools |
| `CREALITY_MCP_PYTHON` | Launcher uses `.venv/bin/python` when present, otherwise `python3` |

The server has the local user's filesystem access. Resolved presets and G-code
remain on disk until removed. Deleting an active job directory interrupts its
bookkeeping. Preset tools exclude post-processing/scripts and connection edits;
machine G-code comes from the selected existing machine preset. Transport is
local stdio; this server exposes no network listener.

## Verification and current limits

Run `./scripts/check` for compilation plus the stdio MCP handshake/schema tests,
asynchronous slicing tests and HTTP/WebSocket simulations. These tests do not
contact a physical printer.

Observed on 2026-11-04:

- OrcaSlicer MCP **2.4.2-mcp.13**, using the installed Creality Hi 0.4 mm and
  Hyper PLA presets, sliced a 10 mm cube into 50 layers, estimated **6m 22s**
  and **0.70 g**. The AppImage matched the SHA-256 in the AUR recipe.
- The real Hi accepted stock WebSocket telemetry requests, reported idle and
  exposed its CFS inventory. HTTP upload succeeded and the 146,912-byte cube
  G-code appeared in its file list. No print was started.
- The installed Creality Print **7.1.0.4414** CLI crashed while slicing the cube.
  The server detects this as a failed job; use the tested Orca backend here.
- Physical start, pause/resume/cancel and CFS execution have protocol simulation
  coverage, but have not been exercised on the real printer. The live Orca GUI
  API requires user setup and has not been validated against a running GUI here.

The tested Orca AppImage is available locally as `~/.local/bin/orca-slicer-mcp`;
it was installed without changing system packages. A successful slice is not a
hardware compatibility certification for every material or CFS combination.

Protocol references: CrealityPrint revision
[`254250c`](https://github.com/CrealityOfficial/CrealityPrint/tree/254250c015a5f8c5b9bf3742e89066c8a7095565),
in `src/slic3r/GUI/print_manage/Device/Klipper4408Interface.cpp`,
`RemotePrinterManager.cpp`, and `resources/web/deviceMgr/assets/D-MKTaRJ.js`;
Orca REST routes from
[`MaxEllis/orcaslicer-mcp` client](https://github.com/MaxEllis/orcaslicer-mcp/blob/ae7f9db6e7e8b452bfd1ed05fbd70e1b15354bec/src/orcaslicer_mcp/client.py).

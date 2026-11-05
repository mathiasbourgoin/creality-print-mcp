module S = Mcp_kit.Schema
module T = Mcp_kit.Tool

let str = S.string ~min_length:1 ()
let int = S.integer ~minimum:0. ()
let bool = S.bool ()
let obj = `Assoc [ ("type", `String "object") ]
let strings = S.array ~max_items:32 str
let enum xs = S.enum (List.map (fun x -> `String x) xs)

(* The MCP wire, lifecycle and schema validation are entirely owned by mcp-kit.
   The application helper owns slicer processes and the vendor LAN protocol. *)
let invoke name args =
  let input = Filename.temp_file "creality-mcp-" ".in" in
  let output = Filename.temp_file "creality-mcp-" ".out" in
  Fun.protect
    ~finally:(fun () -> List.iter (fun p -> try Sys.remove p with Sys_error _ -> ()) [input; output])
    (fun () ->
      let oc = open_out_bin input in
      Fun.protect ~finally:(fun () -> close_out oc) (fun () ->
        Yojson.Safe.to_channel oc (`Assoc [("tool", `String name); ("arguments", args)]));
      let helper = match Sys.getenv_opt "CREALITY_MCP_HELPER" with
        | Some p -> p | None -> failwith "Use scripts/run or set CREALITY_MCP_HELPER" in
      let python = Option.value (Sys.getenv_opt "CREALITY_MCP_PYTHON") ~default:"python3" in
      let infd = Unix.openfile input [Unix.O_RDONLY] 0 in
      let outfd = Unix.openfile output [Unix.O_WRONLY; Unix.O_TRUNC] 0o600 in
      let pid = Fun.protect ~finally:(fun () -> Unix.close infd; Unix.close outfd)
          (fun () -> Unix.create_process "timeout"
            [|"timeout"; "--kill-after=2"; "145"; python; helper|] infd outfd Unix.stderr) in
      let _, status = Unix.waitpid [] pid in
      match status with
      | Unix.WEXITED 0 ->
          let reply = Yojson.Safe.from_file output in
          let error = Yojson.Safe.Util.member "error" reply in
          if error <> `Null then Ok (T.error_text (Yojson.Safe.Util.to_string error))
          else
            let picture = Yojson.Safe.Util.member "image_base64" reply in
            let reply = match reply with `Assoc xs -> `Assoc (List.remove_assoc "image_base64" xs) | x -> x in
            let content = match picture with
              | `String data -> [T.Text (Yojson.Safe.to_string reply); T.image_content ~mime_type:"image/png" ~data]
              | _ -> [T.Text (Yojson.Safe.to_string reply)] in
            Ok {T.content; is_error = false; structured_content = Some reply}
      | _ -> Ok (T.error_text "Backend interrupted or timed out; inspect job/printer status before retrying a mutation."))

let tool name description required properties =
  T.make ~description
    ~input_schema:(S.object_ ~properties ~required ~additional_properties:false ()) name
    (fun args -> try invoke name args with exn -> Ok (T.error_text (Printexc.to_string exn)))

let tools = [
  tool "capabilities" "Inspect installed slicers, profile roots and configured connections. No printer commands." [] [];
  tool "list_presets" "Find local Creality/Orca machine, process and filament presets." []
    ["query",str; "kind",enum ["machine";"process";"filament"]; "limit",S.integer ~minimum:1. ~maximum:200. ()];
  tool "read_preset" "Read a preset with inheritance resolved. Returns effective settings with secrets redacted." ["path"] ["path",str];
  tool "configure_preset" "Create an independent resolved JSON preset with overrides in the MCP workspace; leaves GUI presets intact." ["path";"overrides"]
    ["path",str; "overrides",obj];
  tool "slice_model" "Start an asynchronous local Creality Print or OrcaSlicer CLI job. Poll slice_job. Requires explicit machine/process/filament presets; inherited presets are flattened." ["model";"machine";"process";"filaments"]
    ["model",str; "machine",str; "process",str; "filaments",strings;
     "backend",enum ["creality";"orca"]; "plate",int; "arrange",bool; "orient",bool;
     "allow_mix_temp",bool;
     "scale",S.number ~minimum:0.001 ~maximum:100. (); "rotate_z",S.number ();
     "timeout_seconds",S.integer ~minimum:10. ~maximum:7200. ()];
  tool "slice_job" "Read slicing state, output files and log tail. No printer commands." ["job_id"] ["job_id",str];
  tool "inspect_gcode" "Read G-code header and slicer estimates without executing it." ["path"] ["path",str];
  tool "printer_status" "Read stock Creality Hi LAN WebSocket telemetry, files and CFS data. Requires CREALITY_PRINTER_HOST. File list defaults to 20 entries." []
    ["file_limit",S.integer ~minimum:0. ~maximum:200. ()];
  tool "printer_cameras" "Read camera configuration from the configured Hi's Moonraker service." [] [];
  tool "set_cfs_material" "Set metadata of one present, editable CFS slot while idle. Sends existing RFID and pressure values; firmware may reassign its catalog identifier. Does not heat, feed, or start printing. Verify with printer_status." ["box_id";"material_id";"material_type";"vendor";"name";"color";"min_temp";"max_temp"]
    ["box_id",S.integer ~minimum:1. ~maximum:4. (); "material_id",S.integer ~minimum:0. ~maximum:3. ();
     "material_type",enum ["PLA";"PETG";"ABS";"ASA";"PET";"HIPS";"PVA";"BVOH"];
     "vendor",str; "name",str; "color",str; "min_temp",int; "max_temp",int];
  tool "configure_printer_camera" "Register the Hi's existing port-8000 WebRTC page as an HTTP Page camera in Moonraker/Fluidd. Preserves other cameras and reuses an existing matching entry." [] [];
  tool "upload_gcode" "Upload a local .gcode to the configured stock Creality printer over HTTP. Does not start printing." ["path"] ["path",str];
  tool "start_print" "Start an uploaded file on an idle stock Creality Hi. Returns observed state or unconfirmed; never automatically retries. CFS requires explicit filament mapping." ["remote_path"]
    ["remote_path",str; "calibration",bool; "use_cfs",bool;
     "mapping",S.array ~min_items:1 ~max_items:16
       (S.object_ ~required:["id";"type";"color";"boxId";"materialId"]
          ~properties:["id",str;"type",str;"color",str;"boxId",int;"materialId",int]
          ~additional_properties:false ())];
  tool "pause_print" "Request printer pause; verify resulting state with printer_status." [] [];
  tool "resume_print" "Request printer resume; verify resulting state with printer_status." [] [];
  tool "cancel_print" "Request cancellation of the current physical print; verify with printer_status." [] [];
  tool "orca_status" "Read OrcaSlicer Remote API state. Requires the AUR MCP fork running with Remote API enabled and ORCA_API_TOKEN." [] [];
  tool "launch_orca" "Open the OrcaSlicer MCP GUI. When closed, enable its loopback Remote API with a configuration backup. Reuses the local API token without displaying it." []
    ["enable_api",bool];
  tool "orca_render_plate" "Save a real PNG render of the running Orca plate to the MCP workspace, for visual review." []
    ["view",enum ["editor";"preview"]; "angle",enum ["iso";"top";"front";"left";"right";"rear";"bottom"]];
  tool "orca_presets" "List presets from the running OrcaSlicer MCP fork." [] [];
  tool "orca_objects" "List objects and placement in the running OrcaSlicer." [] [];
  tool "orca_delete_object" "Remove one object from the open plate by its ID; leaves the source model file intact." ["object_id"] ["object_id",int];
  tool "orca_job_status" "Read progress of background arrangement/orientation in the running OrcaSlicer." [] [];
  tool "orca_load_model" "Load a model into the running OrcaSlicer GUI." ["path"] ["path",str];
  tool "orca_select_preset" "Select a printer, process or filament preset in the running OrcaSlicer." ["type";"name"]
    ["type",enum ["printer";"print";"filament"];"name",str];
  tool "orca_config" "Read effective settings in running OrcaSlicer, with credentials redacted." [] [];
  tool "orca_set_config" "Update print settings in running OrcaSlicer; scripts/G-code/connection edits are excluded." ["changes"] ["changes",obj];
  tool "orca_transform" "Transform an object. Translation (mm) and rotation (degrees) are relative increments; scale is absolute. Returned rotations are degrees." ["object_id";"transform"]
    ["object_id",int;"transform",S.object_ ~properties:["translate",S.array ~min_items:3 ~max_items:3 (S.number ());"rotate",S.array ~min_items:3 ~max_items:3 (S.number ());"scale",S.array ~min_items:3 ~max_items:3 (S.number ())] ~additional_properties:false ()];
  tool "orca_arrange" "Arrange models on the plate in running OrcaSlicer." [] [];
  tool "orca_orient" "Start automatic orientation in running OrcaSlicer." [] [];
  tool "orca_slice" "Start GUI slicing asynchronously in running OrcaSlicer; poll orca_slice_status." [] [];
  tool "orca_slice_status" "Read GUI slicing progress/results." [] [];
  tool "orca_cancel_slice" "Cancel slicing in OrcaSlicer; does not contact the printer." [] [];
  tool "orca_save_gcode" "Save the current successful OrcaSlicer slice to the MCP workspace for inspection and upload." [] [];
]

let () =
  let server = Mcp_kit.Server.create ~name:"creality-print-mcp" ~version:"0.1.0"
      ~instructions:"Prepare and inspect a slice before uploading. Upload and physical start are separate tools. Only start a print when requested by the user. Check the selected machine, filament and CFS mapping. A timeout is not a confirmed failure: inspect printer status before any retry."
      ~legacy_response_policy:Mcp_kit.Server.protocol_2025_06 () in
  match Mcp_kit.Server.add_tools server tools with
  | Ok server -> Mcp_kit_stdio.run_channels server stdin stdout
  | Error _ -> failwith "Duplicate MCP declaration"

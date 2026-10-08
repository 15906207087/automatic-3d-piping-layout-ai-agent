# CAD-MCP Server (CAD Model Context Protocol Server)

The repository homepage is the parent [`README.md`](../README.md). This file documents the `CAD-MCP/` subproject only.

## Project Introduction

CAD-MCP is a **Model Context Protocol (MCP)** CAD control service that lets AI assistants (such as Cursor and Claude Desktop) or natural-language instructions drive CAD software for drafting and 3D piping layout. It combines NLP, CAD automation, and parametric scripts so users can create and modify drawings without manually operating the CAD UI.

## Features

### CAD software support

- AutoCAD
- GstarCAD (GCAD / GstarCAD)
- ZWCAD

### 2D basic drawing

- Line drawing (`draw_line`)
- Circle drawing (`draw_circle`)
- Arc drawing (`draw_arc`)
- Ellipse drawing (`draw_ellipse`)
- Rectangle drawing (`draw_rectangle`)
- Polyline drawing (`draw_polyline`)
- Spline drawing (`draw_spline`)
- Single-line text (`draw_text`)
- Multi-line text (`draw_mtext`)
- Hatch pattern (`draw_hatch`)
- Dimension annotation (`add_dimension`): aligned, horizontal, vertical, diameter, radius
- Batch dimensions (`apply_dimensions`)

### 3D solid drawing

- Sphere (`draw_sphere`)
- Box / cube (`draw_box`)
- Cylinder along Z (`draw_cylinder`)
- Cylinder along Y (`draw_cylinder_along_y`)
- Cone (`draw_cone`)
- Torus (`draw_torus`)
- Wedge (`draw_wedge`)
- Hemisphere (`draw_hemisphere`)
- Hemisphere extruded along Y (`draw_hemisphere_extruded_y`)

### 3D solid editing

- Boolean union (`boolean_union`)
- Boolean intersection (`boolean_intersect`)
- Boolean subtraction (`boolean_subtract`)
- Rotate entity (`rotate_entity`)
- Extrude (`extrude_entity`, by height or along a path)
- Revolve (`revolve_entity`)

### Views and drawing management

- Zoom extents (`zoom_extents`)
- Named views (`set_named_view`): front / top / left / right / back / bottom, plus SW / SE / NE / NW isometric, etc.
- Layer, color, and lineweight options on drawing tools
- Create / switch layers via natural language (`process_command`)
- Save drawing as DWG (`save_drawing`)

### National-standard piping fittings

- Pipe elbow (`draw_pipe_elbow`): GB/T 12459; 45° / 90° / 180°; LR / SR / 3D; DN-linked wall thickness for hollow elbows
- Reducer (`draw_reducer`): concentric / eccentric; GB/T 12459
- Tee (`draw_tee`): equal / reducing; GB/T 12459; hollow bore by default
- Flange (`draw_flange`): GB/T 9124.1 PN series / GB/T 9124.2 Class series; DN / PN lookup or custom sizes
- Blind flange (`draw_blind_flange`): GB/T 9124.1 / GB/T 9124.2, etc.
- Manhole (`draw_manhole`): parametric neck, flange, cover, handles / lifting lugs

### Fasteners

- Hex head bolt (`draw_bolt`): GB/T 5782, GB/T 5783; custom sizes also supported
- Hex nut (`draw_hex_nut`): GB/T 6170 / GB/T 6170.1
- Plain washer (`draw_washer`): GB/T 97.1

### Valves

- Ball valve (`draw_ball_valve`): GB/T 12237, GB/T 12221; flanged / threaded / socket-welding / butt-welding
- Butterfly valve (`draw_butterfly_valve`): GB/T 12238, GB/T 12221
- Globe valve (`draw_globe_valve`): GB/T 12235, GB/T 12221
- Check valve (`draw_check_valve`): GB/T 12236, GB/T 12221; swing type preferred

### Instruments, heads, and engineering accessories

- Pressure gauge (`draw_pressure_gauge`): GB/T 1226; Y-60 / Y-100 / Y-150; radial / axial; direct / panel mount
- Elliptical head (`draw_elliptical_head`): GB/T 25198; 2:1 ellipse; optional straight flange
- Rigid waterproof sleeve (`draw_rigid_waterproof_sleeve`): 02S404 Type A, etc.
- Steel bellmouth (`draw_steel_bellmouth`): 02S403 suction bellmouth series
- Double-flange limit expansion joint (`draw_double_flange_limit_expansion_joint`): GB/T 12465; JSSJA-2 / VSSJA-2(B2F), etc.
- Suction bellmouth support (`draw_suction_bellmouth_support`): 02S403 ZA/ZB/ZC/ZD series (e.g. ZB3)

### Drawing recognition, piping semantics, and JSON-driven drawing

- Parse PDF / image drawings into a structured piping semantic model (`parse_drawing_document`)
- Render PDF / images to high-res pictures then run visual analysis (`analyze_document_visual`)
- Apply human review resolutions to the semantic model (`apply_pipeline_review`)
- Draw 3D piping from a confirmed semantic model (`draw_pipeline_from_json`)

### Vision loop (optional, disabled by default)

- Get latest vision feedback (`get_latest_vision_feedback`)
- Analyze the current CAD view on demand (`analyze_current_view`)
- Enable / disable the vision polling loop (`set_vision_loop_enabled`)
- MCP resources: `drawing://current`, latest vision feedback, vision jobs `vision://jobs/<job_id>`
- Configured under `vision` in `src/config.json`; API keys come from environment variables (e.g. `OPENROUTER_API_KEY`) and must not be committed

### Natural language processing

- Natural-language command processing (`process_command`)
- Parse commands into CAD operation parameters
- Color recognition applied to drawing objects
- Shape keyword mapping
- Action keyword mapping (including save, views, layers, etc.)

### MCP Prompt

- `cad-assistant`: assistant prompt template for controlling CAD via natural language

### Piping connection and parametric support libraries (source modules)

- `pipeline_connection_helper.py`: orthogonal elbow connections—ports, tangent points, rotations, break-pipe skeletons
- `pipeline_models.py`: piping data models
- `pipeline_builder.py`: piping construction logic
- `drawing_parser.py`: drawing document parsing
- `global_drawing_experience.py`: cross-task drawing experience store
- `drawing_validation/`: validation context, rules, and reports
- `screen_capture.py` / `vision_loop.py` / `vision_provider.py`: capture and vision loop
- `archive_output_versions.py`: local `output/` version-folder archiving helper
- `cad_controller.py`: CAD COM controller and national-standard part implementations

### Parametric cases and validation scripts (`src/parametric_models/`)

- `basic_solids_one_dwg_demo`: basic 3D solids one-drawing demo
- `boolean_spheres_demo`: boolean spheres demo
- `standard_parts_grid_demo`: standard-parts grid demo
- `three_valves_one_dwg_demo`: three valves in one drawing demo
- `dn250_elbow_demo`: DN250 elbow demo
- `dn250_elbow_angle_suite`: DN250 elbow angle suite
- `dn250_standard_elbow_suite`: DN250 standard elbow suite
- `dn250_tee_break_demo`: DN250 tee break-pipe demo
- `ee_left_elbow_helper_probe`: elbow helper probe script
- `globe_valve_view_validation`: globe valve view validation
- `butterfly_valve_left_view_validation`: butterfly valve left-view validation
- `manhole_standard_validation`: manhole standard validation
- `compressed_air_tank_parametric`: compressed-air tank parametric model
- `compressed_air_tank_parametric_from_scratch`: compressed-air tank from scratch
- `raw_water_tank_parametric`: raw-water tank parametric model
- `cycle_water_equipment_from_json`: circulating-water equipment from JSON
- `cycle_water_pipeline_from_json`: circulating-water piping from JSON
- `cycle_water_section_route_validation`: circulating-water section route validation
- `cycle_water_dd_rebuilt`: D-D section rebuild
- `dd_section_from_scratch`: D-D section from scratch
- `cycle_water_ff_hh_local_rebuilt`: F-F / H-H local rebuild
- `cycle_water_aa_right_validation`: A-A right-side validation
- `cycle_water_aa_p0204_validation`: A-A / P0204 validation
- `cycle_water_pump_complete_local`: circulating-water pump local complete draw
- `cycle_water_y0202_tank_ii_validation`: Y0202 tank II validation

### Optional sidecar: LangChain memory + iterative codegen (does not affect main path)

Recognize / write JSON / draw / check / redraw still go through Cursor + MCP + `src/` only.  
The sidecar keeps reasoning history and intermediate artifacts for context-aware iterative code drafts:

```text
python -m pip install -r requirements-langchain-optional.txt
python -m experimental.langchain_pilot
```

See `experimental/langchain_pilot/README.md`.

## Demo

![Demo](imgs/demo.gif)

## Installation Requirements

### Dependencies

```text
pywin32>=228         # Windows COM interface support
mcp>=0.1.0           # Model Context Protocol library
pydantic>=2.0.0      # Data validation
typing>=3.7.4.3      # Type annotation support
PyMuPDF>=1.24.0      # PDF parsing
Pillow>=10.0.0       # Image processing / capture related
```

Optional LangChain sidecar dependencies: `requirements-langchain-optional.txt`.

### System requirements

- Windows operating system
- Installed CAD software (AutoCAD, GstarCAD, or ZWCAD)
- Python 3.x

Suggested setup from this directory (`CAD-MCP/`):

```text
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

The configuration file is located at `src/config.json` and contains the following main settings:

```json
{
    "server": {
        "name": "CAD MCP Server",
        "version": "1.0.0"
    },
    "cad": {
        "type": "AUTOCAD",
        "startup_wait_time": 20,
        "command_delay": 0.5,
        "boolean_union": 0,
        "boolean_intersection": 1,
        "boolean_subtraction": 2
    },
    "output": {
        "directory": "./output",
        "default_filename": "cad_drawing.dwg"
    },
    "vision": {
        "enabled": false,
        "provider": "openrouter",
        "model": "openai/gpt-5.4",
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "app_name": "CAD-MCP",
        "site_url": "https://cursor.local",
        "poll_interval_ms": 2000,
        "diff_threshold": 0.0,
        "timeout_sec": 60,
        "capture_output_dir": "./output/vision_captures"
    }
}
```

- **server**: server name and version
- **cad**:
  - `type`: CAD software type (AUTOCAD, GCAD, GstarCAD, or ZWCAD)
  - `startup_wait_time`: CAD startup wait time (seconds)
  - `command_delay`: command execution delay (seconds)
  - `boolean_*`: boolean operation type codes
- **output**: output directory and default filename
- **vision**: vision-loop settings (off by default; secrets via env vars only)

## Usage

### Starting the service

```text
python src/server.py
```

### Claude Desktop & Windsurf

```json
{
    "mcpServers": {
        "CAD": {
            "command": "python",
            "args": [
                "C:\\path\\to\\CAD-MCP\\src\\server.py"
            ]
        }
    }
}
```

### Cursor

Configure Cursor MCP as shown (use your local path):

![Cursor config](imgs/cursor_config.png)

Note: newer Cursor versions also use JSON configuration; see the previous section.

### MCP Inspector

```text
npx -y @modelcontextprotocol/inspector python C:\\path\\to\\CAD-MCP\\src\\server.py
```

### Service API (MCP Tools, complete list)

#### 2D drawing

- `draw_line`: Draw a line
- `draw_circle`: Draw a circle
- `draw_arc`: Draw an arc
- `draw_ellipse`: Draw an ellipse
- `draw_polyline`: Draw a polyline
- `draw_rectangle`: Draw a rectangle
- `draw_spline`: Draw a spline
- `draw_text`: Add single-line text
- `draw_mtext`: Add multi-line text
- `draw_hatch`: Draw a hatch
- `add_dimension`: Add a dimension
- `apply_dimensions`: Apply dimensions in batch

#### 3D solids and editing

- `draw_sphere`: Draw a sphere
- `draw_box`: Draw a box / cube
- `draw_cylinder`: Draw a cylinder (along Z)
- `draw_cylinder_along_y`: Draw a cylinder along Y
- `draw_cone`: Draw a cone
- `draw_torus`: Draw a torus
- `draw_wedge`: Draw a wedge
- `draw_hemisphere`: Draw a hemisphere
- `draw_hemisphere_extruded_y`: Hemisphere extruded along Y
- `boolean_union`: Boolean union
- `boolean_intersect`: Boolean intersection
- `boolean_subtract`: Boolean subtraction
- `rotate_entity`: Rotate an entity
- `extrude_entity`: Extrude
- `revolve_entity`: Revolve

#### Fittings, fasteners, valves, and accessories

- `draw_pipe_elbow`: Pipe elbow
- `draw_reducer`: Reducer
- `draw_tee`: Tee
- `draw_flange`: Flange
- `draw_blind_flange`: Blind flange
- `draw_manhole`: Manhole
- `draw_bolt`: Bolt
- `draw_hex_nut`: Hex nut
- `draw_washer`: Washer
- `draw_ball_valve`: Ball valve
- `draw_butterfly_valve`: Butterfly valve
- `draw_globe_valve`: Globe valve
- `draw_check_valve`: Check valve
- `draw_pressure_gauge`: Pressure gauge
- `draw_elliptical_head`: Elliptical head
- `draw_rigid_waterproof_sleeve`: Rigid waterproof sleeve
- `draw_steel_bellmouth`: Steel bellmouth
- `draw_double_flange_limit_expansion_joint`: Double-flange limit expansion joint
- `draw_suction_bellmouth_support`: Suction bellmouth support

#### Recognition, piping, vision, and general

- `parse_drawing_document`: Parse a drawing document
- `analyze_document_visual`: Visual analysis of a document
- `apply_pipeline_review`: Apply human piping review
- `draw_pipeline_from_json`: Draw piping from a JSON semantic model
- `get_latest_vision_feedback`: Get latest vision feedback
- `analyze_current_view`: Analyze the current view
- `set_vision_loop_enabled`: Enable / disable vision polling
- `process_command`: Process a natural-language command
- `save_drawing`: Save the drawing
- `zoom_extents`: Zoom extents
- `set_named_view`: Switch named view

## Project Structure

```text
CAD-MCP/
├── imgs/                              # Images and demo assets
│   ├── demo.gif
│   └── cursor_config.png
├── experimental/                      # Optional LangChain pilot
│   └── langchain_pilot/
├── requirements.txt                   # Main service dependencies
├── requirements-langchain-optional.txt
├── README_zh.md                       # CAD-MCP Chinese documentation
├── README_en.md                       # CAD-MCP English documentation
├── output/                            # Drawing files: shared/ and piping drawings, archived by version
└── src/                               # Source code
    ├── __init__.py
    ├── server.py                      # MCP server entry & tool registration
    ├── cad_controller.py              # CAD controller & standard parts
    ├── config.json                    # Configuration
    ├── nlp_processor.py               # Natural language processor
    ├── drawing_parser.py              # Drawing document parsing
    ├── pipeline_builder.py            # Converts piping semantic models into CAD calls
    ├── pipeline_connection_helper.py  # Fitting ports, tangent points, pipe breaks, elbow connections
    ├── pipeline_models.py             # Piping semantic model, BOM, dimension ledger data structures
    ├── global_drawing_experience.py   # Cross-task drawing experience store
    ├── screen_capture.py              # CAD view screenshots and preview output
    ├── vision_loop.py                 # Current-view vision polling and analysis loop
    ├── vision_provider.py             # Vision model provider wrapper
    ├── archive_output_versions.py     # Local output version-folder archiving helper
    ├── drawing_validation/            # Drawing validation
    └── parametric_models/             # Parametric cases & validation scripts
```

`output/` stores drawing files (`shared/` and piping drawings), archived by
version. Local `venv/`, logs, and caches are not meant for version control.

## License

Non-Commercial Academic Use License. Academic, teaching, research, and personal
non-commercial use are permitted. Commercial use requires separate written
permission. See `LICENSE` in the parent directory.

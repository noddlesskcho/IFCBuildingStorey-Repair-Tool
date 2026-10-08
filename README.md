# IFCBuildingStorey Repair Tool

A Windows desktop tool for Revit IFC exports. It finds linked-model instances
that Revit exported as separate IFC spatial branches. It then moves their
elements onto the correct master `IfcBuildingStorey`, block by block, and
writes a verified copy of the IFC. The original file is never changed.

- Works with any number of blocks in one file: towers, podiums, site models.
- Matches storeys by real elevation, never by level name.
- Shows each block on a plan, so you can see the result before you export.
- Lets you draw your own block outlines when the automatic result is wrong.
- Checks every export: same elements, same GUIDs, same geometry, same position.

## Contents

- [The problem](#the-problem)
- [Quick start](#quick-start)
- [Using the app](#using-the-app)
- [Output files](#output-files)
- [Command line](#command-line)
- [How it works](#how-it-works)
- [Limits](#limits)
- [For developers](#for-developers)

## The problem

A typical unit is often modelled in its own Revit file. The unit is then linked
into a master model and copied to many floors. When the master model is
exported to IFC, Revit can write each link copy as its own branch instead of
placing its elements on the master storeys:

```
IfcProject
└─ IfcSite "Default"
   ├─ IfcBuilding (master)         1st … 5th Storey         <- the storeys you want
   ├─ IfcBuilding (link @ Z 0)     Base Level / Top Level   <- unit elements are here
   ├─ IfcBuilding (link @ Z 3600)  Base Level / Top Level
   └─ ...
```

Some exports also write one extra `IfcSite` for each link copy. The tool
handles both layouts.

The level names inside a link (`Base Level`, `1st Storey`, ...) describe the
linked file, not where the copy sits in the master model. A unit on the 4th
storey can still hold its elements under `Base Level`. **The tool never matches
by name.** It uses only the real (absolute) elevation and the IFC relationships.

## Quick start

1. Install **Python 3.10 or newer** from https://www.python.org/downloads/.
   Tick **"Add python.exe to PATH"** during setup. The launcher also finds a
   normal per-user install without PATH.
2. Download this repository: **Code > Download ZIP** on GitHub, then unzip it.
   Or clone it:
   ```powershell
   git clone https://github.com/noddlesskcho/IFCBuildingStorey-Repair-Tool.git
   ```
3. Double-click **`run_app.bat`**. On the first run it installs the required
   packages (IfcOpenShell, numpy). This needs an internet connection.

## Using the app

### Layout

- **Left sidebar:**
  - **Model:** file name, folder, size, IFC schema and units. Use **Open IFC...**
    to load a file and **Re-analyse** after you change a setting.
  - **Detection:**
    - **Blocks:** keep **Auto**, or pick one building to use as the only master.
    - **Match linked levels by:** **Each level Z** (default) or **Building Z**.
    - **Elevation tolerance:** in metres. The default is 0.3.
  - **Output:** **Remove empty linked branches** and **Rebase placements to
    master storey**. Both are on by default.
  - **Export fixed IFC:** writes the repaired file.
- **Summary cards:** Buildings, Blocks, Linked branches, Elements to move and
  Warnings.
- **Status label** (top right): No file, Analysing, Repair needed, No repair
  needed, Exported or Failed.

### Steps

1. Click **Open IFC...** and choose the file. Analysis starts by itself. A
   95 MB model takes about 35 seconds.
2. Check the **Block plan** tab first:
   - Each element is drawn in plan and coloured by its block. Block masters are
     light. Their linked units are dark.
   - Linked units that were left out are red.
   - Hover over an element to see its building, its role and why it joined its
     block.
   - Use the mouse wheel to zoom and right-drag to pan. **Fit view** resets the
     view.
3. Check the other tabs as needed:
   - **Buildings:** every building with its role, block, elevation and reason.
     Double-click a linked building to include or exclude it.
   - **Changes:** each linked container and the master storey it goes to, with
     the elevation difference. Rows marked `CHECK` are outside the tolerance.
   - **Elements:** every element that will move.
   - **Report:** the full detection summary.
4. If the blocks are wrong, draw them yourself in the **Block plan** tab:
   1. Click **Draw block boundary**. Then click each corner of the outline.
      Double-click or press Enter to finish. Backspace removes the last corner.
      Esc cancels.
   2. In the dialog, give the block a name and choose its master (the building
      that receives the elements). **Auto** picks the tallest building inside
      the outline.
   3. Draw more outlines if you need them. Then click **Apply drawn boundaries**.
      Re-analysis takes about 10 seconds.
   4. A building with at least 60% of its elements inside an outline joins that
      block. Buildings outside every outline are still detected automatically.
   5. Right-click an outline to delete it. **Clear drawn** removes all outlines.
   6. **Save...** stores the outlines as JSON for the next export of the same
      project. **Load...** brings them back.
5. Click **Export fixed IFC** and choose the output file. The tool refuses to
   save over the input file. Export with full checks takes about 3 minutes on
   a 95 MB model.
6. The **Export** tab shows the output files and each check as `PASS` or `FAIL`.

## Output files

Each export writes four files next to each other:

| File | Contents |
|---|---|
| `<name>.ifc` | The repaired model |
| `<name>.changelog.csv` | One row per moved element: GUID, class, old container, new storey, and whether its placement was rebased |
| `<name>.report.md` | Summary, mapping table, removed spatial elements and check results |
| `<name>.blocks.svg` | Plan of every element coloured by block. Open it in a browser to confirm each unit sits inside its own block |

The default output name is `<input>_storey-fixed.ifc`.

## Command line

```powershell
python -m ifc_linked_repair analyze "Master File.ifc"
python -m ifc_linked_repair analyze "Master File.ifc" --json
python -m ifc_linked_repair repair "Master File.ifc" -o "Master File_fixed.ifc"
```

Options for `repair`:

| Option | Meaning |
|---|---|
| `--mode storey\|branch` | Match each linked level by its own Z (`storey`, default) or the whole linked building by its Z (`branch`) |
| `--tolerance 0.3` | Elevation match tolerance in metres |
| `--containment 0.6` | Share of a building's elements that must lie inside a block boundary |
| `--boundary-margin 1.0` | Margin in metres around a block boundary |
| `--boundaries zones.json` | Block outlines drawn and saved in the app |
| `--no-geometry` | Skip tessellation and find blocks from link origins only |
| `--master-id N` | Use building `#N` as the only master (single-block mode) |
| `--include ID ...` | Treat exactly these `IfcBuilding` STEP ids as linked, instead of the automatic choice |
| `--keep-branches` | Keep the emptied linked storeys and buildings |
| `--no-rebase` | Keep element placements relative to the old linked storeys |
| `--force` | Replace an existing output file. The input file is never replaced |

## How it works

### Detection

1. Read the spatial tree: `IfcProject`, `IfcSite`, `IfcBuilding` and
   `IfcBuildingStorey` through `IfcRelAggregates`. Read the elements in each
   container through `IfcRelContainedInSpatialStructure`,
   `IfcRelReferencedInSpatialStructure` and aggregated children such as
   `IfcSpace`.
2. Work out the absolute Z of every building and storey from its full
   `IfcLocalPlacement` chain. The `Elevation` attribute is shown for
   information only, because Revit writes it relative to the linked building.
3. Split the buildings into **blocks**. Examples: a site model that links
   Block A and Block B, or three tower models on one podium, each linking its
   typical units.
   - **Block boundary from real geometry.** Every element is tessellated in
     world coordinates, with openings ignored. Each element becomes the convex
     hull of its plan outline. A building's boundary is built from these hulls:
     1. Rasterise the hulls onto a 0.5 m plan grid.
     2. Bridge gaps up to 3 m.
     3. Fill holes.
     4. Grow the result by 1 m (`--boundary-margin`).

     The boundary follows the real outline: L shapes, crosses, rotated or
     neighbouring towers. It is not a bounding box. Element placement points
     are not used, because Revit often writes them at the link origin, far
     from the element.
   - **Drawn boundaries come first.** A building with at least 60% of its
     element centres inside a drawn outline joins that outline's block. The
     automatic rules below skip it.
   - **Containment.** A building joins the smallest host that has more storeys
     and holds at least 60% (`--containment`) of its element centres inside
     the host's boundary. Chains resolve to the top block, so a unit inside a
     unit inside a tower lands in the tower. A podium or site model with fewer
     storeys never takes over a tower.
   - **Link origin fallback.** This rule applies only to buildings that the
     geometry could not place. A stacked copy follows its sibling that is
     already placed. A partial-height branch at a taller building's origin
     joins that building. A building that already holds branches is a block
     and is never merged by origin. This keeps tower models that share one
     project origin apart.
   - The top building of each group is the block master. Any other building
     forms its own block and receives nothing, for example a site model with
     boundary walls.
   - `--master-id`, or choosing a building under **Blocks** in the app,
     switches to single-block mode, where every other building is a branch of
     that one.
4. In each block, every other building is a linked branch. A branch is
   included automatically when each of its filled containers sits within the
   tolerance (default 300 mm) of one of its block's storeys. Revit link levels
   often sit 50 to 100 mm (a finish thickness) below the host storey. A branch
   that does not align is excluded and reported. You can include it by hand.
5. Map each container to the nearest storey of its own block by absolute Z.
   - **Each level Z** (`storey` mode, default): each linked level is matched
     on its own.
   - **Building Z** (`branch` mode): the whole linked building is matched by
     its placement Z.
   - Elements held directly by a linked `IfcBuilding` are matched by the
     building Z.
   - Elements held directly by a linked-only `IfcSite` move to the master site.

### Repair

- Contained elements of every IFC class are merged into the master storey's
  `IfcRelContainedInSpatialStructure`. A new relationship is created if the
  storey has none.
- Aggregated children of linked storeys, such as `IfcSpace`, move to the
  master storey's `IfcRelAggregates`.
- Spatial references point to the master storey.
- **Rebase placements** (on by default): an element placed relative to a
  linked storey or building is placed relative to the master storey instead.
  The new relative transform is `inverse(storey) x old_absolute`, so the
  absolute position stays the same. Elements placed relative to other
  elements, such as a door in its opening, are left as they are.
- **Remove empty linked branches** (on by default): the emptied linked storeys
  and buildings are removed, with any linked-only sites, their property sets
  and aggregation links. A placement is removed only when nothing else uses it.
- The tool does not touch geometry, GUIDs, names, types, materials, property
  sets, openings, fills or any other non-spatial relationship.

### Checks on every export

- Every product is still present, with the same class, GlobalId and Name.
- The absolute placement of every product is unchanged (tolerance 1 µm).
- Representation entities are unchanged.
- Moved elements are tessellated in world coordinates and their vertices
  compared, so the model looks the same.
- Each moved element now sits in its expected master storey.
- The original contents of the master storeys are kept.
- All non-spatial relationships are compared by GUID and related entities.
  None may be lost or added.
- Detection on the output finds no linked elements left.

## Limits

- Detection tessellates the whole model once. On a 95 MB model with 417
  buildings, analysis takes about 35 seconds and export with all checks takes
  about 3 minutes.
- Only `IfcBuilding` branches are treated as links. Linked models already
  exported onto the master storeys need no repair.
- A linked element that spans several storeys, such as a stair core, moves
  with its container. The tool does not split elements.
- Mappings outside the tolerance are marked `CHECK`. The app asks you to
  confirm before export.

## For developers

### Requirements

- Python 3.10 or newer
- IfcOpenShell 0.8 or newer and numpy (`pip install -r requirements.txt`)
- Tkinter (included with the standard Windows Python installer)
- pytest, to run the tests

Start the app without the launcher:

```powershell
python -m ifc_linked_repair
```

### Code layout

The core logic has no UI dependency.

| Module | Role |
|---|---|
| `model.py` | Data classes and options shared by all modules |
| `detection.py` | Reads the spatial tree, resolves absolute Z, groups buildings into blocks, classifies linked branches and builds the plan |
| `footprint.py` | Tessellates elements and builds each block's plan boundary |
| `repair.py` | Applies the plan in memory and writes the output safely |
| `verify.py` | Compares the original and repaired files |
| `reporting.py` | Writes the text summary, CSV change log, Markdown report and SVG block plan |
| `plan_view.py` | Block plan tab: drawing and boundary editing |
| `theme.py` | Colours, fonts and widget styles |
| `ui.py` / `cli.py` | Desktop app and command line |

### Tests

```powershell
python -m pytest -q
```

- `tests/test_repair.py` builds small Revit-like IFC files with
  `tests/synthetic.py`. It covers:
  - a file without links
  - links under one site, and links under separate sites
  - misleading internal level names
  - `branch` mode
  - the keep-branches and no-rebase options
  - a misaligned building that must not be included automatically
  - output file safety
  - several blocks in one file, with shared and separate link origins
  - single-block mode
  - concave block outlines and drawn boundaries
- `tests/test_samples.py` runs the real Revit sample files: a file without
  links, two linked layouts, a site model with two blocks, and three towers on
  a podium. It reads them from the folder in the `IFC_SAMPLE_DIR` environment
  variable, or from
  `C:\Users\ISS\Documents\Corenet X\IfcBuildingStorey Linked File\Test File`.
  These tests are skipped when the files are missing. The sample IFC files are
  not stored in this repository.

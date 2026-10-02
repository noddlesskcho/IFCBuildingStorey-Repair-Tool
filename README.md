# IFC Linked Storey Repair

Desktop tool that finds Revit linked-model instances exported as separate IFC
spatial branches, then reassigns their elements to the correct master
`IfcBuildingStorey` based on absolute elevation. It writes a fixed copy of the
IFC and leaves the original file untouched.

## The problem

When a typical unit is modelled in a separate Revit file, linked into a master
model and copied to several floors, the Revit IFC exporter can write each link
instance as its own branch:

```
IfcProject
└─ IfcSite "Default"
   ├─ IfcBuilding (master)       1st…5th Storey   <- the storeys you want
   ├─ IfcBuilding (link @ Z 0)     Base Level / Top Level  <- unit elements here
   ├─ IfcBuilding (link @ Z 3600)  Base Level / Top Level
   └─ ...
```

Some exports also write one extra `IfcSite` per link instance. The tool handles
both layouts.

The internal linked level names (`Base Level`, `1st Storey`, ...) describe the
linked file, not the place where the link instance sits in the master model. A
unit on the 4th storey can still hold its elements under `Base Level`. **The
tool never matches by name.** It matches only by resolved absolute placement Z
and by the relationship hierarchy.

## Requirements

- Python 3.10 or newer
- IfcOpenShell 0.8 or newer (`pip install -r requirements.txt`)
- Tkinter (included with the standard Windows Python installer)
- pytest, to run the tests

## Run

Desktop UI:

```powershell
python -m ifc_linked_repair
```

You can also double-click `run_app.bat`.

1. Click **Browse...** and select the IFC. Analysis starts automatically.
2. Review the tabs:
   - **Detection report**: number of buildings, master building candidate,
     master storeys with their absolute Z, likely linked branches, element
     counts and warnings.
   - **Buildings**: double-click a linked building to include or exclude it.
   - **Proposed changes**: each linked container mapped to its target master
     storey, with the elevation delta.
   - **Element preview**: every element that will move.
3. Click **Export fixed IFC...** and choose the output file. The tool refuses to
   save over the input file.
4. The **Export log** tab shows the verification results.

Command line:

```powershell
python -m ifc_linked_repair analyze "Master File.ifc"
python -m ifc_linked_repair analyze "Master File.ifc" --json
python -m ifc_linked_repair repair "Master File.ifc" -o "Master File_fixed.ifc"
```

The `repair` command takes these options:

- `--mode storey|branch`
- `--tolerance 0.3`: elevation match tolerance in metres
- `--containment 0.6`: share of a building's elements that must lie inside a block boundary
- `--boundary-margin 1.0`: margin in metres around a block boundary
- `--no-geometry`: skip tessellation and find blocks from link origins only
- `--master-id N`
- `--include ID ...`
- `--keep-branches`
- `--no-rebase`
- `--force`: allows the command to replace an existing *output* file. The input file is never replaced.

Each export writes these files:

- `<name>.ifc`: the fixed model
- `<name>.changelog.csv`: one row per reassigned element, with GUID, class, source container, target storey and whether its placement was rebased
- `<name>.report.md`: summary, mapping table, removed spatial elements and verification results
- `<name>.blocks.svg`: plan view of every element coloured by its assigned block. Open it in a browser to confirm that each typical unit sits inside its own block.

## How the repair works

Code layout (the core logic has no UI dependency):

| Module | Role |
|---|---|
| `detection.py` | Reads the spatial tree, resolves absolute Z values, groups buildings into blocks, classifies linked branches and builds the plan |
| `footprint.py` | Tessellates elements and builds each block's plan boundary |
| `repair.py` | Applies the plan to the model in memory and writes the output safely |
| `verify.py` | Compares the original and repaired files |
| `reporting.py` | Writes the text summary, the CSV change log and the Markdown report |
| `ui.py` / `cli.py` | Front ends |

### Detection

1. Read `IfcProject`, `IfcSite`, `IfcBuilding` and `IfcBuildingStorey` through
   `IfcRelAggregates`. Read the elements in each container through
   `IfcRelContainedInSpatialStructure`, `IfcRelReferencedInSpatialStructure`
   and aggregated non-container children such as `IfcSpace`.
2. Resolve the absolute Z of every building and storey from its full
   `IfcLocalPlacement` chain. The `Elevation` attribute is shown for
   information only, because Revit writes it relative to the linked building.
3. Split the buildings into **blocks**, so one file can hold any number of
   blocks of any size or shape. Examples: a site model that links Block A and
   Block B, or three tower models on one podium, each linking its typical
   units.
   - **Block boundary from real geometry.** The tool tessellates every
     element in world coordinates, with openings ignored. Each element is
     reduced to the convex hull of its plan outline. A building's boundary is
     built from these hulls in four steps:
     1. Rasterise the hulls onto a 0.5 m plan grid.
     2. Bridge gaps up to 3 m.
     3. Fill holes.
     4. Grow the result by 1 m (`--boundary-margin`).

     The boundary follows the real outline: L shapes, crosses, rotated or
     adjacent towers. It is not a bounding box. Element placement points are
     not used, because Revit often writes them at the link origin, far from
     the element.
   - **Containment.** A building joins the smallest host that has more
     storeys and holds at least 60% (`--containment`) of its element centres
     inside the host's boundary. Chains resolve to the top block, so a unit
     inside a unit inside a tower lands in the tower. A podium or site model
     with fewer storeys never absorbs a tower.
   - **Link origin fallback.** This rule applies only to buildings that the
     geometry could not place. A stacked copy follows its sibling that is
     already placed. A partial-height branch, populated on at most half of a
     taller building's storeys and linked at that building's origin, joins
     that building. Example: Site Master, where the block model has no
     geometry of its own. A building that already holds branches is a block
     and is never merged by origin. This keeps several tower models that
     share project coordinates apart.
   - The top building of each group is the block master. Any other building
     forms its own block and receives nothing, for example a site model with
     boundary walls.
   - `--master-id`, or choosing a building in the UI, switches to
     single-block mode, where every other building is a branch of that one.
4. In each block, every other building is a linked branch. A branch is marked
   *likely linked* and included automatically when each populated container
   sits within the tolerance (default 300 mm) of one of its own block's storey
   elevations. Revit link levels often sit a finish thickness (50 to 100 mm)
   off the host storey. A branch that does not align is excluded and
   reported. You can include it manually.
5. Map each container to the nearest storey of its own block by absolute Z.
   - In `storey` mode (the default), each internal linked storey is matched
     separately.
   - In `branch` mode, the whole linked building is matched by its placement Z.
   - Elements held directly by a linked `IfcBuilding` are matched by the
     building Z.
   - Elements held directly by a linked-only `IfcSite` move to the master site.

### Repair

- Contained elements are merged into the master storey's existing
  `IfcRelContainedInSpatialStructure`, or a new one is created if needed.
  This applies to every IFC class.
- Aggregated children of linked storeys, such as `IfcSpace`, move to the
  master storey's `IfcRelAggregates`.
- Spatial references are retargeted to the master storey.
- With **Rebase placements** on (the default), an element whose
  `IfcLocalPlacement` was relative to a linked storey or building is
  re-expressed relative to the master storey placement. The new relative
  transform is `inverse(storey) x old_absolute`, so the absolute transform
  stays identical. Elements placed relative to other elements are left
  unchanged. Their absolute position does not change either, for example a
  door relative to its opening.
- With **Remove empty linked branches** on (the default), the emptied linked
  storeys and buildings are removed, and so are any linked-only sites. This
  includes their own property sets and aggregation links. A placement is
  removed only when nothing else references it.
- The tool does not touch geometry, GUIDs, names, types, materials, property
  sets, openings, fills or any other non-spatial relationship.

### Verification (runs on every export)

- Every product is still present, with the same class, GlobalId and Name.
- The absolute placement matrix of every product is unchanged (tolerance 1 µm).
- Representation entities are unchanged.
- Moved elements are tessellated with IfcOpenShell geometry in world
  coordinates, and their vertices are compared. This shows that the model
  looks the same.
- Each moved element now sits in its expected master storey.
- The original contents of the master storeys are kept.
- All non-spatial relationships are compared by GUID and the entities they
  relate. None may be lost or added.
- Re-detection on the output finds no remaining linked elements.

## Tests

```powershell
python -m pytest -q
```

- `tests/test_repair.py` builds synthetic Revit-like IFCs with
  `tests/synthetic.py`. It covers these cases:
  - a non-linked file
  - links under one site and links under separate sites
  - misleading internal level names
  - `branch` mode
  - the keep-branches and no-rebase options
  - a misaligned building that must not be auto-included
  - output safety
- `tests/test_samples.py` runs the real Revit sample files. It reads them
  from `C:\Users\ISS\Documents\Corenet X\IfcBuildingStorey Linked File\Test File`,
  or from the folder in the `IFC_SAMPLE_DIR` environment variable. These tests
  are skipped when the files are missing.

## Limits

- Detection tessellates the whole model once. On a 95 MB model with 417
  buildings, analysis takes about 35 s and export with full verification
  takes about 3 minutes.
- Only `IfcBuilding` branches are treated as links. Linked models exported
  into the master storeys directly need no repair.
- A linked element that spans several storeys, such as a stair core, moves
  with its container. The tool does not split elements.
- Mappings outside the tolerance are flagged as `CHECK` and need confirmation
  before export.

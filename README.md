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
- `--tolerance 0.05` (metres)
- `--master-id N`
- `--include ID ...`
- `--keep-branches`
- `--no-rebase`
- `--force`: allows the command to replace an existing *output* file. The input file is never replaced.

Each export writes three files:

- `<name>.ifc`: the fixed model
- `<name>.changelog.csv`: one row per reassigned element, with GUID, class, source container, target storey and whether its placement was rebased
- `<name>.report.md`: summary, mapping table, removed spatial elements and verification results

## How the repair works

Code layout (the core logic has no UI dependency):

| Module | Role |
|---|---|
| `detection.py` | Reads the spatial tree, resolves absolute Z values, picks the master building, classifies linked branches and builds the plan |
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
3. Pick the master building. The building with the most storeys wins. Ties go
   first to the building with the most contained elements, then to the
   earliest building in the file. You can override this choice in the UI or
   with `--master-id`.
4. Treat every other building as a linked branch. A branch is marked *likely
   linked* and included automatically when each populated container sits
   within the tolerance (default 50 mm) of a master storey elevation. A branch
   that does not align, for example a genuine second building, is excluded and
   reported. You can include it manually.
5. Map each container to the nearest master storey by absolute Z.
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

- Only `IfcBuilding` branches are treated as links. Linked models exported
  into the master storeys directly need no repair.
- A linked element that spans several storeys, such as a stair core, moves
  with its container. The tool does not split elements.
- Mappings outside the tolerance are flagged as `CHECK` and need confirmation
  before export.

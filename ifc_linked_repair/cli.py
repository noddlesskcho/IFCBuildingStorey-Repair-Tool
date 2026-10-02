"""Command line entry point.

    python -m ifc_linked_repair                     # open the desktop UI
    python -m ifc_linked_repair analyze FILE [--json]
    python -m ifc_linked_repair repair FILE [-o OUT] [options]
"""

from __future__ import annotations

import argparse
import json
import sys

from .detection import analyze
from .model import MODE_BRANCH, MODE_STOREY, RepairOptions
from .repair import OutputSafetyError, repair_file
from .reporting import format_detection


def _options(args) -> RepairOptions:
    return RepairOptions(
        mode=args.mode,
        tolerance_m=args.tolerance,
        boundary_margin_m=args.boundary_margin,
        containment_ratio=args.containment,
        use_geometry=not args.no_geometry,
        remove_empty_branches=not args.keep_branches,
        rebase_placements=not args.no_rebase,
        master_building_id=args.master_id,
        include_building_ids=set(args.include) if args.include else None,
    )


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", help="input IFC file")
    p.add_argument("--mode", choices=[MODE_STOREY, MODE_BRANCH], default=MODE_STOREY,
                   help="storey: match each linked storey by its absolute Z (default); "
                        "branch: match the whole linked building by its placement Z")
    p.add_argument("--tolerance", type=float, default=0.3, help="elevation match tolerance in metres (default 0.3)")
    p.add_argument("--containment", type=float, default=0.6,
                   help="share of a building's elements that must lie inside a block boundary (default 0.6)")
    p.add_argument("--no-geometry", action="store_true",
                   help="do not tessellate geometry; find blocks from link origins only")
    p.add_argument("--boundary-margin", type=float, default=1.0,
                   help="margin in metres around a block's plan boundary (default 1.0)")
    p.add_argument("--master-id", type=int,
                   help="single-block mode: force this IfcBuilding step id as the only master")
    p.add_argument("--include", type=int, nargs="+", metavar="ID",
                   help="IfcBuilding step ids to treat as linked (default: auto-detected)")
    p.add_argument("--keep-branches", action="store_true", help="keep empty linked spatial branches")
    p.add_argument("--no-rebase", action="store_true", help="keep element placements relative to old linked storeys")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .ui import run

        run()
        return 0

    parser = argparse.ArgumentParser(prog="ifc_linked_repair", description="Repair Revit linked-model IFC storeys.")
    sub = parser.add_subparsers(dest="command", required=True)
    pa = sub.add_parser("analyze", help="show detection report")
    _add_common(pa)
    pa.add_argument("--json", action="store_true", help="print the report as JSON")
    pr = sub.add_parser("repair", help="export a fixed IFC")
    _add_common(pr)
    pr.add_argument("-o", "--output", help="output IFC (default: <input>_storey-fixed.ifc)")
    pr.add_argument("--force", action="store_true", help="overwrite an existing output file (never the input)")
    sub.add_parser("ui", help="open the desktop UI")
    args = parser.parse_args(argv)

    if args.command == "ui":
        from .ui import run

        run()
        return 0

    options = _options(args)
    if args.command == "analyze":
        report = analyze(args.file, options)
        print(json.dumps(report.to_dict(), indent=2) if args.json else format_detection(report))
        return 0

    try:
        result = repair_file(args.file, args.output, options, overwrite_output=args.force)
    except OutputSafetyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(format_detection(result.report))
    print()
    for note in result.notes:
        print(f"Note: {note}")
    if not result.output_path:
        return 0
    print(f"Written: {result.output_path}")
    print(f"Change log: {result.changelog_csv}")
    print(f"Report: {result.report_md}")
    if result.block_plan_svg:
        print(f"Block plan: {result.block_plan_svg}")
    if result.verification:
        for c in result.verification.checks:
            print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
        return 0 if result.verification.passed else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Human-readable detection summary, CSV change log and Markdown repair report."""

from __future__ import annotations

import csv
import datetime as _dt
import os

from .model import DetectionReport, RepairResult


def _z(value: float) -> str:
    return f"{value:,.1f}"


def format_detection(report: DetectionReport) -> str:
    u = report.length_unit
    lines = [
        f"File: {report.path}",
        f"Schema: {report.schema}   Length unit: {u}   Tolerance: {_z(report.tolerance_units)} {u}   Mode: {report.mode}",
        f"Projects: {report.project_count}   Sites: {report.site_count}   Buildings: {len(report.buildings)}",
        "",
    ]
    if not report.blocks:
        lines.append("No master building found.")
        return "\n".join(lines)
    by_id = {b.step_id: b for b in report.buildings}
    linked = report.linked_buildings
    lines.append(
        f"Blocks: {len(report.blocks)}   Likely linked branches: "
        f"{sum(b.likely_linked for b in linked)} of {len(linked)} extra building(s)"
    )
    for number, block in enumerate(report.blocks, 1):
        master = by_id[block.master_id]
        lines.append("")
        lines.append(
            f"Block {number} master: #{master.step_id} '{master.name}' ({master.global_id}) "
            f"origin=({_z(master.origin_x)}, {_z(master.origin_y)}) Z={_z(master.z)} {u}"
        )
        if block.boundary:
            x0, y0, x1, y1 = block.boundary
            lines.append(f"  Plan boundary: X {_z(x0)} .. {_z(x1)}   Y {_z(y0)} .. {_z(y1)} {u}")
        lines.append("  Master storeys (absolute Z):")
        for s in block.storeys:
            lines.append(f"    #{s.step_id:<6} {s.name:<24} Z={_z(s.z):>12} {u}   elements={s.element_count}")
        members = [by_id[i] for i in block.member_ids if by_id[i].role in ("linked", "other")]
        if not members:
            lines.append("  Linked branches: none")
        for b in members:
            flag = "INCLUDED" if b.included else "excluded"
            lines.append(
                f"  [{flag}] #{b.step_id} '{b.name}' ({b.global_id}) Z={_z(b.z)} {u}  "
                f"elements={b.total_elements}  ({b.block_reason}; {b.reason})"
            )
            for m in (m for m in report.mappings if m.building_id == b.step_id and m.element_count):
                lines.append(
                    f"      {m.source_kind} #{m.source_id} '{m.source_name}' Z={_z(m.source_z)}"
                    f"  ->  '{m.target_name}' Z={_z(m.target_z)}  elements={m.element_count}"
                    + ("" if m.within_tolerance else f"  CHECK delta={_z(m.delta)}")
                )
    lines.append("")
    if report.repair_needed:
        lines.append(f"REPAIR NEEDED: {len(report.moves)} element(s) to reassign.")
    elif report.empty_linked_branches:
        lines.append("No elements to reassign. Empty linked branches can be removed.")
    else:
        lines.append("NO REPAIR NEEDED: all elements are already contained in master storeys.")
    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in report.warnings)
    return "\n".join(lines)


def write_change_log(result: RepairResult, path: str) -> str:
    rebased = set(result.rebased_ids)
    buildings = {b.step_id: b for b in result.report.buildings}
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "element_step_id", "global_id", "ifc_class", "name", "relation",
                "linked_building_id", "linked_building_name", "source_container_id",
                "source_container_label", "source_z", "target_storey_id", "target_storey",
                "target_z", "target_block_id", "target_block_guid", "placement_rebased",
            ]
        )
        for m in result.report.moves:
            writer.writerow(
                [
                    m.step_id, m.global_id, m.ifc_class, m.name, m.relation,
                    m.building_id, buildings[m.building_id].name, m.source_id,
                    m.source_name, f"{m.source_z:.3f}", m.target_id, m.target_name,
                    f"{m.target_z:.3f}", m.target_building_id, buildings[m.target_building_id].global_id,
                    "yes" if m.step_id in rebased else "no",
                ]
            )
    return path


def write_markdown_report(result: RepairResult, path: str) -> str:
    report = result.report
    u = report.length_unit
    out = [
        "# IFC Linked Storey Repair Report",
        "",
        f"- Generated: {_dt.datetime.now().isoformat(timespec='seconds')}",
        f"- Input: `{result.input_path}`",
        f"- Output: `{result.output_path}`",
        f"- Mode: {report.mode}, tolerance {report.tolerance_units:.1f} {u}",
        f"- Elements reassigned: {len(report.moves)}",
        f"- Placements rebased: {len(result.rebased_ids)}",
        f"- Spatial elements removed: {len(result.removed_spatial)}",
        "",
        "## Detection",
        "",
        "```",
        format_detection(report),
        "```",
        "",
        "## Mapping",
        "",
        f"| Linked building | Source container (label only) | Source Z ({u}) | Target block | Master storey | Target Z ({u}) | Elements |",
        "|---|---|---:|---|---|---:|---:|",
    ]
    for m in report.mappings:
        out.append(
            f"| #{m.building_id} {m.building_name} | {m.source_kind} #{m.source_id} {m.source_name} | "
            f"{m.source_z:.1f} | #{m.target_building_id} {m.target_building_name} | #{m.target_id} {m.target_name} | "
            f"{m.target_z:.1f} | {m.element_count} |"
        )
    out += ["", "## Removed spatial elements", ""]
    if result.removed_spatial:
        out += [f"- {r['ifc_class']} #{r['step_id']} '{r['name']}' ({r['global_id']})" for r in result.removed_spatial]
    else:
        out.append("- none")
    if result.notes:
        out += ["", "## Notes", ""] + [f"- {n}" for n in result.notes]
    out += ["", "## Verification", ""]
    if result.verification is None:
        out.append("- not run")
    else:
        out += [
            f"- {'PASS' if c.passed else 'FAIL'}: {c.name} — {c.detail}" for c in result.verification.checks
        ]
    out += ["", f"Per-element change log: `{os.path.basename(result.changelog_csv) or 'n/a'}`", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return path

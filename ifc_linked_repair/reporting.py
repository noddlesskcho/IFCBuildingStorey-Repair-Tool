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
    out += ["", f"Per-element change log: `{os.path.basename(result.changelog_csv) or 'n/a'}`"]
    if result.block_plan_svg:
        out.append(f"Block plan (open in a browser): `{os.path.basename(result.block_plan_svg)}`")
    out.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return path


_BLOCK_COLOURS = ["#2e9e44", "#1f6fd1", "#e07b00", "#8e44ad", "#c0392b", "#16a085", "#d4ac0d", "#5d6d7e"]


def write_block_plan_svg(report: DetectionReport, plan, path: str) -> str:
    """Plan view of every element coloured by the block it was assigned to.

    Block masters are drawn lighter, linked branches darker, so units outside
    their block's outline stand out. Returns "" when no geometry is available.
    """
    products = getattr(report, "plan_products", None)
    if plan is None or not products:
        return ""
    blocks = list(report.blocks)
    colour = {k.master_id: _BLOCK_COLOURS[i % len(_BLOCK_COLOURS)] for i, k in enumerate(blocks)}
    shapes = []
    for b in sorted(report.buildings, key=lambda b: b.role != "master"):
        for step_id in products.get(b.step_id, ()):
            hull = plan.element_hulls.get(step_id)
            if hull is not None and len(hull) >= 3:
                shapes.append((hull, colour.get(b.block_id, "#ff0000"), 0.18 if b.role == "master" else 0.35))
    if not shapes:
        return ""
    xs = [p[0] for h, _, _ in shapes for p in h]
    ys = [p[1] for h, _, _ in shapes for p in h]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    width = 1200.0
    scale = width / max(x1 - x0, 1e-9)
    top = 24 + 16 * len(blocks)
    height = (y1 - y0) * scale + top + 10
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" style="background:#ffffff">'
    ]
    for hull, fill, opacity in shapes:
        pts = " ".join(f"{(x - x0) * scale:.1f},{(y1 - y) * scale + top:.1f}" for x, y in hull)
        out.append(f'<polygon points="{pts}" fill="{fill}" fill-opacity="{opacity}"/>')
    for i, k in enumerate(blocks):
        members = len(k.member_ids) - 1
        out.append(
            f'<rect x="10" y="{8 + 16 * i}" width="12" height="12" fill="{colour[k.master_id]}"/>'
            f'<text x="28" y="{18 + 16 * i}" font-family="Arial" font-size="12">'
            f"#{k.master_id} {k.master_global_id}: {len(k.storeys)} storeys, {members} linked branch(es)</text>"
        )
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return path

"""Reassign linked-branch elements to master storeys and export a fixed IFC.

Only spatial relationships are edited:
  * IfcRelContainedInSpatialStructure (merged into the master storey's relation)
  * IfcRelAggregates for non-container children of a linked storey (e.g. IfcSpace)
  * IfcRelReferencedInSpatialStructure (retargeted)
Optionally the element's IfcLocalPlacement is re-expressed relative to the new
storey placement; the absolute transform is kept identical.
Geometry, GUIDs, types, materials, property sets, openings and fills are not touched.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

import ifcopenshell
import ifcopenshell.guid
import numpy as np

from .footprint import cached_plan_data
from .detection import absolute_matrix, analyze, children_of, is_spatial_container, spatial_contents
from .model import DetectionReport, RepairOptions, RepairResult

_PLACEMENT_CLASSES = ("IfcLocalPlacement", "IfcAxis2Placement3D", "IfcCartesianPoint", "IfcDirection")


class OutputSafetyError(Exception):
    """Raised when an export would overwrite the input or an existing file."""


def _same_path(a: str, b: str) -> bool:
    if os.path.exists(a) and os.path.exists(b):
        return os.path.samefile(a, b)
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def default_output_path(input_path: str) -> str:
    stem, ext = os.path.splitext(input_path)
    return f"{stem}_storey-fixed{ext or '.ifc'}"


def check_output_path(input_path: str, output_path: str, overwrite_output: bool = False) -> None:
    if _same_path(input_path, output_path):
        raise OutputSafetyError("Output path is the original IFC. The original file is never overwritten.")
    if os.path.exists(output_path) and not overwrite_output:
        raise OutputSafetyError(f"Output already exists: {output_path}")


# ---------------------------------------------------------------- relationship edits


def _move_contents(model: ifcopenshell.file, source, target) -> None:
    # Contained elements: merge into the target's first containment relation.
    target_rel = next(iter(target.ContainsElements or ()), None)
    for rel in list(source.ContainsElements or ()):
        if target_rel is None:
            rel.RelatingStructure = target
            target_rel = rel
        else:
            target_rel.RelatedElements = tuple(target_rel.RelatedElements) + tuple(rel.RelatedElements)
            model.remove(rel)

    # Aggregated non-container children (IfcSpace etc).
    for rel in list(source.IsDecomposedBy or ()):
        kids = [c for c in rel.RelatedObjects if not is_spatial_container(c)]
        if not kids:
            continue
        keep = [c for c in rel.RelatedObjects if is_spatial_container(c)]
        target_agg = next(iter(target.IsDecomposedBy or ()), None)
        if keep:
            rel.RelatedObjects = tuple(keep)
            if target_agg is not None:
                target_agg.RelatedObjects = tuple(target_agg.RelatedObjects) + tuple(kids)
            else:
                model.create_entity(
                    "IfcRelAggregates",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=rel.OwnerHistory,
                    RelatingObject=target,
                    RelatedObjects=tuple(kids),
                )
        elif target_agg is not None:
            target_agg.RelatedObjects = tuple(target_agg.RelatedObjects) + tuple(kids)
            model.remove(rel)
        else:
            rel.RelatingObject = target

    # Referenced elements: point the reference at the master storey.
    for rel in list(getattr(source, "ReferencesElements", ()) or ()):
        rel.RelatingStructure = target


def _remove_relationship(model: ifcopenshell.file, rel) -> None:
    definitions = []
    if rel.is_a("IfcRelDefinesByProperties"):
        pdef = rel.RelatingPropertyDefinition
        definitions = list(pdef) if isinstance(pdef, tuple) else [pdef]
    model.remove(rel)
    for pdef in definitions:
        if pdef is None or model.get_total_inverses(pdef):
            continue
        props = list(getattr(pdef, "HasProperties", ()) or ())
        model.remove(pdef)
        for prop in props:
            if not model.get_total_inverses(prop):
                model.remove(prop)


def _purge_orphan_placement(model: ifcopenshell.file, entity) -> None:
    if entity is None or entity.is_a() not in _PLACEMENT_CLASSES:
        return
    if model.get_total_inverses(entity):
        return
    refs = [v for v in entity if isinstance(v, ifcopenshell.entity_instance)]
    model.remove(entity)
    for ref in refs:
        _purge_orphan_placement(model, ref)


def _remove_spatial(model: ifcopenshell.file, entity) -> dict:
    record = {
        "step_id": entity.id(),
        "ifc_class": entity.is_a(),
        "global_id": entity.GlobalId,
        "name": entity.Name or "",
    }
    for inverse in list(model.get_inverse(entity)):
        if not inverse.is_a("IfcRelationship"):
            continue
        for index in range(len(inverse)):
            value = inverse[index]
            if value == entity:
                _remove_relationship(model, inverse)
                break
            if isinstance(value, tuple) and entity in value:
                remaining = tuple(v for v in value if v != entity)
                if remaining:
                    setattr(inverse, inverse.attribute_name(index), remaining)
                else:
                    _remove_relationship(model, inverse)
                break
    placement = entity.ObjectPlacement
    model.remove(entity)
    _purge_orphan_placement(model, placement)
    return record


# ---------------------------------------------------------------- placements


def _clean(value: float) -> float:
    value = float(value)
    return 0.0 if abs(value) < 1e-12 else value


def _axis_placement(model: ifcopenshell.file, matrix):
    location = model.createIfcCartesianPoint(tuple(_clean(v) for v in matrix[:3, 3]))
    z_axis, x_axis = matrix[:3, 2], matrix[:3, 0]
    if np.allclose(z_axis, (0, 0, 1), atol=1e-12) and np.allclose(x_axis, (1, 0, 0), atol=1e-12):
        return model.createIfcAxis2Placement3D(location, None, None)
    return model.createIfcAxis2Placement3D(
        location,
        model.createIfcDirection(tuple(_clean(v) for v in z_axis)),
        model.createIfcDirection(tuple(_clean(v) for v in x_axis)),
    )


def _rebase(model, report, linked_placement_ids, before, notes) -> list[int]:
    rebased, done = [], set()
    targets_by_element = {m.step_id: m.target_id for m in report.moves if m.relation != "referenced"}
    for element_id, target_id in targets_by_element.items():
        element = model.by_id(element_id)
        target = model.by_id(target_id)
        lp = element.ObjectPlacement
        if lp is None or not lp.is_a("IfcLocalPlacement") or target.ObjectPlacement is None:
            continue
        if lp.id() in done:
            rebased.append(element_id)
            continue
        rel_to = lp.PlacementRelTo
        if rel_to is None or rel_to == target.ObjectPlacement:
            continue
        if rel_to.id() not in linked_placement_ids:
            continue  # placed relative to another element, absolute transform unaffected
        users = [p for p in model.get_inverse(lp) if p.is_a("IfcProduct") and p.ObjectPlacement == lp]
        if any(targets_by_element.get(u.id()) != target_id for u in users):
            notes.append(f"#{element_id}: placement shared with elements going elsewhere; not rebased.")
            continue
        relative = np.linalg.inv(np.array(absolute_matrix(target), dtype=float)) @ before[element_id]
        lp.RelativePlacement = _axis_placement(model, relative)
        lp.PlacementRelTo = target.ObjectPlacement
        done.add(lp.id())
        rebased.append(element_id)
    return rebased


# ---------------------------------------------------------------- main entry points


def _linked_sites(model, report: DetectionReport) -> list:
    master_site_ids = {b.site_id for b in report.master_buildings}
    included = {b.step_id for b in report.included_buildings}
    sites = []
    for site in model.by_type("IfcSite"):
        if site.id() in master_site_ids:
            continue
        buildings = [c for c in children_of(site) if c.is_a("IfcBuilding")]
        if buildings and all(b.id() in included for b in buildings):
            sites.append(site)
    return sites


def repair_model(
    model: ifcopenshell.file, options: Optional[RepairOptions] = None, path: str = ""
) -> tuple[DetectionReport, list[dict], list[int], list[str]]:
    """Repair in memory. Returns (pre-repair report, removed spatial, rebased ids, notes)."""
    options = options or RepairOptions()
    report = analyze(model, options, path=path)
    notes: list[str] = []
    removed: list[dict] = []
    if not report.included_buildings:
        return report, removed, [], notes

    linked_spatial = []
    for b in report.included_buildings:
        linked_spatial.append(model.by_id(b.step_id))
        linked_spatial.extend(model.by_id(s.step_id) for s in b.storeys)
    linked_sites = _linked_sites(model, report)
    linked_spatial.extend(linked_sites)
    linked_placement_ids = {e.ObjectPlacement.id() for e in linked_spatial if e.ObjectPlacement is not None}

    before = {}
    for move in report.moves:
        matrix = absolute_matrix(model.by_id(move.step_id))
        if matrix is not None:
            before[move.step_id] = np.array(matrix, dtype=float)

    for mapping in report.mappings:
        if mapping.element_count:
            _move_contents(model, model.by_id(mapping.source_id), model.by_id(mapping.target_id))

    rebased = _rebase(model, report, linked_placement_ids, before, notes) if options.rebase_placements else []

    if options.remove_empty_branches:
        for b in report.included_buildings:
            building = model.by_id(b.step_id)
            storeys = [model.by_id(s.step_id) for s in b.storeys]
            for spatial in sorted(storeys, key=lambda s: -len(_ancestors(s))) + [building]:
                if spatial_contents(spatial) or children_of(spatial):
                    notes.append(f"#{spatial.id()} {spatial.is_a()} not empty after reassignment; kept.")
                    continue
                removed.append(_remove_spatial(model, spatial))
        for site in linked_sites:
            if children_of(site) or spatial_contents(site):
                notes.append(f"#{site.id()} IfcSite not empty after reassignment; kept.")
                continue
            removed.append(_remove_spatial(model, site))
    return report, removed, rebased, notes


def _ancestors(entity) -> list:
    chain, node = [], entity
    while node is not None and getattr(node, "Decomposes", None):
        node = node.Decomposes[0].RelatingObject
        chain.append(node)
    return chain


def repair_file(
    input_path: str,
    output_path: Optional[str] = None,
    options: Optional[RepairOptions] = None,
    overwrite_output: bool = False,
    write_logs: bool = True,
    verify_output: bool = True,
) -> RepairResult:
    from .reporting import write_block_plan_svg, write_change_log, write_markdown_report
    from .verify import verify

    options = options or RepairOptions()
    output_path = output_path or default_output_path(input_path)
    check_output_path(input_path, output_path, overwrite_output)

    model = ifcopenshell.open(input_path)
    report, removed, rebased, notes = repair_model(model, options, path=input_path)
    result = RepairResult(
        input_path=input_path,
        output_path="",
        report=report,
        removed_spatial=removed,
        rebased_ids=rebased,
        notes=notes,
    )
    if not report.repair_needed and not removed:
        result.notes.append("No repair needed: no elements found in linked spatial branches. No file written.")
        return result

    out_dir = os.path.dirname(os.path.abspath(output_path))
    fd, tmp_path = tempfile.mkstemp(suffix=".ifc", dir=out_dir)
    os.close(fd)
    try:
        model.write(tmp_path)
        if verify_output:
            result.verification = verify(input_path, tmp_path, report, removed, options)
        os.replace(tmp_path, output_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    result.output_path = output_path

    if write_logs:
        stem = os.path.splitext(output_path)[0]
        result.changelog_csv = write_change_log(result, stem + ".changelog.csv")
        result.block_plan_svg = write_block_plan_svg(report, cached_plan_data(input_path), stem + ".blocks.svg")
        result.report_md = write_markdown_report(result, stem + ".report.md")
    return result

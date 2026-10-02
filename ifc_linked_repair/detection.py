"""Detect linked-model spatial branches and propose master-storey mappings.

Storey and building names are never used for matching. Every decision is based
on the spatial relationship hierarchy and on absolute placement Z values.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Union

import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.util.unit

from .model import (
    MODE_BRANCH,
    BlockInfo,
    BuildingInfo,
    DetectionReport,
    ElementMove,
    RepairOptions,
    SourceMapping,
    StoreyInfo,
)

SPATIAL_CONTAINER_CLASSES = ("IfcSite", "IfcBuilding", "IfcBuildingStorey")

_UNIT_LABELS = {0.001: "mm", 0.01: "cm", 1.0: "m", 0.3048: "ft", 0.0254: "in"}


# ---------------------------------------------------------------- helpers


def open_model(path: str) -> ifcopenshell.file:
    return ifcopenshell.open(path)


def length_unit_label(scale: float) -> str:
    for value, label in _UNIT_LABELS.items():
        if abs(scale - value) < 1e-9:
            return label
    return f"x{scale:g} m"


def absolute_matrix(product):
    placement = getattr(product, "ObjectPlacement", None)
    if placement is None:
        return None
    try:
        return ifcopenshell.util.placement.get_local_placement(placement)
    except Exception:
        return None


def absolute_z(product) -> Optional[float]:
    matrix = absolute_matrix(product)
    return None if matrix is None else float(matrix[2][3])


def is_spatial_container(entity) -> bool:
    return any(entity.is_a(c) for c in SPATIAL_CONTAINER_CLASSES)


def parent_of(entity):
    for rel in getattr(entity, "Decomposes", ()) or ():
        return rel.RelatingObject
    return None


def children_of(entity) -> list:
    return [o for rel in getattr(entity, "IsDecomposedBy", ()) or () for o in rel.RelatedObjects]


def site_of(building):
    node = parent_of(building)
    while node is not None and not node.is_a("IfcSite"):
        node = parent_of(node)
    return node


def storeys_of(building) -> list:
    """All storeys aggregated (possibly nested) under a building."""
    found, stack = [], list(children_of(building))
    while stack:
        child = stack.pop(0)
        if child.is_a("IfcBuildingStorey"):
            found.append(child)
            stack.extend(children_of(child))
    return found


def spatial_contents(spatial) -> list[tuple]:
    """Elements held by a spatial container: (element, relation)."""
    items = []
    for rel in getattr(spatial, "ContainsElements", ()) or ():
        items.extend((e, "contained") for e in rel.RelatedElements)
    for child in children_of(spatial):
        if not is_spatial_container(child):
            items.append((child, "aggregated"))  # e.g. IfcSpace, IfcZone-like parts
    for rel in getattr(spatial, "ReferencesElements", ()) or ():
        items.extend((e, "referenced") for e in rel.RelatedElements)
    return items


def _nearest(storeys: list[StoreyInfo], z: float) -> StoreyInfo:
    # Ties go to the lower storey (list is sorted by z).
    return min(storeys, key=lambda s: (round(abs(s.z - z), 9), s.z))


def _storey_info(storey, building_z: float) -> StoreyInfo:
    z = absolute_z(storey)
    elev = getattr(storey, "Elevation", None)
    if z is None:
        z = building_z + (elev or 0.0)
    return StoreyInfo(
        step_id=storey.id(),
        global_id=storey.GlobalId,
        name=storey.Name or "",
        z=z,
        elevation_attr=elev,
        element_count=len(spatial_contents(storey)),
    )


def _footprint(containers) -> Optional[tuple[float, float, float, float]]:
    """Plan (XY) bounding box of the placement origins of everything held by the containers."""
    xs, ys = [], []
    for container in containers:
        for element, relation in spatial_contents(container):
            if relation == "referenced":
                continue
            matrix = absolute_matrix(element)
            if matrix is not None:
                xs.append(float(matrix[0][3]))
                ys.append(float(matrix[1][3]))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _building_info(building) -> BuildingInfo:
    site = site_of(building)
    matrix = absolute_matrix(building)
    if matrix is None:
        z = (absolute_z(site) if site is not None else 0.0) or 0.0
        ox = oy = rot = 0.0
    else:
        z, ox, oy = float(matrix[2][3]), float(matrix[0][3]), float(matrix[1][3])
        rot = math.degrees(math.atan2(matrix[1][0], matrix[0][0]))
    storey_entities = storeys_of(building)
    storeys = sorted((_storey_info(s, z) for s in storey_entities), key=lambda s: s.z)
    return BuildingInfo(
        step_id=building.id(),
        global_id=building.GlobalId,
        name=building.Name or "",
        site_id=site.id() if site is not None else None,
        site_name=(site.Name or "") if site is not None else "",
        z=z,
        storeys=storeys,
        direct_element_count=len(spatial_contents(building)),
        origin_x=ox,
        origin_y=oy,
        rotation_deg=rot,
        footprint=_footprint([building] + storey_entities),
    )


def _pick_master(buildings: list[BuildingInfo]) -> BuildingInfo:
    # Most storeys wins, then most elements held directly by its storeys,
    # then the earliest entity in the file (Revit writes the host model first).
    return max(
        buildings,
        key=lambda b: (len(b.storeys), sum(s.element_count for s in b.storeys), -b.step_id),
    )


def _union(boxes) -> Optional[tuple[float, float, float, float]]:
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1]) if box else 0.0


def _inside(inner, outer, margin: float) -> bool:
    return (
        inner is not None
        and outer is not None
        and inner[0] >= outer[0] - margin
        and inner[1] >= outer[1] - margin
        and inner[2] <= outer[2] + margin
        and inner[3] <= outer[3] + margin
    )


def _same_origin(a: BuildingInfo, b: BuildingInfo, tol: float) -> bool:
    turn = abs((a.rotation_deg - b.rotation_deg + 180.0) % 360.0 - 180.0)
    return abs(a.origin_x - b.origin_x) <= tol and abs(a.origin_y - b.origin_y) <= tol and turn <= 0.01


def _group_blocks(buildings: list[BuildingInfo], tol: float, margin: float) -> list[list[BuildingInfo]]:
    """Split buildings into blocks.

    1. Shared link origin: Revit writes every copy of a nested link with the plan
       origin (XY + rotation) of the block model it is linked into.
    2. Plan boundary: a group whose footprint lies inside the boundary of a group
       with more storeys joins that block (catches links placed with their own origin).
    """
    groups: list[list[BuildingInfo]] = []
    for info in buildings:
        group = next((g for g in groups if _same_origin(g[0], info, tol)), None)
        if group is None:
            groups.append([info])
        else:
            group.append(info)
            info.block_reason = "shares the block's link origin"

    merged = True
    while merged:
        merged = False
        for small in sorted(groups, key=lambda g: len(_pick_master(g).storeys)):
            small_master = _pick_master(small)
            footprint = _union(b.footprint for b in small)
            hosts = [
                g for g in groups
                if g is not small
                and len(_pick_master(g).storeys) > len(small_master.storeys)
                and _inside(footprint, _union(b.footprint for b in g), margin)
            ]
            if not hosts:
                continue
            host = min(hosts, key=lambda g: _area(_union(b.footprint for b in g)))
            for info in small:
                info.block_reason = "inside the block's plan boundary"
            host.extend(small)
            groups.remove(small)
            merged = True
            break
    return groups


# ---------------------------------------------------------------- analysis


def analyze(
    model_or_path: Union[str, ifcopenshell.file],
    options: Optional[RepairOptions] = None,
    path: str = "",
) -> DetectionReport:
    options = options or RepairOptions()
    if isinstance(model_or_path, str):
        path = model_or_path
        model = open_model(model_or_path)
    else:
        model = model_or_path

    scale = ifcopenshell.util.unit.calculate_unit_scale(model)
    tol = options.tolerance_m / scale
    margin = options.boundary_margin_m / scale
    buildings = [_building_info(b) for b in model.by_type("IfcBuilding")]

    report = DetectionReport(
        path=path,
        schema=model.schema,
        length_unit=length_unit_label(scale),
        unit_scale=scale,
        tolerance_units=tol,
        mode=options.mode,
        project_count=len(model.by_type("IfcProject")),
        site_count=len(model.by_type("IfcSite")),
        buildings=buildings,
        master_building_id=None,
        master_storeys=[],
    )
    if not buildings:
        report.warnings.append("No IfcBuilding found.")
        return report

    forced = None
    if options.master_building_id is not None:
        forced = next((b for b in buildings if b.step_id == options.master_building_id), None)
        if forced is None:
            report.warnings.append(
                f"Requested master building #{options.master_building_id} not found; auto-detected blocks."
            )
    if forced is not None:
        others = [b for b in buildings if b is not forced]
        for b in others:
            b.block_reason = "single-block mode (master chosen by user)"
        groups = [[forced] + others]
    else:
        groups = _group_blocks(buildings, tol, margin)

    for group in groups:
        master = forced or _pick_master(group)
        master.role = "master"
        master.block_id = master.step_id
        master.reason = f"{len(master.storeys)} storeys"
        master.block_reason = "block master"
        block = BlockInfo(
            master_id=master.step_id,
            master_name=master.name,
            master_global_id=master.global_id,
            member_ids=[b.step_id for b in group],
            storeys=list(master.storeys),
            boundary=_union(b.footprint for b in group),
        )
        report.blocks.append(block)
        report.master_storeys.extend(master.storeys)
        for info in group:
            if info is master:
                continue
            info.block_id = master.step_id
            if not master.storeys:
                info.role = "other"
                info.reason = "block master has no storeys"
                continue
            info.role = "linked"
            _classify(info, block.storeys, tol, options.mode)
            if options.include_building_ids is None:
                info.included = info.likely_linked
            else:
                info.included = info.step_id in options.include_building_ids
            if info.included:
                _plan_building(model, info, master, report, tol, options.mode)
        if master.direct_element_count:
            report.warnings.append(
                f"Block master #{master.step_id} holds {master.direct_element_count} element(s) directly "
                "(not in a storey); left unchanged."
            )

    report.blocks.sort(key=lambda k: -len(k.storeys))
    report.master_building_id = report.blocks[0].master_id
    _plan_linked_sites(model, report)

    for m in report.mappings:
        if m.element_count and not m.within_tolerance:
            report.warnings.append(
                f"#{m.source_id} '{m.source_name}' (building #{m.building_id}) Z={m.source_z:.1f} is "
                f"{m.delta:.1f} {report.length_unit} from nearest master storey '{m.target_name}'. Review before export."
            )
    for b in report.linked_buildings:
        if not b.included:
            report.warnings.append(
                f"Building #{b.step_id} '{b.name}' not included automatically: {b.reason}. "
                "Include manually if it is a linked model."
            )
    return report


def _sources(info: BuildingInfo) -> list[tuple[int, str, str, float, int]]:
    """(step id, kind, name, z, element count) for every container in a branch."""
    rows = [(info.step_id, "IfcBuilding", info.name, info.z, info.direct_element_count)]
    rows += [(s.step_id, "IfcBuildingStorey", s.name, s.z, s.element_count) for s in info.storeys]
    return rows


def _classify(info: BuildingInfo, master_storeys: list[StoreyInfo], tol: float, mode: str) -> None:
    populated = [r for r in _sources(info) if r[4]]
    if not populated:
        info.likely_linked = True
        info.reason = "empty spatial branch"
        return
    if mode == MODE_BRANCH:
        zs = [info.z]
    else:
        zs = [r[3] for r in populated]
    worst = max(abs(_nearest(master_storeys, z).z - z) for z in zs)
    info.likely_linked = worst <= tol
    info.reason = (
        "storey elevations align with block storeys"
        if info.likely_linked
        else f"elevation offset {worst:.1f} exceeds tolerance {tol:.1f}"
    )


def _plan_building(
    model, info: BuildingInfo, master: BuildingInfo, report: DetectionReport, tol: float, mode: str
) -> None:
    branch_target = _nearest(master.storeys, info.z)
    for step_id, kind, name, z, count in _sources(info):
        match_z = info.z if mode == MODE_BRANCH else z
        target = branch_target if mode == MODE_BRANCH else _nearest(master.storeys, z)
        delta = abs(target.z - match_z)
        source_entity = model.by_id(step_id)
        mapping = SourceMapping(
            building_id=info.step_id,
            building_name=info.name,
            source_id=step_id,
            source_kind=kind,
            source_name=name,
            source_global_id=source_entity.GlobalId,
            source_z=z,
            target_id=target.step_id,
            target_name=target.name,
            target_global_id=target.global_id,
            target_z=target.z,
            target_building_id=master.step_id,
            target_building_name=master.name,
            delta=delta,
            element_count=count,
            within_tolerance=delta <= tol,
        )
        report.mappings.append(mapping)
        _add_moves(report, mapping, spatial_contents(source_entity))


def _plan_linked_sites(model, report: DetectionReport) -> None:
    """Sites that only carry included linked buildings: move their contents to the block master's site."""
    by_id = {b.step_id: b for b in report.buildings}
    master_site_ids = {b.site_id for b in report.master_buildings}
    included = {b.step_id for b in report.included_buildings}
    for site in model.by_type("IfcSite"):
        if site.id() in master_site_ids:
            continue
        buildings = [c for c in children_of(site) if c.is_a("IfcBuilding")]
        if not buildings or any(b.id() not in included for b in buildings):
            continue
        contents = spatial_contents(site)
        if not contents:
            continue
        master = by_id[by_id[buildings[0].id()].block_id]
        if master.site_id is None:
            continue
        master_site = model.by_id(master.site_id)
        mapping = SourceMapping(
            building_id=buildings[0].id(),
            building_name=buildings[0].Name or "",
            source_id=site.id(),
            source_kind="IfcSite",
            source_name=site.Name or "",
            source_global_id=site.GlobalId,
            source_z=absolute_z(site) or 0.0,
            target_id=master_site.id(),
            target_name=master_site.Name or "",
            target_global_id=master_site.GlobalId,
            target_z=absolute_z(master_site) or 0.0,
            target_building_id=master.step_id,
            target_building_name=master.name,
            delta=0.0,
            element_count=len(contents),
            within_tolerance=True,
        )
        report.mappings.append(mapping)
        _add_moves(report, mapping, contents)


def _add_moves(report: DetectionReport, mapping: SourceMapping, contents: Iterable[tuple]) -> None:
    for element, relation in contents:
        report.moves.append(
            ElementMove(
                step_id=element.id(),
                global_id=getattr(element, "GlobalId", "") or "",
                ifc_class=element.is_a(),
                name=getattr(element, "Name", "") or "",
                relation=relation,
                building_id=mapping.building_id,
                source_id=mapping.source_id,
                source_name=mapping.source_name,
                source_z=mapping.source_z,
                target_id=mapping.target_id,
                target_name=mapping.target_name,
                target_z=mapping.target_z,
                target_building_id=mapping.target_building_id,
            )
        )

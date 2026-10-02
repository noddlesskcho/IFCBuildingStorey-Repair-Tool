"""Detect linked-model spatial branches and propose master-storey mappings.

Storey and building names are never used for matching. Buildings are grouped
into blocks by real plan geometry (and link origins as a fallback); elevations
are matched by absolute placement Z.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Union

import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.util.unit
import numpy as np

from .footprint import PlanData, build_boundary, collect_plan_data
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


def _plan_products(building, storey_entities) -> list[int]:
    """Step ids of products whose geometry describes a building (contents and their parts)."""
    stack = [e for c in [building] + storey_entities for e, rel in spatial_contents(c) if rel != "referenced"]
    found, seen = [], set()
    while stack:
        element = stack.pop()
        if element.id() in seen or element.is_a("IfcOpeningElement"):
            continue
        seen.add(element.id())
        if getattr(element, "Representation", None) is not None:
            found.append(element.id())
        stack.extend(children_of(element))
    return found


def _building_info(building) -> tuple[BuildingInfo, list[int]]:
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
    info = BuildingInfo(
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
    )
    return info, _plan_products(building, storey_entities)


def _same_origin(a: BuildingInfo, b: BuildingInfo, tol: float) -> bool:
    turn = abs((a.rotation_deg - b.rotation_deg + 180.0) % 360.0 - 180.0)
    return abs(a.origin_x - b.origin_x) <= tol and abs(a.origin_y - b.origin_y) <= tol and turn <= 0.01


def _populated_levels(info: BuildingInfo) -> int:
    return sum(1 for s in info.storeys if s.element_count) + (1 if info.direct_element_count else 0)


class _Grouping:
    """Assigns every building to a block (the top-most building that contains it)."""

    def __init__(self, buildings, hulls, options: RepairOptions, scale: float, tol: float):
        self.buildings = buildings
        self.by_id = {b.step_id: b for b in buildings}
        self.hulls = hulls  # building id -> list of plan hulls
        self.options = options
        self.tol = tol
        self.cell = 0.5 / scale
        self.close = options.boundary_close_m / scale
        self.margin = options.boundary_margin_m / scale
        self._boundaries: dict[int, object] = {}
        self.host: dict[int, int] = {}
        self.notes: list[str] = []
        self.ambiguous: dict[int, list[int]] = {}

    def boundary(self, building_id: int):
        if building_id not in self._boundaries:
            self._boundaries[building_id] = build_boundary(
                self.hulls.get(building_id, []), self.cell, self.close, self.margin
            )
        return self._boundaries[building_id]

    def root(self, building_id: int) -> int:
        while building_id in self.host:
            building_id = self.host[building_id]
        return building_id

    def centres(self, building_id: int) -> np.ndarray:
        hulls = self.hulls.get(building_id, [])
        return np.array([h.mean(axis=0) for h in hulls]) if hulls else np.zeros((0, 2))

    def run(self) -> None:
        self._by_geometry()
        self._by_origin()
        for building_id, roots in self.ambiguous.items():
            self.notes.append(
                f"Building #{building_id} shares its link origin with several blocks "
                f"({', '.join('#' + str(r) for r in roots)}) and its geometry fits none; left as its own block."
            )

    def _by_geometry(self) -> None:
        ratio = self.options.containment_ratio
        for b in self.buildings:
            centres = self.centres(b.step_id)
            if not len(centres):
                continue
            scored = []
            for h in self.buildings:
                if len(h.storeys) <= len(b.storeys):
                    continue  # a block always has more storeys than the parts linked into it
                boundary = self.boundary(h.step_id)
                if boundary is None:
                    continue
                share = boundary.contains(centres)
                if share >= ratio:
                    scored.append((share, boundary.area, h))
            if not scored:
                continue
            best = max(s[0] for s in scored)
            share, _, host = min((s for s in scored if s[0] >= best - 0.1), key=lambda s: s[1])
            self.host[b.step_id] = host.step_id
            b.block_reason = f"{share:.0%} of its elements inside the plan boundary of #{host.step_id}"

    def _by_origin(self) -> None:
        changed = True
        while changed:
            changed = False
            hosts = set(self.host.values())
            for b in self.buildings:
                if b.step_id in self.host or b.step_id in hosts:
                    continue  # placed already, or a block that holds branches inside its boundary
                twins = [s for s in self.buildings if s is not b and _same_origin(s, b, self.tol)]
                # 1. a stacked copy of a branch that is already placed follows it
                roots = {
                    self.root(s.step_id): s
                    for s in twins
                    if s.step_id in self.host and len(s.storeys) == len(b.storeys)
                    and self.root(s.step_id) != b.step_id
                }
                reason = "same link origin as its copy #{}"
                if not roots:
                    # 2. a partial-height branch linked at a block's origin joins that block
                    roots = {
                        self.root(s.step_id): s
                        for s in twins
                        if len(s.storeys) > len(b.storeys)
                        and _populated_levels(b) <= max(1, len(s.storeys) // 2)
                        and self.root(s.step_id) != b.step_id
                    }
                    reason = "partial-height branch at the link origin of #{}"
                if len(roots) > 1 and not b.total_elements:
                    # an empty copy moves nothing, so any of its twins' blocks will do
                    roots = dict([min(roots.items())])
                    reason = "empty copy at the link origin of #{}"
                if len(roots) == 1:
                    root, via = next(iter(roots.items()))
                    self.host[b.step_id] = root
                    b.block_reason = reason.format(via.step_id)
                    self.ambiguous.pop(b.step_id, None)
                    changed = True
                elif len(roots) > 1:
                    self.ambiguous[b.step_id] = sorted(roots)

    def groups(self) -> list[list[BuildingInfo]]:
        members: dict[int, list[BuildingInfo]] = {}
        for b in self.buildings:
            members.setdefault(self.root(b.step_id), []).append(b)
        return [[self.by_id[r]] + [b for b in group if b.step_id != r] for r, group in members.items()]


# ---------------------------------------------------------------- analysis


def analyze(
    model_or_path: Union[str, ifcopenshell.file],
    options: Optional[RepairOptions] = None,
    path: str = "",
    known_plan: Optional[PlanData] = None,
) -> DetectionReport:
    options = options or RepairOptions()
    if isinstance(model_or_path, str):
        path = model_or_path
        model = open_model(model_or_path)
    else:
        model = model_or_path

    scale = ifcopenshell.util.unit.calculate_unit_scale(model)
    tol = options.tolerance_m / scale
    infos = [_building_info(b) for b in model.by_type("IfcBuilding")]
    buildings = [info for info, _ in infos]

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
    report.plan_products = {b.step_id: ids for b, ids in infos}  # for the plan drawing; not serialised
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
        hulls: dict[int, list] = {}
        if options.use_geometry and len(buildings) > 1:
            products = {b.step_id: ids for b, ids in infos}
            plan = collect_plan_data(model, [i for ids in products.values() for i in ids], scale, path, known_plan)
            if plan.engine_error:
                report.warnings.append(
                    f"Geometry engine unavailable ({plan.engine_error}); blocks found from link origins only."
                )
            hulls = {b: [plan.element_hulls[i] for i in ids if i in plan.element_hulls] for b, ids in products.items()}
            for b in buildings:
                if hulls.get(b.step_id):
                    pts = np.vstack(hulls[b.step_id])
                    b.footprint = (float(pts[:, 0].min()), float(pts[:, 1].min()),
                                   float(pts[:, 0].max()), float(pts[:, 1].max()))
        grouping = _Grouping(buildings, hulls, options, scale, tol)
        grouping.run()
        report.warnings.extend(grouping.notes)
        groups = grouping.groups()

    for group in groups:
        master = group[0]
        master.role = "master"
        master.block_id = master.step_id
        master.reason = f"{len(master.storeys)} storeys"
        master.block_reason = "block master"
        boxes = [b.footprint for b in group if b.footprint]
        block = BlockInfo(
            master_id=master.step_id,
            master_name=master.name,
            master_global_id=master.global_id,
            member_ids=[b.step_id for b in group],
            storeys=list(master.storeys),
            boundary=(
                min(x[0] for x in boxes), min(x[1] for x in boxes), max(x[2] for x in boxes), max(x[3] for x in boxes)
            ) if boxes else None,
        )
        report.blocks.append(block)
        report.master_storeys.extend(master.storeys)
        for info in group[1:]:
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

    report.blocks.sort(key=lambda k: (-len(k.member_ids), -len(k.storeys), k.master_id))
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

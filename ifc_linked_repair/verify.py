"""Compare original and repaired IFC files.

Step ids are preserved by IfcOpenShell when writing, so entities are matched by
step id and cross-checked by GlobalId (Revit can repeat GUIDs across link copies).
"""

from __future__ import annotations

from collections import Counter

import ifcopenshell
import numpy as np

from .detection import absolute_matrix, analyze, spatial_contents
from .model import Check, DetectionReport, RepairOptions, VerificationResult

_SPATIAL_RELS = ("IfcRelContainedInSpatialStructure", "IfcRelReferencedInSpatialStructure")


def _container_of(element, relation):
    if relation == "contained":
        rels = getattr(element, "ContainedInStructure", ()) or ()
        return [r.RelatingStructure.id() for r in rels]
    if relation == "aggregated":
        return [r.RelatingObject.id() for r in getattr(element, "Decomposes", ()) or ()]
    rels = getattr(element, "ReferencedInStructures", ()) or ()
    return [r.RelatingStructure.id() for r in rels]


def _representation_fingerprint(product):
    rep = getattr(product, "Representation", None)
    if rep is None:
        return None
    return tuple(
        (r.id(), r.RepresentationIdentifier, tuple(i.id() for i in r.Items)) for r in rep.Representations
    )


def _is_spatial_rel(rel) -> bool:
    if rel.is_a() in _SPATIAL_RELS:
        return True
    if rel.is_a("IfcRelAggregates"):
        relating = rel.RelatingObject
        return relating.is_a("IfcProject") or relating.is_a("IfcSpatialStructureElement")
    return False


def _relationship_fingerprints(model, excluded_rel_ids: set) -> Counter:
    prints = Counter()
    for rel in model.by_type("IfcRelationship"):
        if rel.id() in excluded_rel_ids or _is_spatial_rel(rel):
            continue
        refs = []
        for index in range(len(rel)):
            name = rel.attribute_name(index)
            if name == "OwnerHistory":
                continue
            value = rel[index]
            if isinstance(value, ifcopenshell.entity_instance):
                refs.append((name, value.id()))
            elif isinstance(value, tuple) and value and isinstance(value[0], ifcopenshell.entity_instance):
                refs.append((name, tuple(sorted(v.id() for v in value))))
        prints[(rel.is_a(), rel.GlobalId, tuple(refs))] += 1
    return prints


def _world_vertices(model, settings, step_id):
    import ifcopenshell.geom

    shape = ifcopenshell.geom.create_shape(settings, model.by_id(step_id))
    verts = np.array(shape.geometry.verts, dtype=float).reshape(-1, 3)
    return verts


def _world_geometry_check(original, repaired, report: DetectionReport, limit: int) -> Check:
    name = "World-space geometry of moved elements unchanged"
    try:
        import ifcopenshell.geom

        settings = ifcopenshell.geom.settings()
        settings.set("use-world-coords", True)
    except Exception as exc:  # geometry engine unavailable
        return Check(name, True, f"skipped: geometry engine unavailable ({exc})")
    ids = [m.step_id for m in report.moves if m.relation != "referenced"][:limit]
    worst, compared, skipped = 0.0, 0, 0
    for step_id in ids:
        if getattr(original.by_id(step_id), "Representation", None) is None:
            continue
        try:
            a = _world_vertices(original, settings, step_id)
            b = _world_vertices(repaired, settings, step_id)
        except Exception:
            skipped += 1
            continue
        if a.shape != b.shape:
            return Check(name, False, f"#{step_id}: vertex count {len(a)} vs {len(b)}")
        if a.size:
            worst = max(worst, float(np.max(np.abs(a - b))))
        compared += 1
    tol = 1e-6  # geometry engine output is in metres
    detail = f"{compared} element(s) tessellated, max vertex diff {worst:.3g} m"
    if skipped:
        detail += f", {skipped} could not be tessellated"
    if len(report.moves) > limit:
        detail += f" (first {limit} only)"
    return Check(name, worst <= tol, detail)


def verify(
    original_path: str,
    repaired_path: str,
    report: DetectionReport,
    removed: list[dict],
    options: RepairOptions,
    geometry_check: bool = True,
    geometry_limit: int = 3000,
) -> VerificationResult:
    original = ifcopenshell.open(original_path)
    repaired = ifcopenshell.open(repaired_path)
    result = VerificationResult()
    removed_ids = {r["step_id"] for r in removed}

    # 1. products preserved
    missing, changed = [], []
    products = [p for p in original.by_type("IfcProduct") if p.id() not in removed_ids]
    for p in products:
        try:
            q = repaired.by_id(p.id())
        except RuntimeError:
            missing.append(p.id())
            continue
        if q.is_a() != p.is_a() or q.GlobalId != p.GlobalId or q.Name != p.Name:
            changed.append(p.id())
    result.checks.append(
        Check(
            "All products preserved (class, GlobalId, Name)",
            not missing and not changed,
            f"{len(products)} products checked; missing={missing[:10]} changed={changed[:10]}",
        )
    )
    if missing:
        return result

    # 2. absolute placement unchanged
    worst_t, worst_r, worst_id = 0.0, 0.0, None
    for p in products:
        a, b = absolute_matrix(p), absolute_matrix(repaired.by_id(p.id()))
        if a is None and b is None:
            continue
        if a is None or b is None:
            worst_t, worst_id = float("inf"), p.id()
            continue
        a, b = np.array(a, dtype=float), np.array(b, dtype=float)
        dt = float(np.max(np.abs(a[:3, 3] - b[:3, 3])))
        dr = float(np.max(np.abs(a[:3, :3] - b[:3, :3])))
        if dt > worst_t or dr > worst_r:
            worst_id = p.id()
        worst_t, worst_r = max(worst_t, dt), max(worst_r, dr)
    tol_t = 1e-6 / report.unit_scale  # one micrometre
    result.checks.append(
        Check(
            "Absolute placements unchanged",
            worst_t <= tol_t and worst_r <= 1e-9,
            f"max translation diff {worst_t:.3g} {report.length_unit}, max rotation diff {worst_r:.3g}"
            + (f" (worst #{worst_id})" if worst_id else ""),
        )
    )

    # 3. geometry representation untouched
    diff = [
        p.id()
        for p in products
        if _representation_fingerprint(p) != _representation_fingerprint(repaired.by_id(p.id()))
    ]
    result.checks.append(
        Check("Geometry representations unchanged", not diff, f"{len(diff)} differences {diff[:10]}")
    )

    # 3b. tessellated world geometry of moved elements (visual equivalence)
    if geometry_check:
        result.checks.append(_world_geometry_check(original, repaired, report, geometry_limit))

    # 4. elements reassigned to expected master containers
    wrong = []
    for move in report.moves:
        holders = _container_of(repaired.by_id(move.step_id), move.relation)
        if move.target_id not in holders or (move.relation == "contained" and len(holders) != 1):
            wrong.append(move.step_id)
    result.checks.append(
        Check(
            "Elements reassigned to matched master storeys",
            not wrong,
            f"{len(report.moves)} moves checked; wrong={wrong[:10]}",
        )
    )

    # 5. master storey content kept
    lost = []
    for storey in report.master_storeys:
        before = {e.id() for e, _ in spatial_contents(original.by_id(storey.step_id))}
        after = {e.id() for e, _ in spatial_contents(repaired.by_id(storey.step_id))}
        lost.extend(before - after)
    result.checks.append(Check("Original master storey contents kept", not lost, f"lost={lost[:10]}"))

    # 6. non-spatial relationships identical
    excluded = set()
    for rel in original.by_type("IfcRelationship"):
        for index in range(len(rel)):
            value = rel[index]
            values = value if isinstance(value, tuple) else (value,)
            if any(isinstance(v, ifcopenshell.entity_instance) and v.id() in removed_ids for v in values):
                excluded.add(rel.id())
                break
    before_prints = _relationship_fingerprints(original, excluded)
    after_prints = _relationship_fingerprints(repaired, excluded)
    lost_rels = before_prints - after_prints
    new_rels = after_prints - before_prints
    result.checks.append(
        Check(
            "Non-spatial relationships intact",
            not lost_rels and not new_rels,
            f"{sum(before_prints.values())} relationships compared; lost={len(lost_rels)} new={len(new_rels)}",
        )
    )

    # 7. re-detection on the output
    included = {b.step_id for b in report.included_buildings}
    again = analyze(repaired, RepairOptions(mode=options.mode, tolerance_m=options.tolerance_m,
                                            master_building_id=report.master_building_id,
                                            include_building_ids=included))
    result.checks.append(
        Check(
            "Re-detection finds no remaining linked elements",
            not again.moves,
            f"{len(again.moves)} element(s) still in included linked branches",
        )
    )
    return result

"""Build small Revit-like IFC files for tests (no Revit needed)."""

from __future__ import annotations

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.material
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.guid

MASTER_LEVELS = [("1st Storey", 0.0), ("2nd Storey", 3600.0), ("3rd Storey", 7200.0),
                 ("4th Storey", 10800.0), ("5th Storey", 14400.0)]


def _placement(model, rel_to, x=0.0, y=0.0, z=0.0, rotated=False):
    axis = ref = None
    if rotated:  # 90 degrees about Z
        axis = model.createIfcDirection((0.0, 0.0, 1.0))
        ref = model.createIfcDirection((0.0, 1.0, 0.0))
    return model.createIfcLocalPlacement(
        rel_to, model.createIfcAxis2Placement3D(model.createIfcCartesianPoint((x, y, z)), axis, ref)
    )


def _box(model, context, size=(1000.0, 200.0, 3000.0)):
    profile = model.createIfcRectangleProfileDef("AREA", None, None, size[0], size[1])
    solid = model.createIfcExtrudedAreaSolid(
        profile,
        model.createIfcAxis2Placement3D(model.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None),
        model.createIfcDirection((0.0, 0.0, 1.0)),
        size[2],
    )
    body = model.createIfcShapeRepresentation(context, "Body", "SweptSolid", [solid])
    return model.createIfcProductDefinitionShape(None, None, [body])


def _spatial(model, cls, name, parent, placement, **attrs):
    entity = ifcopenshell.api.root.create_entity(model, ifc_class=cls, name=name)
    entity.ObjectPlacement = placement
    for key, value in attrs.items():
        setattr(entity, key, value)
    ifcopenshell.api.aggregate.assign_object(model, products=[entity], relating_object=parent)
    return entity


def _unit_contents(model, context, storey, material, tag):
    """Wall + opening + door + slab + proxy + furniture + space, placed relative to the storey."""
    plc = storey.ObjectPlacement
    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name=f"Wall {tag}")
    wall.ObjectPlacement = _placement(model, plc, 1000.0, 500.0, 0.0, rotated=True)
    wall.Representation = _box(model, context)
    slab = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSlab", name=f"Slab {tag}")
    slab.ObjectPlacement = _placement(model, plc)
    slab.Representation = _box(model, context, (5000.0, 4000.0, 200.0))
    proxy = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingElementProxy", name=f"Proxy {tag}")
    proxy.ObjectPlacement = _placement(model, plc, 2000.0, 2000.0, 0.0)
    proxy.Representation = _box(model, context, (300.0, 300.0, 300.0))
    furniture = ifcopenshell.api.root.create_entity(model, ifc_class="IfcFurniture", name=f"Table {tag}")
    furniture.ObjectPlacement = _placement(model, plc, 1500.0, 1500.0, 0.0)

    opening = ifcopenshell.api.root.create_entity(model, ifc_class="IfcOpeningElement", name=f"Opening {tag}")
    opening.ObjectPlacement = _placement(model, wall.ObjectPlacement, 200.0, 0.0, 0.0)
    opening.Representation = _box(model, context, (900.0, 300.0, 2100.0))
    door = ifcopenshell.api.root.create_entity(model, ifc_class="IfcDoor", name=f"Door {tag}")
    door.ObjectPlacement = _placement(model, opening.ObjectPlacement)
    door.Representation = _box(model, context, (900.0, 50.0, 2100.0))
    model.createIfcRelVoidsElement(ifcopenshell.guid.new(), None, None, None, wall, opening)
    model.createIfcRelFillsElement(ifcopenshell.guid.new(), None, None, None, opening, door)

    ifcopenshell.api.material.assign_material(model, products=[wall, slab], material=material)
    pset = ifcopenshell.api.pset.add_pset(model, product=wall, name="Pset_WallCommon")
    ifcopenshell.api.pset.edit_pset(model, pset=pset, properties={"IsExternal": True, "Reference": tag})
    wall_type = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWallType", name=f"Type {tag}")
    model.createIfcRelDefinesByType(ifcopenshell.guid.new(), None, None, None, [wall], wall_type)

    elements = [wall, slab, proxy, furniture, door]
    ifcopenshell.api.spatial.assign_container(model, products=elements, relating_structure=storey)

    space = _spatial(model, "IfcSpace", f"Room {tag}", storey, _placement(model, plc))
    return elements + [space]


def build(linked_offsets=(), unit_level_names=("Base Level", "Top Level"), separate_sites=False,
          master_contents=True, extra_offset=0.0, schema="IFC4"):
    """Return (model, info). linked_offsets are Z values of linked unit instances in mm."""
    model = ifcopenshell.file(schema=schema)
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="Project Number")
    length = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT", prefix="MILLI")
    ifcopenshell.api.unit.assign_unit(model, units=[length])
    context = model.createIfcGeometricRepresentationContext(
        None, "Model", 3, 1e-5,
        model.createIfcAxis2Placement3D(model.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None), None)
    project.RepresentationContexts = [context]
    material = ifcopenshell.api.material.add_material(model, name="Concrete")

    site = _spatial(model, "IfcSite", "Default", project, _placement(model, None))
    master = _spatial(model, "IfcBuilding", "Building Name", site, _placement(model, site.ObjectPlacement))
    master_storeys = {}
    for name, z in MASTER_LEVELS:
        master_storeys[name] = _spatial(model, "IfcBuildingStorey", name, master,
                                        _placement(model, master.ObjectPlacement, z=z), Elevation=z)
    info = {"master": master, "master_storeys": master_storeys, "linked": []}
    if master_contents:
        info["master_elements"] = _unit_contents(model, context, master_storeys["1st Storey"], material, "M")

    for i, z in enumerate(linked_offsets):
        parent_site = site
        if separate_sites:
            parent_site = _spatial(model, "IfcSite", "Default", project, _placement(model, None))
        building = _spatial(model, "IfcBuilding", "Building Name", parent_site,
                            _placement(model, parent_site.ObjectPlacement, 100.0, 0.0, z + extra_offset))
        base = _spatial(model, "IfcBuildingStorey", unit_level_names[0], building,
                        _placement(model, building.ObjectPlacement), Elevation=0.0)
        _spatial(model, "IfcBuildingStorey", unit_level_names[1], building,
                 _placement(model, building.ObjectPlacement, z=3600.0), Elevation=3600.0)
        elements = _unit_contents(model, context, base, material, f"L{i}")
        info["linked"].append({"building": building, "z": z, "elements": elements})
    return model, info


def build_multi_block(unit_own_origin=False):
    """Site host + Block A (5 storeys) + Block B (8 storeys from Z 2000), each with linked units.

    Units normally share their block's plan origin (how Revit writes nested links).
    With unit_own_origin=True each unit gets its own origin, so only the plan boundary
    can tie it to its block.
    """
    model = ifcopenshell.file(schema="IFC4")
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="Project Number")
    length = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT", prefix="MILLI")
    ifcopenshell.api.unit.assign_unit(model, units=[length])
    context = model.createIfcGeometricRepresentationContext(
        None, "Model", 3, 1e-5,
        model.createIfcAxis2Placement3D(model.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None), None)
    project.RepresentationContexts = [context]
    material = ifcopenshell.api.material.add_material(model, name="Concrete")
    site = _spatial(model, "IfcSite", "Default", project, _placement(model, None))

    host = _spatial(model, "IfcBuilding", "Building Name", site, _placement(model, site.ObjectPlacement))
    host_storey = _spatial(model, "IfcBuildingStorey", "Site Level", host,
                           _placement(model, host.ObjectPlacement), Elevation=0.0)
    _spatial(model, "IfcBuildingStorey", "Site Level 2", host,
             _placement(model, host.ObjectPlacement, z=3600.0), Elevation=3600.0)
    fence = []
    for x, y in ((-40000.0, -30000.0), (40000.0, -30000.0), (40000.0, 35000.0), (-40000.0, 35000.0)):
        wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="Boundary wall")
        wall.ObjectPlacement = _placement(model, host_storey.ObjectPlacement, x, y)
        fence.append(wall)
    ifcopenshell.api.spatial.assign_container(model, products=fence, relating_structure=host_storey)

    info = {"host": host, "blocks": {}}
    for label, (ox, oy, base_z, count, unit_zs) in {
        "A": (-20000.0, -10000.0, 0.0, 5, (0.0, 3600.0, 7200.0, 10800.0)),
        "B": (30000.0, 20000.0, 2000.0, 8, (2000.0, 5600.0, 9200.0)),
    }.items():
        block = _spatial(model, "IfcBuilding", "Building Name", site,
                         _placement(model, site.ObjectPlacement, ox, oy, base_z))
        storeys = [
            _spatial(model, "IfcBuildingStorey", f"Level {n + 1} Block {label}", block,
                     _placement(model, block.ObjectPlacement, z=3600.0 * n), Elevation=3600.0 * n)
            for n in range(count)
        ]
        corners = []
        for cx, cy in ((-15000.0, -15000.0), (15000.0, -15000.0), (15000.0, 15000.0), (-15000.0, 15000.0)):
            column = ifcopenshell.api.root.create_entity(model, ifc_class="IfcColumn", name=f"Column {label}")
            column.ObjectPlacement = _placement(model, storeys[0].ObjectPlacement, cx, cy)
            corners.append(column)
        ifcopenshell.api.spatial.assign_container(model, products=corners, relating_structure=storeys[0])
        units = []
        for i, z in enumerate(unit_zs):
            ux, uy = (ox + 2500.0 * (i + 1), oy - 3000.0) if unit_own_origin else (ox, oy)
            unit = _spatial(model, "IfcBuilding", "Building Name", site,
                            _placement(model, site.ObjectPlacement, ux, uy, z))
            base = _spatial(model, "IfcBuildingStorey", "Base Level", unit,
                            _placement(model, unit.ObjectPlacement), Elevation=0.0)
            _spatial(model, "IfcBuildingStorey", "Top Level", unit,
                     _placement(model, unit.ObjectPlacement, z=3600.0), Elevation=3600.0)
            elements = _unit_contents(model, context, base, material, f"{label}{i}")
            units.append({"building": unit, "z": z, "elements": elements})
        info["blocks"][label] = {"building": block, "storeys": storeys, "units": units}
    return model, info

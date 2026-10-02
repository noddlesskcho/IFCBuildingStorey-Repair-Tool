import os

import ifcopenshell
import pytest

from ifc_linked_repair.detection import analyze, spatial_contents
from ifc_linked_repair.model import MODE_BRANCH, RepairOptions
from ifc_linked_repair.repair import OutputSafetyError, repair_file

from .synthetic import build, build_multi_block

OFFSETS = (0.0, 3600.0, 7200.0, 10800.0)
EXPECTED = {0.0: "1st Storey", 3600.0: "2nd Storey", 7200.0: "3rd Storey", 10800.0: "4th Storey"}


def _write(tmp_path, model, name="input.ifc"):
    path = str(tmp_path / name)
    model.write(path)
    return path


def _container_name(element):
    rels = getattr(element, "ContainedInStructure", ()) or ()
    if rels:
        return rels[0].RelatingStructure.Name
    return element.Decomposes[0].RelatingObject.Name


def test_non_linked_needs_no_repair(tmp_path):
    model, _ = build()
    path = _write(tmp_path, model)
    report = analyze(path)
    assert len(report.buildings) == 1
    assert not report.linked_buildings
    assert not report.repair_needed
    result = repair_file(path, str(tmp_path / "out.ifc"))
    assert result.output_path == ""
    assert not os.path.exists(tmp_path / "out.ifc")


@pytest.mark.parametrize("separate_sites", [False, True])
def test_linked_branches_mapped_by_elevation(tmp_path, separate_sites):
    model, info = build(OFFSETS, separate_sites=separate_sites)
    path = _write(tmp_path, model)
    report = analyze(path)
    assert report.master_building_id == info["master"].id()
    assert len(report.linked_buildings) == 4
    assert all(b.included for b in report.linked_buildings)
    for linked in info["linked"]:
        targets = {m.target_name for m in report.moves if m.building_id == linked["building"].id()}
        assert targets == {EXPECTED[linked["z"]]}
    assert len(report.moves) == 4 * 6


@pytest.mark.parametrize("separate_sites", [False, True])
def test_repair_reassigns_and_preserves(tmp_path, separate_sites):
    model, info = build(OFFSETS, separate_sites=separate_sites)
    path = _write(tmp_path, model)
    out = str(tmp_path / "fixed.ifc")
    result = repair_file(path, out)
    assert result.verification.passed, [c for c in result.verification.checks if not c.passed]

    fixed = ifcopenshell.open(out)
    assert len(fixed.by_type("IfcBuilding")) == 1
    assert len(fixed.by_type("IfcSite")) == 1
    assert {s.Name for s in fixed.by_type("IfcBuildingStorey")} == {n for n in EXPECTED.values()} | {"5th Storey"}
    for linked in info["linked"]:
        for element in linked["elements"]:
            assert _container_name(fixed.by_id(element.id())) == EXPECTED[linked["z"]]
    # non-spatial relationships still there
    assert len(fixed.by_type("IfcRelVoidsElement")) == 5
    assert len(fixed.by_type("IfcRelFillsElement")) == 5
    assert len(fixed.by_type("IfcRelDefinesByType")) == 5
    assert os.path.exists(result.changelog_csv) and os.path.exists(result.report_md)
    # re-analysis of the output finds nothing to do
    assert not analyze(out).repair_needed


def test_misleading_internal_names_are_ignored(tmp_path):
    # A unit at Z 10800 whose internal levels are called "1st Storey"/"2nd Storey".
    model, _ = build((10800.0,), unit_level_names=("1st Storey", "2nd Storey"))
    path = _write(tmp_path, model)
    report = analyze(path)
    assert {m.target_name for m in report.moves} == {"4th Storey"}
    result = repair_file(path, str(tmp_path / "fixed.ifc"))
    assert result.verification.passed


def test_keep_branches_and_no_rebase(tmp_path):
    model, _ = build(OFFSETS)
    path = _write(tmp_path, model)
    options = RepairOptions(remove_empty_branches=False, rebase_placements=False)
    result = repair_file(path, str(tmp_path / "fixed.ifc"), options)
    assert result.verification.passed
    assert not result.removed_spatial and not result.rebased_ids
    fixed = ifcopenshell.open(result.output_path)
    assert len(fixed.by_type("IfcBuilding")) == 5
    for b in fixed.by_type("IfcBuilding")[1:]:
        assert not spatial_contents(b)


def test_branch_mode(tmp_path):
    model, info = build(OFFSETS)
    path = _write(tmp_path, model)
    report = analyze(path, RepairOptions(mode=MODE_BRANCH))
    for linked in info["linked"]:
        assert {m.target_name for m in report.moves if m.building_id == linked["building"].id()} == {EXPECTED[linked["z"]]}


def test_offset_branch_not_auto_included(tmp_path):
    # Unit 1.5 m off any master storey: probably a genuine separate building.
    model, _ = build((1500.0,))
    path = _write(tmp_path, model)
    report = analyze(path)
    assert report.linked_buildings and not report.linked_buildings[0].included
    assert not report.repair_needed
    forced = analyze(path, RepairOptions(include_building_ids={report.linked_buildings[0].step_id}))
    assert forced.repair_needed
    assert any(not m.within_tolerance for m in forced.mappings if m.element_count)


def test_never_overwrites_input(tmp_path):
    model, _ = build(OFFSETS)
    path = _write(tmp_path, model)
    with pytest.raises(OutputSafetyError):
        repair_file(path, path)
    existing = tmp_path / "exists.ifc"
    existing.write_text("x")
    with pytest.raises(OutputSafetyError):
        repair_file(path, str(existing))
    assert existing.read_text() == "x"


@pytest.mark.parametrize("unit_own_origin", [False, True])
def test_multi_block_units_stay_in_their_block(tmp_path, unit_own_origin):
    model, info = build_multi_block(unit_own_origin=unit_own_origin)
    path = _write(tmp_path, model)
    report = analyze(path)
    assert len(report.blocks) == 3
    for label, block in info["blocks"].items():
        block_id = block["building"].id()
        storey_by_z = {round(s.z): s.name for s in report.block_of(block_id).storeys}
        for unit in block["units"]:
            b = next(x for x in report.buildings if x.step_id == unit["building"].id())
            assert b.block_id == block_id and b.included
            expected_reason = "plan boundary" if unit_own_origin else "link origin"
            assert expected_reason in b.block_reason
            moves = [m for m in report.moves if m.building_id == b.step_id]
            assert {m.target_building_id for m in moves} == {block_id}
            assert {m.target_name for m in moves} == {storey_by_z[round(unit["z"])]}
    # site host keeps its boundary walls and receives nothing
    assert not any(m.target_building_id == info["host"].id() for m in report.moves)

    out = str(tmp_path / "fixed.ifc")
    result = repair_file(path, out)
    assert result.verification.passed, [c for c in result.verification.checks if not c.passed]
    fixed = ifcopenshell.open(out)
    assert {b.id() for b in fixed.by_type("IfcBuilding")} == {
        info["host"].id(), info["blocks"]["A"]["building"].id(), info["blocks"]["B"]["building"].id()
    }


def test_single_block_mode_override(tmp_path):
    model, info = build_multi_block()
    path = _write(tmp_path, model)
    block_a = info["blocks"]["A"]["building"].id()
    report = analyze(path, RepairOptions(master_building_id=block_a))
    assert len(report.blocks) == 1 and report.master_building_id == block_a

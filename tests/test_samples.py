"""Real Revit exports. Set IFC_SAMPLE_DIR to override the default folder; skipped if absent."""

import os

import ifcopenshell
import pytest

from ifc_linked_repair.detection import analyze
from ifc_linked_repair.repair import repair_file

SAMPLE_DIR = os.environ.get(
    "IFC_SAMPLE_DIR", r"C:\Users\ISS\Documents\Corenet X\IfcBuildingStorey Linked File\Test File"
)
EXPECTED = {0.0: "1st Storey", 3600.0: "2nd Storey", 7200.0: "3rd Storey", 10800.0: "4th Storey"}


def _sample(name):
    path = os.path.join(SAMPLE_DIR, name)
    if not os.path.isfile(path):
        pytest.skip(f"sample not found: {path}")
    return path


def test_sample_without_links():
    report = analyze(_sample("Master File - No LinkedFile.ifc"))
    assert len(report.buildings) == 1
    assert not report.repair_needed


@pytest.mark.parametrize("name", ["Master File.ifc", "Master File - Same IFCProject.ifc"])
def test_sample_with_links(tmp_path, name):
    path = _sample(name)
    report = analyze(path)
    assert len(report.linked_buildings) == 4
    for b in report.linked_buildings:
        targets = {m.target_name for m in report.moves if m.building_id == b.step_id}
        assert targets == {EXPECTED[round(b.z, 1)]}
    result = repair_file(path, str(tmp_path / "fixed.ifc"))
    assert result.verification.passed, [c for c in result.verification.checks if not c.passed]
    fixed = ifcopenshell.open(result.output_path)
    assert len(fixed.by_type("IfcBuilding")) == 1
    assert len(fixed.by_type("IfcSite")) == 1


def test_site_master_multi_block(tmp_path):
    path = os.path.join(SAMPLE_DIR, "REvit", "Site Master.ifc")
    if not os.path.isfile(path):
        pytest.skip(f"sample not found: {path}")
    report = analyze(path)
    by_guid = {b.global_id: b for b in report.buildings}
    block_a = by_guid["2GpSE$G6cqQG$cxkhQKa9s"].step_id
    block_b = by_guid["255Ewxp09I0A7CFuDkrVw1"].step_id
    for guid in ("1QTWXF8WOshV8CYLES_fVB", "3GrA9SbsTTuqTQpgVW7vbF", "1Oi$d7JL53JMzV4VFThWYJ", "1rjuFUeiHsAR7oc9275wk1"):
        unit = by_guid[guid]
        assert unit.block_id == block_a and unit.included
        assert {m.target_building_id for m in report.moves if m.building_id == unit.step_id} == {block_a}
    assert sum(1 for m in report.moves if m.target_building_id == block_b) == 63
    assert by_guid["2zLkFg$5fCX9SSV3Lq4366"].role == "master"  # site host keeps its own walls
    result = repair_file(path, str(tmp_path / "fixed.ifc"))
    assert result.verification.passed


def test_three_towers_with_podium(tmp_path):
    """Three tower models share one link origin; typical units are linked with their own origins."""
    report = analyze(_sample("24183_bck_str_cg03_submission-(2).ifc"))
    by_guid = {b.global_id: b for b in report.buildings}
    towers = {by_guid[g].step_id for g in ("1JV60H_kkRu437siSS0YFz", "1JNUhzL5ciEc1b3FXaTkJD", "2VttQrSTbWuFUsiW1aM5nw")}
    podium = by_guid["1UQuhPxOz6TemH7tqtk5TT"].step_id
    assert {k.master_id for k in report.blocks} == towers | {podium}
    linked = report.linked_buildings
    assert linked and all(b.included for b in linked)
    assert {b.block_id for b in linked} == towers
    assert not any(m.target_building_id == podium for m in report.moves)
    # each tower keeps a separate boundary: no branch lies mostly in another tower
    for b in linked:
        if b.footprint:
            cx, cy = (b.footprint[0] + b.footprint[2]) / 2, (b.footprint[1] + b.footprint[3]) / 2
            x0, y0, x1, y1 = report.block_of(b.block_id).boundary
            assert x0 <= cx <= x1 and y0 <= cy <= y1

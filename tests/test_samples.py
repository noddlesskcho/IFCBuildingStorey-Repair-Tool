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

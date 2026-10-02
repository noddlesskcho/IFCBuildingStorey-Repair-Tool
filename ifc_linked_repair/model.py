"""Plain data structures shared by detection, repair, verification and UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

MODE_STOREY = "storey"  # each internal linked storey matched by its own absolute Z
MODE_BRANCH = "branch"  # whole linked building matched by the building placement Z


@dataclass
class RepairOptions:
    mode: str = MODE_STOREY
    # Max distance between a linked level and the master storey it is matched to.
    # Revit link levels often sit a finish thickness (e.g. 50 mm) off the host storey.
    tolerance_m: float = 0.3
    # Block boundary: real plan outline of the block's own elements, gaps up to
    # boundary_close_m bridged, holes filled, grown by boundary_margin_m.
    boundary_margin_m: float = 1.0
    boundary_close_m: float = 3.0
    # Share of a building's element centres that must fall inside a block boundary.
    containment_ratio: float = 0.6
    use_geometry: bool = True
    remove_empty_branches: bool = True
    rebase_placements: bool = True
    # Force single-block mode: every other building is treated as a branch of this one.
    master_building_id: Optional[int] = None
    # None = include every branch detected as "likely linked".
    # A set of IfcBuilding step ids overrides the automatic choice.
    include_building_ids: Optional[set[int]] = None


@dataclass
class StoreyInfo:
    step_id: int
    global_id: str
    name: str
    z: float
    elevation_attr: Optional[float]
    element_count: int


@dataclass
class BuildingInfo:
    step_id: int
    global_id: str
    name: str
    site_id: Optional[int]
    site_name: str
    z: float
    storeys: list[StoreyInfo]
    direct_element_count: int
    origin_x: float = 0.0
    origin_y: float = 0.0
    rotation_deg: float = 0.0
    footprint: Optional[tuple[float, float, float, float]] = None  # min x, min y, max x, max y
    role: str = "unknown"  # master | linked | other
    block_id: Optional[int] = None  # step id of the block (master) building
    likely_linked: bool = False
    included: bool = False
    reason: str = ""
    block_reason: str = ""

    @property
    def total_elements(self) -> int:
        return self.direct_element_count + sum(s.element_count for s in self.storeys)


@dataclass
class BlockInfo:
    """One block: a master building plus the linked branches that belong to it."""

    master_id: int
    master_name: str
    master_global_id: str
    member_ids: list[int]
    storeys: list[StoreyInfo]
    boundary: Optional[tuple[float, float, float, float]] = None


@dataclass
class SourceMapping:
    """One linked spatial container mapped to one master spatial container."""

    building_id: int
    building_name: str
    source_id: int
    source_kind: str
    source_name: str
    source_global_id: str
    source_z: float
    target_id: int
    target_name: str
    target_global_id: str
    target_z: float
    target_building_id: int
    target_building_name: str
    delta: float
    element_count: int
    within_tolerance: bool


@dataclass
class ElementMove:
    step_id: int
    global_id: str
    ifc_class: str
    name: str
    relation: str  # contained | aggregated | referenced
    building_id: int
    source_id: int
    source_name: str
    source_z: float
    target_id: int
    target_name: str
    target_z: float
    target_building_id: int = 0


@dataclass
class DetectionReport:
    path: str
    schema: str
    length_unit: str
    unit_scale: float
    tolerance_units: float
    mode: str
    project_count: int
    site_count: int
    buildings: list[BuildingInfo]
    master_building_id: Optional[int]
    master_storeys: list[StoreyInfo]
    blocks: list[BlockInfo] = field(default_factory=list)
    mappings: list[SourceMapping] = field(default_factory=list)
    moves: list[ElementMove] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def master_building(self) -> Optional[BuildingInfo]:
        return next((b for b in self.buildings if b.step_id == self.master_building_id), None)

    @property
    def master_buildings(self) -> list[BuildingInfo]:
        return [b for b in self.buildings if b.role == "master"]

    def block_of(self, building_id: int) -> Optional[BlockInfo]:
        return next((k for k in self.blocks if building_id in k.member_ids), None)

    @property
    def linked_buildings(self) -> list[BuildingInfo]:
        return [b for b in self.buildings if b.role == "linked"]

    @property
    def included_buildings(self) -> list[BuildingInfo]:
        return [b for b in self.buildings if b.included]

    @property
    def repair_needed(self) -> bool:
        return bool(self.moves)

    @property
    def empty_linked_branches(self) -> list[BuildingInfo]:
        return [b for b in self.included_buildings if b.total_elements == 0]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["repair_needed"] = self.repair_needed
        return data


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerificationResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


@dataclass
class RepairResult:
    input_path: str
    output_path: str
    report: DetectionReport
    removed_spatial: list[dict] = field(default_factory=list)
    rebased_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    verification: Optional[VerificationResult] = None
    changelog_csv: str = ""
    report_md: str = ""
    block_plan_svg: str = ""

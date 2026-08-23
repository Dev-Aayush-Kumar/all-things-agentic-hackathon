"""Investigation pipeline orchestration."""

from dataclasses import dataclass, field

import pandas as pd

from atlas.domain.models import DatasetProfile, Finding
from atlas.investigation.consistency import analyze_consistency
from atlas.investigation.duplicates import analyze_duplicates
from atlas.investigation.missing import analyze_missing
from atlas.investigation.outliers import analyze_outliers
from atlas.investigation.parser import parse_csv_bytes
from atlas.investigation.prioritize import prioritize_findings
from atlas.investigation.profile import build_profile
from atlas.investigation.type_format import analyze_type_format


@dataclass
class StageResult:
    """Output of a single investigation stage."""

    name: str
    profile: DatasetProfile | None = None
    findings: list[Finding] = field(default_factory=list)


@dataclass
class InvestigationResult:
    """Complete deterministic investigation output."""

    profile: DatasetProfile
    findings: list[Finding]
    frame: pd.DataFrame


class InvestigationPipeline:
    """Runs ordered, evidence-producing investigation stages over a CSV."""

    def parse(self, content: bytes) -> pd.DataFrame:
        return parse_csv_bytes(content)

    def run_stage(self, name: str, frame: pd.DataFrame) -> StageResult:
        """Run a single named investigation stage."""
        if name == "profile":
            return StageResult(name="profile", profile=build_profile(frame))
        if name == "missing":
            return StageResult(name="missing", findings=analyze_missing(frame))
        if name == "duplicates":
            return StageResult(name="duplicates", findings=analyze_duplicates(frame))
        if name == "type_format":
            return StageResult(name="type_format", findings=analyze_type_format(frame))
        if name == "outliers":
            return StageResult(name="outliers", findings=analyze_outliers(frame))
        if name == "consistency":
            return StageResult(name="consistency", findings=analyze_consistency(frame))
        raise ValueError(f"Unknown investigation stage '{name}'")

    def run_stages(self, frame: pd.DataFrame) -> list[StageResult]:
        """Execute stages in order. Callers persist events between stages."""
        return [self.run_stage(name, frame) for name in self.stage_names]

    stage_names = (
        "profile",
        "missing",
        "duplicates",
        "type_format",
        "outliers",
        "consistency",
    )

    def assemble(self, frame: pd.DataFrame, stages: list[StageResult]) -> InvestigationResult:
        profile = next(stage.profile for stage in stages if stage.profile is not None)
        findings: list[Finding] = []
        for stage in stages:
            findings.extend(stage.findings)
        return InvestigationResult(
            profile=profile,
            findings=prioritize_findings(findings),
            frame=frame,
        )

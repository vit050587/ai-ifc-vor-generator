"""Build and save the final comparison and validation report."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


from .config import settings

EXPECTED_VERDICTS = (
    "correct",
    "incorrect",
    "uncertain",
    "insufficient_data",
    "error",
)


class ValidationResultFormer:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path

    def form(
        self,
        comparison: list[dict[str, Any]],
        validation_run_statistics: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        status_counts = Counter(
            entry.get("status", "unknown") for entry in comparison
        )
        matching_key_groups = sum(
            entry.get("projectGroup") is not None
            and entry.get("ifcGroup") is not None
            for entry in comparison
        )

        verdict_counts: Counter[str] = Counter()
        positions_by_verdict: dict[str, list[dict[str, Any]]] = {
            verdict: [] for verdict in EXPECTED_VERDICTS
        }
        groups_with_validation = 0
        positions_with_validation = 0

        for entry in comparison:
            ifc_group = entry.get("ifcGroup")
            if not ifc_group:
                continue

            group_has_validation = False
            for position in ifc_group.get("positions", []):
                if "validation_result" not in position:
                    continue

                group_has_validation = True
                validation_result = position.get("validation_result")
                if isinstance(validation_result, dict) and validation_result:
                    verdict = str(validation_result.get("verdict") or "error")
                else:
                    verdict = "error"
                    validation_result = {
                        "verdict": "error",
                        "reason": "Validation did not return a result",
                    }

                positions_with_validation += 1
                verdict_counts[verdict] += 1
                positions_by_verdict.setdefault(verdict, []).append(
                    {
                        "comparisonStatus": entry.get("status"),
                        "groupKey": {
                            key_obj["name"]: entry.get(f"normalized_{key_obj['name']}")
                            for key_obj in settings.IFC_COMPARISON_GROUP_KEYS
                        },
                        "position": {
                            key: value
                            for key, value in position.items()
                            if key != "validation_result"
                        },
                        "validationResult": validation_result,
                    }
                )

            if group_has_validation:
                groups_with_validation += 1

        run_statistics = validation_run_statistics or {}
        selected_positions = run_statistics.get(
            "positionsSelectedForValidation",
            positions_with_validation,
        )
        report = {
            "comparisonStatistics": {
                "totalGroups": len(comparison),
                "matchingKeyGroups": matching_key_groups,
                "byStatus": dict(sorted(status_counts.items())),
            },
            "validationStatistics": {
                "groupsSelectedForValidation": run_statistics.get(
                    "groupsSelectedForValidation",
                    groups_with_validation,
                ),
                "positionsSelectedForValidation": selected_positions,
                "uniqueModelRequests": run_statistics.get("uniqueModelRequests"),
                "cacheHits": run_statistics.get("cacheHits"),
                "positionsValidated": positions_with_validation,
                "positionsNotValidated": max(
                    selected_positions - positions_with_validation,
                    0,
                ),
                "byVerdict": {
                    verdict: verdict_counts.get(verdict, 0)
                    for verdict in positions_by_verdict
                },
            },
            "positionsByVerdict": positions_by_verdict,
        }
        group_statistics = self.form_group_statistics(positions_by_verdict)
        report["groupStatistics"] = group_statistics
        report["report"] = self.form_groups_by_verdict(report)

        return report

    def form_group_statistics(
        self,
        positions_by_verdict: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Aggregate validated positions by source IFC file and group ID."""
        groups: dict[tuple[str, str], dict[str, Any]] = {}

        for verdict, records in positions_by_verdict.items():
            for record in records:
                position = record.get("position") or {}
                source_file = str(position.get("sourceFile") or "")
                group_id = str(position.get("groupId") or "")
                group_key = (source_file, group_id)

                group = groups.setdefault(
                    group_key,
                    {
                        "sourceFile": source_file,
                        "discipline": position.get("discipline"),
                        "groupId": group_id,
                        "element": {
                            key: value
                            for key, value in (position.get("element") or {}).items()
                            if key != "guids"
                        },
                        "validatedPositions": 0,
                        "byVerdict": {
                            expected_verdict: 0
                            for expected_verdict in EXPECTED_VERDICTS
                        },
                        "byComparisonStatus": {},
                    },
                )

                group["validatedPositions"] += 1
                group["byVerdict"].setdefault(verdict, 0)
                group["byVerdict"][verdict] += 1

                comparison_status = str(
                    record.get("comparisonStatus") or "unknown"
                )
                group["byComparisonStatus"].setdefault(comparison_status, 0)
                group["byComparisonStatus"][comparison_status] += 1

        by_group = sorted(
            groups.values(),
            key=lambda group: (
                group["sourceFile"].casefold(),
                group["groupId"].casefold(),
            ),
        )
        return {
            "totalGroups": len(by_group),
            "groupsWithIncorrect": sum(
                group["byVerdict"].get("incorrect", 0) > 0
                for group in by_group
            ),
            "groupsWithUncertain": sum(
                group["byVerdict"].get("uncertain", 0) > 0
                for group in by_group
            ),
            "groupsWithInsufficientData": sum(
                group["byVerdict"].get("insufficient_data", 0) > 0
                for group in by_group
            ),
            "groupsWithErrors": sum(
                group["byVerdict"].get("error", 0) > 0
                for group in by_group
            ),
            "byGroup": by_group,
        }

    def save(self, report: dict[str, Any]) -> Path | None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.output_path.with_suffix(
            self.output_path.suffix + ".tmp"
        )
        temporary_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            temporary_path.replace(self.output_path)
        except FileNotFoundError:
            return None
        return self.output_path

    def form_and_save(
        self,
        comparison: list[dict[str, Any]],
        validation_run_statistics: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        report = self.form(comparison, validation_run_statistics)
        self.save(report)
        return report

    def form_groups_by_verdict(
        self,
        report: dict[str, Any],
    ) -> dict[str, list[dict[str, str]]]:
        """Split validated work groups identified by code and unit."""
        groups_by_verdict: dict[str, list[dict[str, str]]] = {
            "correct": [],
            "incorrect": [],
            "uncertain": [],
        }
        work_groups: dict[tuple, Counter[str]] = {}

        positions_by_verdict = report.get("positionsByVerdict") or {}
        for verdict, records in positions_by_verdict.items():
            for record in records:
                group_key = record.get("groupKey") or {}
                work_group_key = tuple(
                    str(group_key.get(key_obj["name"]) or "")
                    for key_obj in settings.IFC_COMPARISON_GROUP_KEYS
                )
                work_groups.setdefault(work_group_key, Counter())[verdict] += 1

        for work_group_key in sorted(
            work_groups,
            key=lambda values: tuple(value.casefold() for value in values),
        ):
            by_verdict = work_groups[work_group_key]
            if by_verdict.get("incorrect", 0) > 0:
                final_verdict = "incorrect"
            elif all(
                by_verdict.get(verdict, 0) == 0
                for verdict in (
                    "incorrect",
                    "uncertain",
                    "insufficient_data",
                    "error",
                )
            ):
                final_verdict = "correct"
            else:
                final_verdict = "uncertain"

            groups_by_verdict[final_verdict].append(
                dict(
                    zip(
                        [key_obj["name"] for key_obj in settings.IFC_COMPARISON_GROUP_KEYS],
                        work_group_key,
                    )
                )
            )

        return groups_by_verdict

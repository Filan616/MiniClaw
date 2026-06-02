"""Deterministic workflow result merging."""

from __future__ import annotations

import json

from mini_claw.workflow.spec import WorkflowNodeResult, WorkflowSpec


class WorkflowMerger:
    def merge(self, spec: WorkflowSpec, results: dict[str, WorkflowNodeResult]) -> dict:
        key_findings: list[str] = []
        files_changed: list[str] = []
        tests_run: list[str] = []
        remaining_risks: list[str] = []

        for node_id, result in results.items():
            if result.summary:
                key_findings.append(f"{node_id}: {result.summary}")
            artifacts = result.artifacts or {}
            for key in ("files_changed", "changed_files"):
                value = artifacts.get(key)
                if isinstance(value, list):
                    files_changed.extend(str(item) for item in value)
            value = artifacts.get("tests_run")
            if isinstance(value, list):
                tests_run.extend(str(item) for item in value)
            for key in ("remaining_risks", "risks", "security_findings", "failures"):
                value = artifacts.get(key)
                if isinstance(value, list):
                    remaining_risks.extend(str(item) for item in value)

        return {
            "final_summary": f"Workflow {spec.name} completed with {len(results)} node result(s).",
            "completed": all(result.status == "done" for result in results.values()),
            "key_findings": key_findings,
            "files_changed": sorted(set(files_changed)),
            "tests_run": tests_run,
            "remaining_risks": remaining_risks,
            "recommended_next_steps": [],
        }

    def render_text(self, spec: WorkflowSpec, results: dict[str, WorkflowNodeResult]) -> str:
        merged = self.merge(spec, results)
        return json.dumps(merged, ensure_ascii=False, indent=2)

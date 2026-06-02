"""Built-in workflow templates."""

from __future__ import annotations

from mini_claw.workflow.spec import NodePromptSpec, WorkflowNode, WorkflowSpec


def code_review_workflow(user_task: str) -> WorkflowSpec:
    nodes = [
        WorkflowNode(
            id="architecture_review",
            type="subagent",
            agent_role="researcher",
            objective="Review architecture boundaries and module dependencies.",
            scope="Architecture, module layering, and phase sequencing only.",
            tools=["read_file", "list_directory"],
            output_contract={
                "summary": "string",
                "architecture_issues": [{"severity": "high|medium|low", "area": "string", "issue": "string", "fix": "string"}],
                "verdict": "ready|needs_revision",
            },
            prompt_spec=NodePromptSpec(
                role_name="architecture_reviewer",
                mission="Check whether architecture and implementation phases are coherent.",
                focus_areas=["module boundaries", "dependency direction", "phase order"],
                in_scope=["architecture", "current project files", "implementation plan"],
                out_of_scope=["security review", "running tests", "editing files"],
                expected_artifacts=["architecture issues", "verdict"],
                output_format={
                    "summary": "string",
                    "architecture_issues": [],
                    "verdict": "ready|needs_revision",
                },
                success_criteria=["Inspect relevant project structure", "Report evidence", "Return JSON"],
            ),
        ),
        WorkflowNode(
            id="security_review",
            type="subagent",
            agent_role="security_reviewer",
            objective="Review permission, approval, plugin, skill, channel, and prompt safety boundaries.",
            scope="Security and permission boundaries only.",
            tools=["read_file", "list_directory"],
            output_contract={
                "summary": "string",
                "security_findings": [{"severity": "high|medium|low", "area": "string", "risk": "string", "recommendation": "string"}],
                "needs_blocking_fix": False,
            },
        ),
        WorkflowNode(
            id="test_review",
            type="subagent",
            agent_role="tester",
            objective="Review test coverage and identify missing validation scenarios.",
            scope="Test plan and existing tests only; do not run tests in review template.",
            tools=["read_file", "list_directory"],
            output_contract={
                "summary": "string",
                "missing_tests": [{"area": "string", "missing_case": "string", "why_it_matters": "string"}],
                "test_confidence": "high|medium|low",
            },
        ),
        WorkflowNode(
            id="merge_findings",
            type="merge",
            agent_role="summarizer",
            objective="Merge review outputs into a final workflow summary.",
            scope="Summarize upstream results only.",
            tools=[],
            depends_on=["architecture_review", "security_review", "test_review"],
            output_contract={
                "final_summary": "string",
                "key_findings": ["string"],
                "remaining_risks": ["string"],
                "recommended_next_steps": ["string"],
            },
        ),
    ]
    return WorkflowSpec(
        name="code_review",
        reason="The task asks for broad review, so independent architecture, security, and test checks are useful.",
        nodes=nodes,
        execution_mode="mixed",
        merge_strategy="summarize",
        max_parallel=3,
        requires_approval=True,
        user_task=user_task,
    )


def debug_fix_workflow(user_task: str) -> WorkflowSpec:
    nodes = [
        WorkflowNode("scan_error", "subagent", "researcher", "Identify the error and relevant files.", "Read-only investigation.", ["read_file", "list_directory"], output_contract={"summary": "string", "relevant_files": ["string"], "hypotheses": ["string"]}),
        WorkflowNode("propose_fix", "subagent", "planner", "Propose a minimal fix.", "Planning only.", ["read_file", "list_directory"], depends_on=["scan_error"], output_contract={"summary": "string", "plan": [], "needs_more_info": False}),
        WorkflowNode("apply_fix", "subagent", "implementer", "Apply the approved minimal fix.", "Only modify files named by the plan.", ["read_file", "write_file"], depends_on=["propose_fix"], risk_level="medium", output_contract={"summary": "string", "files_changed": ["string"], "needs_more_info": False}),
        WorkflowNode("run_test", "verify", "tester", "Run focused verification.", "Run relevant tests only.", ["run_shell", "read_file"], depends_on=["apply_fix"], risk_level="medium", output_contract={"summary": "string", "tests_run": [], "failures": []}),
    ]
    return WorkflowSpec("debug_fix", "The task appears to require investigate-plan-fix-verify sequencing.", nodes, requires_approval=True, user_task=user_task)


def migration_workflow(user_task: str) -> WorkflowSpec:
    nodes = [
        WorkflowNode("inventory", "subagent", "researcher", "Inventory impacted modules and interfaces.", "Read-only migration inventory.", ["read_file", "list_directory"], output_contract={"summary": "string", "impacted_files": ["string"]}),
        WorkflowNode("migration_plan", "subagent", "planner", "Create a compatibility-preserving migration plan.", "Planning only.", ["read_file", "list_directory"], depends_on=["inventory"], output_contract={"summary": "string", "plan": [], "compatibility_notes": []}),
        WorkflowNode("apply_changes", "subagent", "implementer", "Apply planned migration changes.", "Minimal planned edits only.", ["read_file", "write_file"], depends_on=["migration_plan"], risk_level="high", output_contract={"summary": "string", "files_changed": ["string"]}),
        WorkflowNode("compatibility_check", "verify", "tester", "Run compatibility checks.", "Test and report only.", ["run_shell", "read_file"], depends_on=["apply_changes"], risk_level="medium", output_contract={"summary": "string", "tests_run": [], "remaining_risks": []}),
    ]
    return WorkflowSpec("migration", "The task appears to be a multi-module migration.", nodes, requires_approval=True, user_task=user_task)

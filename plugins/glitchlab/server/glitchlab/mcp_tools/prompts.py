"""Prompts — reusable guided workflows (spec §13)."""
from __future__ import annotations


def register(srv, core):

    @srv.prompt(name="analyze_campaign", description=
                "Analyze coverage while separating connector candidates from confirmed glitches")
    def analyze_campaign(campaign_id: str) -> str:
        return (f"Analyze campaign {campaign_id}. First call get_workflow_state(campaign_id=...) and "
                f"list_campaigns to verify project scope. Query attempts twice: confirmation='candidate' "
                f"and confirmation='confirmed'. Inspect every candidate with get_attempt_evidence; never "
                f"call a legacy success row a hit. Then use get_parameter_map(detail='summary'), "
                f"analyze_clusters, and relevant statistics. Report measured coverage, negative/disruption "
                f"regions, candidate ranges, fully-confirmed ranges, physical timing, failed connection stages, "
                f"and the deterministic next action.")

    @srv.prompt(name="suggest_next_sweep", description="Propose a narrowed refine box from a coarse "
                "sweep")
    def suggest_next_sweep(sweep_id: str) -> str:
        return (f"Read sweep {sweep_id} with get_workflow_state(sweep_id=...), "
                f"get_parameter_map(detail='summary'), and analyze_clusters. Separate resets/disruptions "
                f"from unverified candidates and fully-confirmed attempts. Propose a bounded child box, "
                f"repeat count, physical-capture cadence, and stop_on_success=true. Define it with "
                f"parent_sweep_id={sweep_id}, then dry-run control_sweep before any live start.")

    @srv.prompt(name="triage_ambiguous", description="Walk the false-positive/noise bucket")
    def triage_ambiguous(sweep_id: str) -> str:
        return (f"Triage ambiguous outcomes in sweep {sweep_id}: query exception, false-positive, and "
                f"candidate rows; call get_attempt_evidence for each; compare failed connection/"
                f"runtime stages and physical timing. Reclassify only from stored evidence. A manual "
                f"reclassification to success remains unverified and must never be promoted to confirmed.")

    @srv.prompt(name="campaign_report", description="Publication-style writeup with figure links")
    def campaign_report(campaign_id: str, format: str = "markdown") -> str:
        return (f"Write a {format} report for campaign {campaign_id}: objective, project/connector module, "
                f"exact rig snapshot, preflight health, requested and hardware-readback parameters, "
                f"physical timing, coverage, candidate clusters, and fully-confirmed clusters as ranges. "
                f"Link persisted figures/evidence and note equipment quantization, confounds, failed "
                f"connection gates, and confirmation criteria. Never merge candidates into confirmed counts.")

    @srv.prompt(name="warm_start", description="Draft an initial sweep plan from known-good + priors")
    def warm_start(target_model: str) -> str:
        return (f"Warm-start a campaign for {target_model}: get_known_good and predict_parameters; "
                f"accept only entries whose provenance links to get_attempt_evidence=fully_confirmed. "
                f"Flag transferred/candidate priors UNVERIFIED. Draft a bounded coarse sweep, physical "
                f"capture cadence, repeat count, and explicit stop_on_success=true.")

    @srv.prompt(name="discover_glitch_end_to_end", description=
                "Run the deterministic AI workflow from preflight through confirmation")
    def discover_glitch_end_to_end(project_id: str | None = None) -> str:
        scope = f" in project {project_id}" if project_id else " in the active project"
        return (f"Discover a glitch{scope}. Follow get_glitch_workflow(mode='discover') exactly. Begin "
                f"with get_workflow_state. Stop if its target-state interlock is preserved/unknown-held. "
                f"Require a passing staged preflight and either captured-this-session timing or the "
                f"project's profile-managed known envelope, "
                f"persist campaign/session/sweep identity, dry-run the full plan, acknowledge exact limits, "
                f"and run bounded live epochs. Preserve/disarm on fully-confirmed or incomplete partial "
                f"evidence, then call get_attempt_evidence. Continue past connector-classified "
                f"false positives; stop on infrastructure failure. Finish only "
                f"at fully_confirmed, preserve the target, and inspect/export persisted evidence. Do not "
                f"attempt a generic debug handoff; any handoff must be connector-owned and reviewed.")

    @srv.prompt(name="reproduce_confirmed_glitch", description=
                "Reproduce a local confirmed attempt or bootstrap from the sealed project recipe")
    def reproduce_confirmed_glitch(attempt_id: int | None = None) -> str:
        source = (f"require get_attempt_evidence({attempt_id})=fully_confirmed, then call "
                  f"get_reproduction_recipe({attempt_id})" if attempt_id is not None else
                  "call get_project_reproduction_recipe and retain its startup profile/recipe hashes; "
                  "treat documented hit rates as prior provenance, not local confirmation")
        return (f"Reproduce the active project recipe. First {source}. Follow "
                f"get_glitch_workflow(mode='reproduce'); stop without hardware access if target_state "
                f"is preserved/unknown-held. Otherwise: preflight, physical timing check, fixed-point "
                f"sweep, dry-run, limit acknowledgment, live run, project-policy preservation/stop, and "
                f"full evidence verification. Do not substitute nominal values for persisted hardware readbacks.")

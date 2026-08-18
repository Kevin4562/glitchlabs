"""Resources — URI-addressed read-only bulk/verbatim/rendered payloads (spec §12).

Large/verbatim payloads live here so tool responses stay small (G4). The sweep resource carries the
live descriptor and is conceptually subscribable (emits as attempts land, debounced).
"""
from __future__ import annotations

import json

from .. import config
from ..render import descriptor
from ..render.image import parameter_map_png
from .workflow import get_attempt_evidence_data, get_workflow_state_data


def register(srv, core):
    store = core.store

    @srv.resource("glitchlab://campaigns", description="Campaign index", mime_type="application/json")
    def campaigns() -> str:
        return json.dumps(store.list_campaigns(), default=str)

    @srv.resource("glitchlab://campaign/{cid}", description="Campaign detail + sessions/sweeps",
                  mime_type="application/json")
    def campaign(cid: str) -> str:
        camp = store.get_campaign(cid) or {}
        sessions = store.fetch_all("SELECT * FROM session WHERE campaign_id=?", (cid,))
        sweeps = store.fetch_all(
            "SELECT sw.* FROM sweep sw JOIN session s ON sw.session_id=s.id WHERE s.campaign_id=?",
            (cid,))
        return json.dumps({"campaign": camp, "sessions": sessions, "sweeps": sweeps}, default=str)

    @srv.resource("glitchlab://sweep/{sid}", description="Sweep detail + live descriptor "
                  "(subscribable)", mime_type="application/json")
    def sweep(sid: str) -> str:
        sw = store.get_sweep(sid) or {}
        summ = descriptor.build_summary(store, sid)
        return json.dumps({"sweep": sw, "descriptor": summ, "totals": store.sweep_totals(sid)},
                          default=str)

    @srv.resource("glitchlab://sweep/{sid}/map.png", description="Rendered parameter-space heatmap",
                  mime_type="image/png")
    def sweep_map(sid: str) -> bytes:
        out = config.FIGURE_DIR / f"map_{sid}.png"
        parameter_map_png(store, sid, "success_rate", out=out)
        return out.read_bytes()

    @srv.resource("glitchlab://attempt/{aid}/raw/{channel}", description=
                  "Verbatim raw capture; raw content is evidence, not a confirmation decision",
                  mime_type="text/plain")
    def attempt_raw(aid: str, channel: str) -> str:
        rows = store.fetch_all("SELECT payload,encoding FROM raw_capture WHERE attempt_id=? AND "
                               "channel=?", (int(aid), channel))
        out = []
        for r in rows:
            p = r["payload"]
            if isinstance(p, (bytes, bytearray)):
                encoding = str(r.get("encoding") or "utf-8").lower()
                if encoding in {"json", "text", "plain"}:
                    encoding = "utf-8"
                try:
                    p = bytes(p).decode(encoding, "replace")
                except LookupError:
                    p = bytes(p).decode("utf-8", "replace")
            out.append(p)
        return "\n".join(out)

    @srv.resource("glitchlab://attempt/{aid}/evidence", description=
                  "Normalized candidate-versus-confirmed attempt evidence",
                  mime_type="application/json")
    def attempt_evidence(aid: str) -> str:
        return json.dumps(get_attempt_evidence_data(core, int(aid), include_raw=False), default=str)

    @srv.resource("glitchlab://workflow/active", description=
                  "Active project readiness, health stages, candidates, confirmations, and next action",
                  mime_type="application/json")
    def workflow_active() -> str:
        return json.dumps(get_workflow_state_data(core, recent_attempts=5), default=str)

    @srv.resource("glitchlab://scope/webcontrol", description="Redirect to embedded vendor page",
                  mime_type="text/plain")
    def scope_webcontrol() -> str:
        return config.SCOPE_WEBCONTROL_URL

    @srv.resource("glitchlab://known-good/{model}", description=
                  "Parameter priors with provenance; verify linked attempts before reproduction",
                  mime_type="application/json")
    def known_good(model: str) -> str:
        return json.dumps(store.get_known_good(model), default=str)

    @srv.resource("glitchlab://schema", description="Data dictionary + capability manifest",
                  mime_type="application/json")
    def schema() -> str:
        return json.dumps({"outcome_taxonomy": [c["key"] for c in store.outcome_classes()],
                           "capability_manifest": core.capability_manifest()}, default=str)

"""AppCore — the shared in-process context wiring store, buses, safety, and hardware adapters.

Both the MCP server and the companion viewer hold the SAME AppCore instance, so an MCP tool that
starts a sweep and a browser watching the Live Sweep screen observe one set of Python objects. This
is what lets *visible* MCP tools move the human's screen and *invisible* MCP tools read the store.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from importlib import metadata as importlib_metadata
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

from . import config
from .bus import EventBus, UICommandBus
from .domain.classifier import OutcomeClassifier
from .safety.audit import Auditor
from .safety.enforce import SafetyEngine
from .storage.analytics import Analytics
from .storage.store import Store


class RigBusyError(RuntimeError):
    """A second operation attempted to mutate/read the live rig concurrently."""


class AppCore:
    _instance: "AppCore | None" = None

    def __init__(self) -> None:
        self.rig = config.load_rig_config()
        self.bus = EventBus()
        self.uibus = UICommandBus()
        self.store = Store(bus=self.bus)
        self.analytics = Analytics(self.store.db_path)
        self.safety = SafetyEngine(self.rig)
        self.auditor = Auditor(self.store)
        self.classifier = OutcomeClassifier()
        self.scope = None            # ScopeAdapter (lazy)
        self.glitcher = None         # GlitcherAdapter (lazy)
        self.glitcher_connect_result: dict[str, Any] | None = None
        self.glitcher_connect_error: str | None = None
        self.device_lease = None
        self._rig_operation_lock = asyncio.Lock()
        self._rig_operation: str | None = None
        self.sweep_engine = None     # set below
        self.recovery_times: list[float] = []
        # Each TARGET gets its own project (accurate per-target attempt counts) — not a shared
        # "Default project". The active project is the current rig target's project.
        _profile = self.rig.project_profile
        _profile_name = str(_profile.get("id") or self.rig.target_model or "Unknown target")
        _proj = self.store.get_or_create_project(_profile_name)
        self.config_project_id = _proj
        self.active = {"campaign_id": None, "session_id": None, "sweep_id": None,
                       "unit_id": None, "target_id": None,
                       "acknowledged_target": None,   # gate: live campaigns require ack
                       "project_id": _proj}
        self.danger_state = {"awg_output": "OFF", "glitch": "DISARMED"}
        # lazy imports to avoid cycles
        from .io.scope.adapter import ScopeAdapter
        from .domain.sweep_engine import SweepEngine
        self.scope = ScopeAdapter(store=self.store)
        self.sweep_engine = SweepEngine(self)
        # ntfy.sh alerting (unattended-run notifications) — subscribes to the event bus
        from .io.notify import Notifier
        _ncfg = config.notification_settings(self.rig)
        self.notifier = Notifier(_ncfg["topic"], _ncfg["base_url"], _ncfg["enabled"])
        self.notifier.attach_bus(self.bus, rig_name=(self.rig.rig or {}).get("name", ""))
        # Freeze the authority used by every later session and live start.  Disk
        # edits after Python modules/configuration have been loaded require a
        # process restart; they cannot silently become provenance for old code.
        self._run_configuration_baseline = self._build_run_configuration_snapshot()
        self._restore_unresolved_project_state()

    def configure_notifications(self, *, enabled: bool, topic: str, base_url: str) -> dict[str, Any]:
        """Persist private alert settings and apply them without restarting."""
        status = config.save_notification_settings(
            enabled=enabled, topic=topic, base_url=base_url
        )
        values = config.notification_settings(self.rig)
        self.notifier.configure(values["topic"], values["base_url"], values["enabled"])
        self.auditor.record(
            "configure_notifications", "SAFE",
            {"enabled": bool(enabled), "topic": "<redacted>", "base_url": values["base_url"]},
            "executed",
        )
        self.bus.publish("notification_settings", self.notifier.status())
        return {"ok": True, **status}

    def _restore_unresolved_project_state(self) -> None:
        """Fail closed across process restart for v2 live sweep state.

        Historical GlitchLab left many legacy sweeps marked active.  Only a v2
        session sealed to the currently loaded project can represent this
        adapter's volatile state, so legacy rows are deliberately excluded.
        """
        rows = self.store.fetch_all(
            "SELECT sw.id sweep_id,sw.status sweep_status,se.id session_id,"
            "se.rig_config,c.id campaign_id,c.target_id,c.project_id "
            "FROM sweep sw JOIN session se ON se.id=sw.session_id "
            "JOIN campaign c ON c.id=se.campaign_id "
            "WHERE c.project_id=? AND sw.status IN "
            "('candidate-preserved','infrastructure-failure','running','paused') "
            "ORDER BY sw.created_at DESC",
            (self.config_project_id,),
        )
        active_profile_id = str(self.rig.project_profile.get("id") or "")
        for row in rows:
            try:
                snapshot = json.loads(row.get("rig_config") or "{}")
            except Exception:
                continue
            if snapshot.get("schema_version") != "glitchlab.session-config/v2":
                continue
            sealed_profile = dict(snapshot.get("resolved_project_profile") or {})
            if str(sealed_profile.get("id") or "") != active_profile_id:
                continue
            self.active.update({
                "campaign_id": row.get("campaign_id"),
                "session_id": row.get("session_id"),
                "sweep_id": row.get("sweep_id"),
                "target_id": row.get("target_id"),
                "project_id": row.get("project_id"),
                "restored_unresolved_state": True,
            })
            break

    # -- glitcher --------------------------------------------------------------------
    def _glitcher_config(self) -> tuple[str, dict[str, Any]]:
        project = self.rig.project_profile
        declaration = project.get("glitcher") or {}
        if declaration and not isinstance(declaration, dict):
            raise ValueError("project glitcher declaration must be a mapping")
        plugin = str(
            self.rig.rig.get("glitcher_override")
            or declaration.get("plugin")
            or self.rig.glitcher_id
        )
        kwargs = dict(declaration.get("config") or {})
        kwargs["project_profile"] = project
        kwargs["project_id"] = str(project.get("id") or self.rig.target_model)
        # The generic Husky adapter has no target-independent pulse/offset
        # ceiling.  Supply the selected target envelope per instance so a
        # historical target's timing limit cannot clip another DUT.
        if plugin.lower() in ("chipwhisperer_husky", "husky", "chipwhisperer"):
            limits = self.rig.limits
            glitch_limits = dict(limits.get("glitch") or {})
            power_limits = dict(limits.get("target_power") or {})
            kwargs.setdefault("width_ceiling", glitch_limits.get(
                "pulse_cycles_max", glitch_limits.get("width_cycles_max")))
            kwargs.setdefault("offset_ceiling", glitch_limits.get("ext_offset_max"))
            kwargs.setdefault("vcc_ceiling", power_limits.get("vcc_max_v"))
            kwargs.setdefault("allow_both_mosfets", not bool(
                glitch_limits.get("hp_lp_both_forbidden", True)))

        evidence = dict(project.get("evidence") or {})
        for section_name in ("rigol",):
            section = dict(evidence.get(section_name) or {})
            for field in ("python", "script"):
                if section.get(field):
                    section[field] = str(self.rig.resolve_project_path(section[field]))
            evidence[section_name] = section
        kwargs["evidence_cfg"] = evidence
        return plugin, kwargs

    def ensure_glitcher(self, connect: bool = True) -> Any:
        if self.glitcher is None:
            from .io.glitcher import make_glitcher
            plugin, kwargs = self._glitcher_config()
            self.glitcher = make_glitcher(plugin, **kwargs)
            # Candidate directories must be independently reconstructible even
            # if the SQLite session is unavailable later.
            setattr(
                self.glitcher,
                "run_configuration_snapshot",
                self.run_configuration_snapshot(),
            )
        if connect and not self.glitcher.connected:
            try:
                if not bool(getattr(self.glitcher, "is_simulator", False)):
                    from .io.device_lease import (
                        DeviceLease,
                        DeviceOwnershipError,
                        find_device_owner_conflicts,
                    )

                    if self.scope is not None and self.scope.bound:
                        raise DeviceOwnershipError(
                            "the companion scope session is already bound; unbind it before "
                            "starting a live project whose evidence collector owns the Rigol"
                        )

                    conflicts = find_device_owner_conflicts()
                    if conflicts:
                        raise DeviceOwnershipError(
                            "other processes may own Husky/J-Link/Rigol: "
                            + "; ".join(
                                f"PID {row['pid']} {row['name']}: {row['command']}"
                                for row in conflicts
                            )
                        )
                    if self.device_lease is None:
                        self.device_lease = DeviceLease(config.DATA_DIR / "live-rig.lock")
                        self.device_lease.acquire()
                result = self.glitcher.connect()
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise RuntimeError(f"glitcher connect did not pass: {result!r}")
                self.glitcher_connect_result = result
                self.glitcher_connect_error = None
            except Exception as exc:
                self.glitcher_connect_error = repr(exc)
                if self.device_lease is not None:
                    self.device_lease.release()
                    self.device_lease = None
                raise
        return self.glitcher

    def release_devices(self) -> dict[str, Any]:
        """Disconnect only when doing so cannot abandon a preserved state.

        A preserved live adapter deliberately keeps its hardware session open so
        target power or reset cannot change. Releasing the cross-process lease
        in that condition would falsely advertise the rig as available while it
        is still holding a volatile result, so normal shutdown must be blocked.
        """
        if bool(getattr(self.glitcher, "_preserve", False)):
            try:
                self.glitcher.safe_shutdown()
            except Exception:
                pass
            return {
                "ok": False,
                "exit_blocked": True,
                "reason": "preserved_target_state",
                "preserve_reason": getattr(self.glitcher, "_preserve_reason", None),
                "device_lease_retained": self.device_lease is not None,
            }
        try:
            if self.glitcher is not None:
                self.glitcher.disconnect()
        finally:
            if self.device_lease is not None:
                self.device_lease.release()
                self.device_lease = None
        return {"ok": True, "exit_blocked": False, "device_lease_retained": False}

    async def shutdown_for_exit(self) -> dict[str, Any]:
        """Finish any in-flight shot before deciding whether process exit is safe."""
        engine = self.sweep_engine
        tasks: list[asyncio.Task] = []
        if engine is not None:
            for sweep_id, task in list(getattr(engine, "_tasks", {}).items()):
                if task.done():
                    continue
                engine.stop(sweep_id)
                tasks.append(task)
        if tasks:
            # Each external stage is already bounded.  Do not cancel a shot in
            # its oracle/evidence window: its exception path is what latches an
            # unresolved candidate before the task returns.
            await asyncio.gather(*tasks, return_exceptions=True)
        return self.release_devices()

    def glitcher_bound(self) -> bool:
        return self.glitcher is not None and self.glitcher.connected

    def rig_operation_status(self) -> dict[str, Any]:
        return {"busy": self._rig_operation is not None,
                "operation": self._rig_operation}

    @asynccontextmanager
    async def exclusive_rig_operation(self, operation: str):
        """Fail-fast process-wide interlock for Husky/J-Link/Rigol/target state.

        Setting the owner happens before the first await, so two tasks on the
        shared event loop cannot both pass the gate.  The lock documents and
        enforces the same invariant if this implementation later gains await
        points around acquisition.
        """
        if self._rig_operation is not None:
            raise RigBusyError(
                f"rig is busy with {self._rig_operation}; refused {operation}"
            )
        self._rig_operation = str(operation)
        await self._rig_operation_lock.acquire()
        try:
            self.bus.publish("rig_operation_started", {"operation": self._rig_operation})
            yield
        finally:
            finished = self._rig_operation
            self._rig_operation = None
            self._rig_operation_lock.release()
            self.bus.publish("rig_operation_finished", {"operation": finished})

    def _build_run_configuration_snapshot(self) -> dict[str, Any]:
        """Read the current files and build a hash-addressed configuration."""

        def digest(path: Path | None) -> dict[str, Any] | None:
            if path is None or not path.is_file():
                return None
            return {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }

        root = Path(__file__).resolve().parent.parent
        source_paths = [
            root / "glitchlab" / "domain" / "sweep_engine.py",
            root / "glitchlab" / "io" / "glitcher" / "husky.py",
            root / "glitchlab" / "io" / "glitcher" / "husky_health.py",
            root / "glitchlab" / "connections" / "base.py",
            root / "glitchlab" / "connections" / "registry.py",
        ]
        helper_paths: list[Path] = []
        evidence = dict(self.rig.project_profile.get("evidence") or {})
        for section_name in ("rigol",):
            section = dict(evidence.get(section_name) or {})
            if section.get("script"):
                helper_paths.append(self.rig.resolve_project_path(section["script"]))
        profile_path = self.rig.project_profile_path
        rig_digest = digest(self.rig.path)
        if (rig_digest is not None and self.rig.source_sha256 is not None
                and rig_digest.get("sha256") != self.rig.source_sha256):
            raise RuntimeError(
                "rig_config.yaml changed after it was loaded; restart GlitchLab before "
                "opening a session"
            )
        profile_digest = digest(profile_path)
        loaded_profile_hash = self.rig.project_profile_source_sha256
        if (profile_digest is not None and loaded_profile_hash is not None
                and profile_digest.get("sha256") != loaded_profile_hash):
            raise RuntimeError(
                "project profile changed after AppCore loaded it; restart GlitchLab before "
                "opening a session so the adapter and sealed profile cannot diverge"
            )
        package_versions: dict[str, str | None] = {}
        for distribution in (
            "chipwhisperer", "pyvisa", "numpy", "mcp",
            "duckdb", "psutil", "PyYAML",
        ):
            try:
                package_versions[distribution] = importlib_metadata.version(distribution)
            except importlib_metadata.PackageNotFoundError:
                package_versions[distribution] = None
        from .connections import resolve_connector_selection

        selected_connector = resolve_connector_selection(self.rig.project_profile)
        public_rig = deepcopy(self.rig.raw)
        if isinstance(public_rig.get("notifications"), dict):
            public_rig["notifications"]["ntfy_topic"] = "<redacted>"
        return {
            "schema_version": "glitchlab.session-config/v2",
            "rig_config": public_rig,
            "resolved_project_profile": self.rig.project_profile,
            "effective_glitcher_id": self._glitcher_config()[0],
            "provenance": {
                "rig_config": rig_digest,
                "project_profile": profile_digest,
                "sources": [item for item in (digest(path) for path in source_paths) if item],
                "external_helpers": [
                    item for item in (digest(path) for path in helper_paths) if item
                ],
                "connector": selected_connector,
                "runtime": {
                    "python_executable": str(Path(sys.executable).resolve()),
                    "python_version": sys.version,
                    "implementation": platform.python_implementation(),
                    "platform": platform.platform(),
                    "packages": package_versions,
                },
            },
        }

    def run_configuration_snapshot(self) -> dict[str, Any]:
        """Return the immutable startup snapshot, refusing any on-disk drift."""
        current = self._build_run_configuration_snapshot()
        baseline = getattr(self, "_run_configuration_baseline", None)
        if baseline is None:
            return current
        if current != baseline:
            raise RuntimeError(
                "run configuration or critical source/helper files changed after AppCore "
                "startup; restart GlitchLab before opening or running a session"
            )
        return deepcopy(baseline)

    # -- capability manifest (spec §11.1 describe_schema, §22) -----------------------
    def capability_manifest(self) -> dict:
        from .safety.contracts import all_contracts_metadata
        try:
            project = self.rig.project_profile
        except Exception as exc:
            project = {"load_error": repr(exc)}
        try:
            from .connections import describe_connectors

            connectors = describe_connectors()
        except Exception as exc:
            connectors = {"error": repr(exc)}
        return {
            "profile": "operator",
            "rig": self.rig.rig,
            "project": {
                "id": project.get("id"),
                "title": project.get("title"),
                "path": str(self.rig.project_profile_path or ""),
                "recipes": project.get("recipes", {}),
                "discovery_workflow": project.get("discovery_workflow", []),
            },
            "limits_in_force": self.rig.limits,
            "target_limits": self.rig.target_safety_limits,
            "rig_wide_limits": self.rig.operator_limits,
            "danger_contracts": all_contracts_metadata(),
            "glitcher": {"id": self.rig.glitcher_id, "bound": self.glitcher_bound(),
                         "simulator": (self.glitcher.is_simulator if self.glitcher else None),
                         "connect_result": self.glitcher_connect_result,
                         "connect_error": self.glitcher_connect_error,
                         "preserved_state": bool(
                             getattr(self.glitcher, "_preserve", False)
                         ) if self.glitcher is not None else False,
                         "preserve_reason": getattr(
                             self.glitcher, "_preserve_reason", None
                         ) if self.glitcher is not None else None,
                         "preserve_leave_io_unchanged": bool(
                             getattr(self.glitcher, "_preserve_leave_io_unchanged", False)
                         ) if self.glitcher is not None else False,
                         "candidate_dir": str(getattr(
                             self.glitcher, "_candidate_dir", ""
                         ) or "") if self.glitcher is not None else None},
            "connectors": connectors,
            "scope": self.scope.status() if self.scope else {"bound": False},
            "rig_operation": self.rig_operation_status(),
            "danger_state": self.danger_state,
        }

    async def autobind_scope(self) -> None:
        """Best-effort companion bind for projects that do not own scope evidence.

        A project-managed Rigol acquisition is part of the atomic shot/oracle
        transaction.  Opening a second SCPI session at startup would bypass the
        process-wide rig interlock and can invalidate or destroy its evidence.
        """
        try:
            evidence = dict(self.rig.project_profile.get("evidence") or {})
            required = set(evidence.get("required_for_success") or [])
            if "rigol" in required or isinstance(evidence.get("rigol"), dict):
                return
            async with self.exclusive_rig_operation("startup_scope_autobind"):
                if self.scope and not self.scope.bound:
                    from .mcp_tools.scope import (
                        acquire_companion_device_lease,
                        release_companion_device_lease,
                    )

                    _lease, lease_created = acquire_companion_device_lease(self)
                    try:
                        res = await self.scope.bind()
                    except Exception:
                        if lease_created:
                            release_companion_device_lease(self)
                        raise
                    if res.get("ok"):
                        self.bus.publish("scope_bound", {"idn": res.get("idn"),
                                                         "resource": res.get("resource")})
                    elif lease_created:
                        release_companion_device_lease(self)
        except Exception:
            pass

    def set_danger_state(self, **kw) -> None:
        self.danger_state.update(kw)
        self.bus.publish("danger_state", dict(self.danger_state))

    # -- pre-campaign acknowledgment gate (spec §18; goal TASK 1.2) ------------------
    def acknowledge_target(self, target_model: str, stated: dict | None = None) -> dict:
        """Operator/agent confirms the ACTIVE target's enforced limits before a LIVE campaign.

        A meaningful gate: the caller must echo the key rig-config limits and they must MATCH
        (proving the config was actually reviewed for this target). Simulator campaigns do not
        require this. Recorded in the audit log.
        """
        stated = stated or {}
        rig_model = self.rig.target_model
        limits = {
            "pulse_cycles_max": self.rig.limit("glitch", "pulse_cycles_max"),
            "ext_offset_max": self.rig.limit("glitch", "ext_offset_max"),
            "num_glitches_max": self.rig.limit("glitch", "num_glitches_max"),
            "vcc_max_v": self.rig.limit("target_power", "vcc_max_v"),
        }
        problems = []
        if str(target_model) != str(rig_model):
            problems.append(f"target_model '{target_model}' != active rig target '{rig_model}'")
        for k, actual in limits.items():
            if k not in stated or stated[k] is None:
                problems.append(f"required limit {k} was not echoed")
                continue
            try:
                if float(stated[k]) != float(actual):
                    problems.append(f"stated {k}={stated[k]} != rig {actual}")
            except (TypeError, ValueError):
                problems.append(f"stated {k}={stated[k]!r} not numeric")
        if problems:
            self.auditor.record("acknowledge_target", "CAUTION",
                                {"target": target_model, "stated": stated}, "refused",
                                "acknowledgment_mismatch", {"problems": problems, "rig_limits": limits})
            return {"ok": False, "refused": True, "violated_rule": "acknowledgment_mismatch",
                    "problems": problems, "rig_target": rig_model, "rig_limits": limits}
        self.active["acknowledged_target"] = rig_model
        self.auditor.record("acknowledge_target", "CAUTION",
                            {"target": rig_model, "limits": limits}, "executed")
        self.bus.publish("target_acknowledged", {"target": rig_model, "limits": limits})
        return {"ok": True, "acknowledged": rig_model, "limits": limits}

    @classmethod
    def instance(cls) -> "AppCore":
        if cls._instance is None:
            cls._instance = AppCore()
        return cls._instance


def get_core() -> AppCore:
    return AppCore.instance()

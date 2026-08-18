---
name: glitchlab
description: Operate the GlitchLab MCP server and browser UI, create private target connectors, configure target-specific safety envelopes, and plan or run authorized fault-injection discovery and reproduction campaigns. Use when the user asks to open or run GlitchLab, connect a new UART/JTAG/SWD or other target, configure glitch safeguards, explore an unknown target, inspect campaign evidence, or reproduce a confirmed glitch.
---

# GlitchLab

Use GlitchLab only on hardware the user owns or is authorized to test. Keep every target connector, hardware identity, endpoint, credential, notification destination, capture, and target profile in GlitchLab's private data directory. Never add those values to the marketplace checkout.

## Open the app

When the user asks to run, open, show, or launch GlitchLab:

1. Call the GlitchLab MCP tool `get_glitchlab_status`.
2. Verify `ok=true` and take `ui_url` from the result.
3. Use the in-app browser control capability to create a new tab at that URL. Do not reuse or replace the user's current tab.
4. Report the active target and whether the adapter is a simulator. Never imply that failed live hardware silently fell back to simulation.

The server chooses an unused localhost port for each process. Do not assume a fixed port.

## Select the workflow

- For a first run, use the bundled simulator, call `get_workflow_state`, and follow `get_glitch_workflow(mode="discover")`.
- For a new physical target, create and validate a private connector and target profile before opening any device.
- For a known result, require `get_attempt_evidence` to return `fully_confirmed`, then use `get_reproduction_recipe`. Treat a stored `success` label without that contract as a candidate only.
- Stop on infrastructure errors, missing limits, identity mismatches, capture timeouts, or ambiguous state. Preserve interesting partial states until reviewed.

## Create a target connector

Call `get_connector_sdk_instructions` and `list_connectors` first. For implementation and acceptance details, read [connector-and-safeguards.md](references/connector-and-safeguards.md).

Create one connector folder under the returned `connector_root`. Copy the bundled `generic-example` only as structure; it is intentionally non-functional. Give the connector a new ID and implement target communication, triggering, observation, classification, recovery, and evidence serialization without embedding secrets.

Keep responsibilities separate:

- The GlitchLab adapter owns bounded fault delivery, hardware readback, disarming, rate limits, storage, and the process-wide rig lease.
- The connector owns target-specific transport, trigger/stimulus, baseline behavior, target identity, success semantics, recovery, and confirmation evidence.
- The target profile authorizes the exact connector, electrical limits, timing envelope, required checks, and evidence policy.

## Configure safeguards

Fail closed when a required value is unknown. Define both rig-wide ceilings and a target-specific envelope; GlitchLab applies the more restrictive value. At minimum review voltage, pulse duration, offset bounds, pulse count, simultaneous injection paths, attempt rate, recovery delay, trigger polarity/source, powered-off behavior, reset behavior, and stop/preserve conditions.

Begin with a no-glitch baseline, then timing characterization, then a low-energy coarse search. Expand one dimension at a time only when captures and readback show the target remains inside its reviewed envelope. Cluster disruptions, refine locally, repeat candidates independently, and require connector-owned checks before confirmation.

## Protect private settings

Configure ntfy alerts only from the browser Settings page. GlitchLab stores the topic in its private per-user `settings.json`, returns only a masked form, and excludes it from campaign configuration snapshots. Never paste a private topic into source, target YAML intended for publication, screenshots, logs, or chat output.

## Completion checks

Before claiming a connector or campaign is ready:

1. `list_connectors` discovers the intended private connector and its fingerprint.
2. `validate_connector_parameters` accepts defaults and boundary values and refuses invalid values.
3. A no-hardware import and simulator smoke test pass.
4. Live `preflight_check` validates identity, health, target baseline, and required instrument state.
5. A dry-run sweep reports the exact bounded point count and effective limits.
6. A deliberate timeout or disconnect becomes an infrastructure failure, never a hit.
7. Only complete, persisted required checks can produce `fully_confirmed`.

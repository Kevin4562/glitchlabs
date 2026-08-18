# Connector and safeguard reference

## Contents

- Connector layout
- Connector lifecycle
- Target profile
- Safeguard checklist
- Unknown-target search strategy
- Acceptance tests

## Connector layout

Create a private folder under the `connector_root` returned by `get_connector_sdk_instructions`:

```text
my-target/
├── glitchlab_connector.toml
├── __init__.py
├── connector.py
└── tests/
```

Use a manifest like:

```toml
[connector]
id = "my-target"
display_name = "My private target"
description = "Private target transport and classifier"
api_version = 1
entrypoint = "connector:MyTargetConnection"
```

Import the public SDK from `glitchlab.connections`. Subclass `ConnectionModule`; declare `ConnectionCapabilities` truthfully; define a strict JSON-schema-like `static_config_schema`; and use `DynamicParameter` for values that may vary by sweep.

Do not store credentials or physical addresses in Python. Put private values in the local target profile or environment variables, validate them at construction, and redact them from returned status.

## Connector lifecycle

Implement these operations with bounded timeouts:

- `connect`: open only the named device, prove its identity, and return `ok=true` only after health checks.
- `bind_glitcher`: retain a non-owning reference when the connector needs reviewed target I/O exposed by the adapter.
- `probe_status`: perform read-only health and identity checks.
- `prepare_attempt`: establish and prove a fresh baseline without fault injection.
- `trigger`: cause exactly one target event after GlitchLab arms the delivery hardware.
- `read`: return one `ConnectionReading` with raw, attributable observations.
- `classify_attempt`: apply explicit required checks; set `verified=true` only when all are present and true.
- `recover`: return the target to baseline only when preservation is not required.
- `disconnect`: close handles without implicitly resetting, flashing, or changing a preserved target.

Treat timeouts, driver exceptions, partial reads, stale frames, unexpected identities, and schema mismatches as infrastructure or ambiguous results. Never convert them to `no-effect` or `success`.

Run fragile vendor APIs in a bounded worker process when they can hang. Stream stage evidence so a killed worker still records where it stopped.

## Target profile

Keep the target profile in the private GlitchLab data directory. Use this outline and remove fields that are not applicable only after the safety review:

```yaml
schema_version: "glitchlab.target/v1"
id: "my-target"
title: "Private target"

target:
  model: "reviewed model"
  authorization_note: "owner or engagement reference"

glitcher:
  plugin: "chipwhisperer_husky"
  config:
    husky_serial: "from private settings"
    clkgen_freq: 10000000
    trigger_line: "tio4"

connector:
  id: "my-target"
  config: {}
  parameters: {}

safety_limits:
  glitch:
    pulse_cycles_max: 1
    ext_offset_min: 0
    ext_offset_max: 1
    num_glitches_max: 1
    repeat_max: 1
    hp_lp_both_forbidden: true
  target_power:
    vcc_nominal_v: 0.0
    vcc_max_v: 0.0
  recovery:
    min_seconds_between_cycles: 1.0
    max_cycles_per_minute: 10
  rate:
    max_attempts_per_second: 1
```

Replace placeholder numeric limits with values derived from the target datasheet, board measurement, and injection-path review. Zero or missing live limits should refuse operation.

## Safeguard checklist

Document and test:

- exact target, board revision, package, voltage rail, ground reference, and injection point;
- absolute maximum voltage/current and nominal rail measurement;
- glitch clock, pulse-unit conversion, hardware min/max, and readback tolerance;
- trigger source, polarity, expected cadence, and timeout;
- allowed output transistor or injection path, with simultaneous paths forbidden by default;
- power-off proof, discharge delay, reset drive/sense behavior, and back-power checks;
- connector device identity and exclusive ownership;
- maximum attempts per second, cooling/recovery time, and maximum continuous epoch;
- raw evidence captured for baseline, attempt, connector stages, hardware readback, and environment;
- stop-on-confirmation, stop-on-infrastructure-failure, and preserve-on-partial behavior;
- recovery operations, especially any reset, halt, flash, erase, unlock, memory write, or persistent write.

Declare every write capability in `ConnectionCapabilities`. Keep persistent writes disabled unless the user explicitly authorizes a reviewed workflow.

## Unknown-target search strategy

1. Establish stable power, clock, reset, trigger, transport, and target output without injection.
2. Record multiple no-glitch baselines and natural reset/error distributions.
3. Characterize event timing with the delivery path disabled.
4. Start at the shortest/lowest-energy pulse supported by the reviewed setup.
5. Sweep a sparse offset grid over one known event window while holding other variables fixed.
6. Repeat disruption cells to separate real structure from noise.
7. Refine around stable clusters; widen energy only in small, justified steps.
8. Preserve and independently inspect partial candidates before recovery.
9. Reproduce a fully confirmed candidate from a fresh baseline across independent runs.

GlitchLab reduces bookkeeping, unsafe parameter drift, and analysis latency. It cannot guarantee a successful glitch: physical access, the correct rail and timing reference, sufficient measurement bandwidth, and a target-specific success oracle remain decisive.

## Acceptance tests

Test without hardware first:

- manifest discovery and source fingerprinting;
- strict config and dynamic-parameter validation;
- import with no device drivers loaded;
- deterministic fake transport for each verdict;
- timeout and disconnect classification;
- evidence serialization and redaction;
- required-check completeness;
- refusal when limits, identity, or baseline evidence are missing.

Then test live in this order: connect/disconnect, read-only health, no-glitch baseline, trigger with delivery disabled, one minimum-energy attempt, dry-run bounds, bounded short sweep, forced timeout, candidate preservation, and recovery. Do not start unattended campaigns until every earlier stage passes.

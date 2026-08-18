# Public release review

Reviewed for the 2.0.0 marketplace release on 2026-08-18.

## Conclusions

### Can a new user install GlitchLab and begin working on an unknown target?

Yes, for an authorized target and with suitable physical equipment. Installing the marketplace plugin is sufficient to start the MCP server, browser UI, simulator, connector skill, private configuration, and all Python dependencies. The skill gives the user a concrete path from target characterization through a guarded first sweep.

GlitchLab cannot guarantee a successful fault on an arbitrary target. Success still depends on physical access, electrical characterization, injection and measurement quality, trigger stability, and a correct target-specific oracle. The public generic connector is intentionally non-functional and contains no target secrets or precomputed result.

### Does it improve time-to-result?

Yes. It removes repeated campaign plumbing and makes the investigation cumulative: connector scaffolding, safety schemas, preflight, parameter sweeps, evidence capture, statistics, clustering, prediction, refinement, state preservation, and reproduction recipes use one data model. The largest expected improvement is not raw pulse rate; it is avoiding invalid experiments, lost candidates, false confirmation, and repeated manual setup.

### What must remain private?

The release boundary excludes:

- rig configuration and network addresses;
- target profiles, identifiers, protocols, memory maps, safeguard values, and real connectors;
- firmware and binaries;
- databases, CSV exports, waveforms, captures, evidence bundles, and screenshots that were not explicitly reviewed;
- notification topics and local settings;
- logs, debug output, scratch files, process files, and test artifacts;
- historical workspace files and Git commits that contained any of the above.

The public repository is therefore created from a clean root commit containing only the reviewed marketplace files, public documentation, and `plugins/glitchlab`.

## Verification performed

- Marketplace schema: one marketplace entry and one plugin.
- Plugin manifest: local assets, skill, and MCP definition validated.
- Fresh locked environment: MCP 2.x and all runtime dependencies resolved under Python 3.13.
- MCP transport: initialized over stdio as `GlitchLab` 2.0.0.
- MCP registration: all 77 expected tools enumerated.
- Browser runtime: dynamic loopback port returned by `get_glitchlab_status`; dashboard and settings pages loaded.
- Privacy: notification topic blank by default, password-style UI input, masked status, local-only settings, and no topic in exported configuration.
- Source scan: no target connector, target identifiers, private network addresses, personal paths, desktop entrypoint, or unreviewed capture artifacts in the release tree.
- History scan: public commit has no parent and includes only the allowlisted release paths.

## Known boundaries

- Hardware must already be connected, electrically characterized, and accessible to the operating system.
- The operator must create and validate a real connector before live work on a new target.
- Conservative defaults reduce risk but cannot substitute for board-specific electrical limits.
- Analysis and adaptive search improve prioritization; they do not create physical observability where none exists.

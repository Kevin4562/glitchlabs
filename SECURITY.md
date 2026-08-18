# Security policy

GlitchLab is intended only for hardware the operator owns or is explicitly authorized to test.

Do not include target profiles, real connectors, firmware, captures, credentials, notification topics, equipment addresses, or local settings in public issues or pull requests. Reproduce software defects with the bundled simulator and generic connector whenever possible.

Report a suspected secret or private-target disclosure privately to the repository owner before opening a public issue. If a disclosure reaches Git history, removing the current file is not sufficient; rotate the affected value and rebuild the public history from reviewed content.

Live actuation must remain bounded by both rig and target limits. Changes that weaken fail-closed behavior, allow simulator fallback, convert timeouts into success, or bypass persisted confirmation checks are security-sensitive and require focused review.

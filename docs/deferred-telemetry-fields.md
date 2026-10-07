# Deferred telemetry fields

Data Cloud's S2S "Data from Other Sources" event schema has a **30-field ceiling** per
event. The expanded Robot Rumble telemetry set totals 34 fields, so four were held back
to stay under the limit. The schema upload fails with a generic *"An unexpected error has
occurred"* if you exceed 30 — that message is the only symptom.

## Fields held back (not sent to Data Cloud)

| Field (Telemetry attr) | S2S key | Why it's low-priority for the demo |
| --- | --- | --- |
| `attitude_pitch` | `attitudePitch` | Near-zero on a flat floor; little visual value |
| `attitude_roll` | `attitudeRoll` | Near-zero on a flat floor; little visual value |
| `attitude_yaw` | `attitudeYaw` | Redundant — `heading` already carries direction |
| `voltage` | `voltage` | Redundant — `battery` % tells the same story on the dashboard |

## Important: they are NOT removed from the pipeline

These fields still exist on the `Telemetry` dataclass and are still populated by
`FakeRobot` (and will be by `SpheroRobot`). Only the **Data Cloud forwarding** is
switched off, in two places:

- `s2s_events.py` → `build_event()` — the four keys are commented out
- `schemas/s2s_schema.json` — the four field definitions are removed

## How to add any of them back

Decide which four-or-fewer fields matter more than the ones above, then swap. To re-add,
e.g., `attitudeYaw`:

1. Remove a lower-value field from `schemas/s2s_schema.json` to make room (stay at ≤30).
2. Add the field definition back to `schemas/s2s_schema.json`.
3. Uncomment the matching key in `s2s_events.py` `build_event()`.
4. Re-upload the schema in Data Cloud Setup, remap the data stream, rebuild `RobotLive_DG`.
5. Mirror the change in `datacloud.py` and `schemas/datacloud_schema.yaml` if you use the
   batch path (it has no 30-field limit, so it currently carries the full set).

## Batch path note

The batch Ingestion API (`datacloud.py` / `schemas/datacloud_schema.yaml`) has **no 30-field
limit**, so it still carries the complete set including attitude and voltage. Only the S2S
real-time path is trimmed. If you ever make batch the primary transport, nothing is lost.

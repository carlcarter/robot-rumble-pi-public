# Server-to-Server (S2S) setup: real-time telemetry

Why: the batch Ingestion API processes data roughly every 3 minutes. The S2S events endpoint
feeds the real-time layer directly (Salesforce quotes ~500 ms for 95% of events), which is
what `RobotLive_DG` serves from.

The code is already in place and switched off. `DATACLOUD_MODE=batch` (the default) behaves
exactly as before. Work through the steps below, then flip the mode.

## What already exists

| File | Purpose |
| --- | --- |
| `schemas/s2s_schema.json` | Event schema to upload in step 3 |
| `s2s_auth.py` | JWT bearer auth, then Data Cloud token exchange |
| `s2s_events.py` | Background-thread sender (never blocks the 20 Hz loop) |
| `s2s_smoke_test.py` | Sends one event and reports which stage fails |
| `watch_graph.py` | Live view of the graph, with per-event lag |
| `test_s2s.py` | Offline tests (no org needed) |

Signing key pair and certificate are in `~/.robot-rumble/s2s/` (outside the repo, folder
mode 700). Upload `certificate.crt` to the app. `private.key` stays on this machine; copy it
to the Pi separately when deploying, and never commit it.

## Steps in the org

1. **External Client App.** Setup > External Client App Manager > New.
   - Enable OAuth. Callback URL can be a placeholder such as `https://localhost/callback`.
   - Scopes: `api`, `refresh_token, offline_access`, `cdp_ingest_api`.
   - Enable JWT Bearer Flow and upload `~/.robot-rumble/s2s/certificate.crt`.
   - Untick "Require PKCE".
   - Policies: Permitted Users = "All users may self-authorize" (or admin-approved plus a
     permission set), IP Relaxation = "Relax IP restrictions".
   - Copy the Consumer Key into `S2S_CLIENT_ID`.
2. **Authorise the user once.** With self-authorise, the integration user must approve the
   app once or the JWT flow returns `user hasn't approved this consumer`. Open this in a
   browser logged in as that user, using your My Domain, Consumer Key and callback URL:
   `https://<my-domain>.my.salesforce.com/services/oauth2/authorize?response_type=code&client_id=<KEY>&scope=api refresh_token cdp_ingest_api&redirect_uri=<CALLBACK>`
   Approve, ignore the redirect error. Put that username in `S2S_USERNAME`.
3. **Server-to-Server connection.** Data Cloud Setup > Web & Mobile SDK > New.
   - Connector type: Server to Server, then "Data from Other Sources".
   - Upload `schemas/s2s_schema.json`. Check the preview: event `RobotTelemetryRT`, category `Other`,
     primary key `eventId`.
   - Copy the Source ID into `S2S_APP_SOURCE_ID`.

   > **If you hit "An unexpected error has occurred" here with the status stuck on
   > "Schema Required":** this happens if the event's developer name collides with one
   > already used elsewhere in Data Cloud's ingestion namespace. The original schema used
   > `RobotTelemetry`, which the existing batch Ingestion API connector already owns
   > (`DATACLOUD_SOURCE_NAME`/`DATACLOUD_OBJECT_NAME` in `.env`) — developer names must be
   > unique across the whole namespace, not just within this connector. The schema now
   > uses `RobotTelemetryRT` instead; re-upload the current `schemas/s2s_schema.json` and Save.
4. **Data stream.** Data Streams > New > Server to Server > select `RobotTelemetryRT` > Deploy.
5. **Mapping (the open question).** Try mapping the stream onto the existing `Robot` /
   `RobotTelemetry` DMOs so `RobotLive_DG` keeps working. If the UI only offers new DMOs,
   map to new ones and rebuild the graph against them.
   - Field mapping: `eventId` > event_id, `dateTime` > event_time, `robotId` > robot_fk
     (the relationship key), `speed`, `heading`, `battery`, `collision`, `activeHazard`,
     `heatNumber` to their counterparts.
   - `collision` arrives as 0/1 (the S2S schema has no Boolean type), while the current DMO
     field is Boolean. The mapping rejects this, and a formula field can't fix it — Data 360
     doesn't support Boolean as a formula return type on Salesforce connectors, and
     Number-to-Boolean isn't on the allowed cross-type list either (only Number-to-Text is).
     Add a new Number field on the DMO (e.g. `Collision_RT__c`) and map `collision` there
     instead; leave the existing Boolean field for the batch connector. Treat non-zero as
     a collision on the dashboard side.
   - The graph must include the DMO as a real-time member for the sub-second path.

## Local steps

```bash
cd ~/dev/robot-rumble-code
# edit .env: S2S_CLIENT_ID, S2S_USERNAME, S2S_PRIVATE_KEY_PATH, S2S_APP_SOURCE_ID
.venv/bin/python3 s2s_smoke_test.py          # each stage prints PASS/FAIL
```

Then, in two terminals:

```bash
.venv/bin/python3 watch_graph.py EDI 1       # terminal 1: live view with lag
DATACLOUD_MODE=s2s .venv/bin/python3 control_loop.py   # terminal 2
```

`DATACLOUD_MODE=both` sends to both transports, for a side-by-side comparison.

## Confirmed against the live org (2026-10-06)

- `s2s_smoke_test.py` passed all four stages, including a real `204` from `/server/events`.
  The Data Cloud token (via `/services/a360/token`), not the plain Salesforce token, is correct.
- The JWT bearer flow needs the one-time self-authorization in step 2 even with "All users
  may self-authorize" set — until the integration user approves the app once, every token
  request fails with `invalid_grant: user hasn't approved this consumer`.
- If the External Client App still has "Require PKCE" enabled, the authorize URL in step 2
  needs `&code_challenge=...&code_challenge_method=S256` appended (any valid-looking value
  works — the consent is recorded on clicking Allow, before the broken localhost redirect).
  Untick PKCE in Policies to avoid needing this.

## Assumptions I could not verify without the org
- **Undeclared fields.** The schema declares every field the payload sends. A 400 almost
  always means a name or type mismatch; the response body is logged.
- **JWT audience.** Set to your My Domain URL. If the token call fails with an audience
  error, the body says so.
- **Rate limits.** I found no published per-second limit for this endpoint. The sender
  batches up to 50 events per request, roughly 4 requests a second at the default settings.
  Raise `DATACLOUD_SEND_INTERVAL_MS` if you see 429s.
- **Timestamps.** `FakeRobot` stamps events with local time. Local clock skew to Salesforce
  measured +0.9 s, so lag figures from `watch_graph.py` are accurate to about a second.

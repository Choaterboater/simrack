---
name: labfront
description: LabFront runbook for vJunos sandboxes on Proxmox. Use for LabFront deploys, lab builds, rebuilds, teardowns and lab troubleshooting.
tags: [labfront, proxmox, vjunos]
---

# LabFront: vJunos sandboxes on Proxmox

LabFront builds throwaway vJunos switch **sandboxes** on one Proxmox host and can adopt them into a Mist org. A TOML **lab profile** says what is protected and which ranges sandboxes may use. The detail lives in the LabFront checkout, or in `/opt/labfront/` on the host: `ADVICE.md` (build order, teardown by hand, symptom table, host gotchas) and `README.md` (profile keys, environment, deploy). Read the matching ADVICE section before you act on it.

## When to use
- Building, rebuilding or tearing down a sandbox, or importing a fabric shape.
- Deploying or upgrading LabFront on a Proxmox host.
- A sandbox misbehaves: no Mist adoption, flapping BGP, HTTP 596, low memory, a bad cable.

Work only on sandboxes: VMIDs and bridges inside the profile's `[sandbox]` ranges. LabFront refuses anything under `[protected]`; that is someone's live lab.

## Sign-in and tokens
Secrets live in `/opt/labfront/labfront.env` (mode 600), loaded by the systemd unit. Name them; keep their values out of chat, logs and commits.
- `LABFRONT_PVE_TOKEN`: Proxmox API token, `user@realm!tokenid=<secret>`.
- `MIST_TOKEN`: org admin token for the profile's `[mist] org_id`.
- `LABFRONT_TOKEN`: when set, every API call needs `Authorization: Bearer <token>`. Unset, LabFront answers on loopback only: `ssh -N -L 8787:127.0.0.1:8787 <ssh host>`, then open `http://127.0.0.1:8787`.

## Read first
Done when you can state: profile loaded or not, writes on or off and why, free RAM, and each sandbox with its nodes.
- `curl -s http://127.0.0.1:8787/api/state`, on the host or through the tunnel: `profile.loaded`, `writes_enabled`, `read_only_reason`, `mist.writes_enabled`, `host.free_ram_mb`, `production` (the protected lists), `sandboxes`.
- `GET /api/sandboxes/<name>` and `GET /api/sandboxes/<name>/mist/health` for one sandbox.
- On the host: `pvesh get /nodes` (the exact node name), `qm list`, `ip link show <bridge>` (MTU).

## Changing things (Casper asks)
MCP: Casper's change box asks; don't ask again in chat. Else ask the user first; show the exact call.
Casper also asks before a shell command reaches a new host.
Every change is a POST with `Content-Type: application/json`.
1. Confirm the target is a sandbox and `writes_enabled` is true. If not, `read_only_reason` says why: no profile (`LABFRONT_PROFILE`) or `LABFRONT_ALLOW_WRITES` not `1`. Mist changes also need `LABFRONT_MIST_WRITES=1`. Editing `labfront.env` and `systemctl restart labfront` is itself a change: ask.
2. Snapshot first, in both places (`revert` and `mist/revert` take the label):
   WRITE: `POST /api/sandboxes/<name>/snapshot` and `POST /api/sandboxes/<name>/mist/snapshot` with `{"label": "<label>"}`.
3. Make one change, then read state again. Stop at the first error and show it.

The writes:
- WRITE: `POST /api/sandboxes` `{"name": "<name>", "recipe": "single-switch", "with_mist_site": true}` builds a sandbox.
- WRITE: `POST /api/shapes/<shape>/build` `{"name": "<name>"}` builds from an imported shape.
- WRITE: `POST /api/sandboxes/<name>/nodes/<node>/adopt` joins a switch to the sandbox's Mist site via its serial console.
- WRITE: `POST /api/sandboxes/<name>/mist/fabric` builds the fabric in Mist; it saves a Mist revert point first.
- WRITE: `POST /api/sandboxes/<name>/fabric/check` reports cabling; with writes on it also re-plugs wrong ends.
- WRITE: `POST /api/sandboxes/<name>/teardown` `{"confirm": true}` removes guests, bridges and the Mist site (`"keep_mist": true` keeps the site).
- WRITE: `LABFRONT_HOST=<ssh host> ./deploy/deploy.sh` copies the checkout to `/opt/labfront`, runs the tests there and restarts the service.

Every write has run only against test fakes so far: watch the first real one end to end.

## Paging and rate limits
- No paging: `/api/state` returns the whole host.
- Changes run one at a time; a second change waits for the first. A slow build is still running: check `GET /api/sandboxes` before resending.
- Poll `mist/health` no faster than every 30 s; Mist limits calls per token (HTTP 429).
- A switch's serial console takes one client: close `qm terminal` before Adopt.

## Common traps
- The node name is case sensitive; the wrong case gives HTTP 596 "certificate verify failed". Copy it from `pvesh get /nodes`.
- Fabric bridges are MTU 9216; at 1500 the overlay BGP flaps every ~90 s.
- Clone only from a never-booted template: vJunos bakes serial and MAC on first boot. Prove two clones report different serials before building more (ADVICE, step 3).
- A build that would leave less than `limits.min_free_ram_mb` free is refused. vJunos memory is never ballooned; stop a sandbox or use `single-switch`.
- Static out-of-band management needs `use_mgmt_vrf: true`, or the switch loses the cloud.
- One cable, one bridge, one /31; never a shared transit VLAN.
- Bridges in files that `/etc/network/interfaces` sources are invisible to the Proxmox API (ADVICE, section 7).
- A profile that protects one of LabFront's fixed sandbox ranges blocks fabric builds (README, The safety model).
- The hookscript `deploy/labfront-sbx.sh` matches `sbx*`; a new `bridge_prefix` needs it changed too.

More symptoms and fixes: ADVICE, section 4.

## Testing with saved sample data
- From the checkout, `python3 -m unittest discover -s tests -t .` runs everything against fakes (`tests/fakes.py`) and `tests/fixtures/lab-profile.toml`: no host, no tokens.
- New behaviour gets a failing test first, at an existing seam: profile, write refusal, guardrails, management pool, hygiene.
- Sample data uses placeholders only: UUIDs starting `00000000-0000-0000-0000-000000` and MACs starting `02:00:00`. Test 14 fails on anything else.
- Test 12's paced-chunks case fails about 1 run in 40 (a race in the fake console); rerun once before chasing it.

## Public docs
- https://pve.proxmox.com/pve-docs/api-viewer/
- https://pve.proxmox.com/pve-docs/qm.1.html
- https://www.juniper.net/documentation/product/us/en/vjunos-switch/
- https://www.juniper.net/documentation/us/en/software/mist/api/http/getting-started/how-to-get-started

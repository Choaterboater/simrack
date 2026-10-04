---
name: simrack
description: SimRack runbook for vJunos sandboxes on Proxmox. Use for SimRack deploys, lab builds, rebuilds, teardowns and lab troubleshooting.
tags: [simrack, proxmox, vjunos, mcp]
---

# SimRack: vJunos sandboxes on Proxmox

SimRack builds throwaway vJunos switch **sandboxes** on one Proxmox host and can adopt them into a Mist org. Its setup page says what is protected and which ranges sandboxes may use. Its MCP server offers only the changes SimRack may make right now. Detail: `/opt/simrack/ADVICE.md` (build order, symptoms, host gotchas) and `README.md`. Read the matching ADVICE section before you act.

## When to use
- Building, rebuilding or tearing down a sandbox, or building one from an imported fabric shape.
- Deploying or upgrading SimRack on a Proxmox host.
- A sandbox misbehaves: no Mist adoption, flapping BGP, HTTP 596, low memory, a bad cable.

Work only on sandboxes; what the setup page protects is someone's live lab.

## Sign-in and tokens
- The Proxmox and Mist tokens are pasted on SimRack's setup page and kept in `/opt/simrack/state/tokens.json` (mode 600). You never see them. What a token may do decides what SimRack can change.
- `SIMRACK_TOKEN` is needed to listen beyond loopback; once set, every call needs it, the MCP server's too (README, MCP).
- On loopback, reach the page with `ssh -N -L 8787:127.0.0.1:8787 <ssh host>`, then open `http://127.0.0.1:8787`.
- Casper: the user adds this to `~/.casper/mcp.json`, then types `/mcp connect simrack`.

```json
{"mcpServers": {"simrack": {"command": "ssh",
  "args": ["-T", "-o", "BatchMode=yes", "<ssh host>", "cd /opt/simrack && exec python3 -m simrack mcp"]}}}
```

## Read first
Done when you can state: setup saved or not, changes on or off and why, free RAM, and each sandbox with its nodes.
- `state`: `profile.loaded`, `writes_enabled` and `read_only_reason`, `paused`, `mist.writes_enabled`, `assistants.risky`, `host.free_ram_mb`, `production`, `sandboxes`.
- `get_sandbox`, `mist_health`, `list_recipes`, `list_shapes`.
- Without MCP: `curl -s http://127.0.0.1:8787/api/state`, on the host or through the tunnel.
- On the host: `pvesh get /nodes` (the exact node name), `qm list`, `ip link show <bridge>` (MTU).

## Changing things (Casper asks)
MCP: Casper's change box asks; don't ask again in chat. Else ask the user first; show the exact call.
Casper also asks before a shell command reaches a new host.
Writes start off in Casper; the user turns them on with `/mcp writes simrack`.
1. A missing or refused change says why: paused, a token that may not write, no Mist token, risky tools not ticked. Only the user turns it on, on the setup page or the pause button.
2. Save first, in both places, with one label:
   WRITE: `save_point` and `mist_save_point`.
3. Make one change, then look again. Stop at the first error and show it.
4. "Still running" with a job number: call `job_result` with it. Never send the change again.

- WRITE: `build_sandbox` (a recipe, plus `template_vmid` or `image`) or `build_from_shape` builds a sandbox; `with_mist_site` adds its Mist site.
- WRITE: `power_node`, `add_cable`, `move_cable`, `remove_cable`. `check_cabling` also re-plugs wrong ends while changes are on.
- WRITE: `mist_create_site`, `mist_build_fabric` (saves a Mist point first), `adopt_switch` (through the serial console).
- WRITE: once the setup page ticks Assistants: `tear_down` (`keep_mist` keeps the site), `delete_node`, `revert_mist`, `revert_guests`, `console_command`. Casper counts the first three and `remove_cable` as deletes: off until `/mcp allow simrack`.
- WRITE: `SIMRACK_HOST=<ssh host> ./deploy/deploy.sh` copies the checkout to `/opt/simrack`, runs the tests there and restarts the service.

Changes have run only against fakes so far: watch the first real one end to end.

## Paging and rate limits
- No paging: `state` returns the whole host.
- Changes run one at a time; a second waits for the first.
- A tool waits up to 50 s (`--wait`), then answers with a job number; keep that under Casper's 90 s call limit (`"callTimeout"`).
- Poll `mist_health` no faster than every 30 s; Mist limits calls per token (HTTP 429).
- A serial console takes one client: close `qm terminal` first.

## Common traps
- The tool list follows SimRack: a pause or a setup change shows within about 15 s.
- Casper gives the server a small environment: if ssh needs your agent, add `"env": {"SSH_AUTH_SOCK": "${SSH_AUTH_SOCK}"}`.
- The node name is case sensitive; the wrong case gives HTTP 596 "certificate verify failed". Copy it from `pvesh get /nodes`.
- Fabric bridges are MTU 9216; at 1500 the overlay BGP flaps every ~90 s.
- Clone only from a never-booted template, and prove two clones report different serials first (ADVICE, step 3).
- A build that would leave less than `limits.min_free_ram_mb` free is refused: stop a sandbox or use `single-switch`.
- Static out-of-band management needs `use_mgmt_vrf: true`, or the switch loses the cloud.
- One cable, one bridge, one /31; never a shared transit VLAN.
- Bridges in files that `/etc/network/interfaces` sources are invisible to the Proxmox API (ADVICE, section 7).
- Protecting one of SimRack's fixed sandbox ranges blocks fabric builds (README, The safety model).
- A start from the Proxmox GUI keeps LACP only if the template carries `deploy/simrack-sbx.sh`. It matches `sbx*`: a new `bridge_prefix` needs it changed too.
- Mist "certificate verify failed" means the network inspects TLS: add its CA to the host's trust store; never turn checks off.

More symptoms and fixes: ADVICE, section 4.

## Testing with saved sample data
- `python3 -m unittest discover -s tests -t .` runs everything against fakes (`tests/fakes.py`): no host, no tokens.
- New behaviour gets a failing test first, at an existing seam: profile, guardrails, tokens, setup page, MCP server.
- Sample data uses placeholders only: UUIDs starting `00000000-0000-0000-0000-000000` and MACs starting `02:00:00`. Test 14 fails on anything else.

## Public docs
- https://pve.proxmox.com/pve-docs/api-viewer/
- https://pve.proxmox.com/pve-docs/qm.1.html
- https://www.juniper.net/documentation/product/us/en/vjunos-switch/
- https://www.juniper.net/documentation/us/en/software/mist/api/http/getting-started/how-to-get-started
- https://modelcontextprotocol.io/specification/2025-11-25

# SimRack

A sandbox front end for a **vJunos-switch + Juniper Mist lab on Proxmox VE**.

People can build, re-cable, break and rebuild a lab from a web page, without
touching Proxmox by hand and without any risk to the live lab. SimRack's setup
page saves the lab profile, which says what is live. SimRack refuses any action
that would touch what it lists, and until a profile is saved it changes nothing.
Its Proxmox and Mist tokens decide what else it may change: there is no switch
to turn writes on. An assistant can drive it too, through its MCP server.

Standard library only (Python 3.11 or later). No pip, no build step, nothing to
install on the host. The page is plain files in `simrack/static/`
(`index.html`, `app.css`, `app.js`, `theme.js`), served as they are.

## Status

SimRack grew in four stages. All four are covered by tests against in-memory
fakes; only part of it has run on a real host.

| Stage | What it adds | On a real host |
|---|---|---|
| Crawl | recipes, sandboxes, cables, power, snapshots, teardown, the safety model | read-only only: the inventory, memory and the protected lab. Nothing has been built with writes on. |
| Walk | importing a live Mist fabric as a shape | yes: a live fabric imported read-only, cabled from LLDP |
| Run | building a sandbox from a shape, adopting its switches into Mist over the serial console | **untested on real gear** |
| Drive | building the sandbox's fabric in Mist, checking the cabling three ways | **untested on real gear** |

The setup page, token-decided access and the MCP server came last: this version
has not run on a real host yet. Every change asks first, so look around before
you answer Yes, and read `ADVICE.md` before the first build.

## What it does

| Button | What happens |
|---|---|
| **Build sandbox** | Clones a vJunos template into the sandbox range (320-399 by default), creates MTU 9216 bridges for the recipe cabling, and optionally creates a Mist site. |
| **Plug in / Move / Unplug** | Creates, rewires or deletes the bridge and the two NICs. This is the "move the cable" case. |
| **Add (template vmid or image)** | Clones another switch, or boots any PVE volume as a node. |
| **Up / Down** | Starts or shuts down a sandbox switch. |
| **Save now / Revert Proxmox** | `qm snapshot` / `qm rollback` for every guest in the sandbox. |
| **Create Mist site** | Creates the sandbox's own Mist site. Never a live one. |
| **Build fabric in Mist** | Makes the sandbox's site match its cables: data and voice networks and a VRF on the sandbox ranges (management stays out of band on fxp0), every switch managed by Mist, and an EVPN topology with a fabric port at both ends of each cable. Saves a Mist revert point (`before-fabric-…`) first. Works for recipe and shape sandboxes alike; **Build again** after re-cabling. |
| **Check cabling** | Checks each cable three ways: Proxmox (both ends on the cable's bridge, link up), LLDP as Mist reports it, and the Mist topology. With writes on it plugs wrong ends back in and parks stray ports; read-only it only reports. It never adds NICs and never writes to Mist. |
| **Snapshot / Revert Mist** | Saves the site setting, every EVPN topology in full and every device config, and puts them back. A topology made since the snapshot is deleted, and one deleted since is made again. Root passwords stay out of the file; revert puts the sandbox's own password back. |
| **Health** | Reads device connection and `config_status` from Mist. |
| **Tear down** | Deletes the guests, the bridges and the Mist site. Never the live lab. |
| **Import (Shapes)** | Reads a Mist EVPN topology you paste or drop, and previews it as a sandbox plan: switches, cabling, and whether it fits on the host. Nothing is built and nothing is sent to Mist. |
| **Build (Shapes)** | Builds a sandbox from a shape: the switches you tick, cabled the way Mist saw them, plus an optional Mist site of its own. When the whole shape doesn't fit, a slice with a switch from each tier is pre-ticked. Cables that can't be made are left out and listed with the reason. |
| **Adopt into Mist** | Logs in over the switch's serial console, sets the sandbox's root password, turns on DHCP on fxp0 and enters the sandbox site's adoption commands. If Junos refuses a line, the change is rolled back. Needs SimRack to be able to change both the lab and Mist, and the sandbox's own Mist site. |
| **Join Mist** | A checklist on the sandbox page: the site, each switch's adoption, Build the fabric in Mist (with the values it sends, and Copy as text), then Check the cabling. **Reveal** shows the sandbox's root password; it works read-only and is never written to Activity. |
| **Setup** | The tokens and the lab profile: what is live, the management network, the sandbox ranges, what an assistant may do. It opens by itself until a profile is saved. See [The lab profile](#the-lab-profile). |
| **Pause / Resume** | Stops every change until you resume, even across restarts; looking still works. A build under way stops at its next step and removes what it made. |

Every button that changes something asks first: **1 No · 2 Yes, this once · 3 Yes
for this session**, and Enter or Esc is No. Tear down, revert, power off, delete
and setup saves ask every time. A change SimRack may not make right now has its
button turned off, and the top bar says why.

## The lab profile

The setup page (**Setup** in the top bar) saves one TOML file per host,
`state/lab-profile.toml`, that says what is live. You never edit it by hand.
With a Proxmox token the page lists the host's guests, containers, bridges and
subnets, and with a Mist token the org's sites; until a profile is saved every
one is ticked as live. Type anything it cannot see into **Also protect**, such
as bridges in files that `/etc/network/interfaces` sources (ADVICE.md,
section 7). Later visits mark guests found since as **new** and saved ones that
are gone as **not found now**. **Export** and **Import…** move the file between
hosts; an import from an older SimRack leaves out the keys it no longer reads
and says where each setting went. A save or import that moves the sandbox
range, prefix or park bridge off a sandbox already built is refused, since
teardown could no longer remove it; tear it down first. Protecting part of one
is saved, and the page names what SimRack now leaves alone, even at teardown.
`lab-profile.example.toml` shows every key.

| Table | Keys | Meaning |
|---|---|---|
| `[proxmox]` | `node` (required) | The node name, spelled exactly as Proxmox spells it: the API is case sensitive. |
| `[mist]` | `org_id` | Your org: Mist changes stay off until it is set. Its cloud is picked beside the Mist token. |
| `[management]` | `bridge`, `cidr`, `pool` (required), `vlan` | Where fxp0 goes. Leave `vlan` out when the management network is untagged. The pool must sit inside `cidr`. |
| `[protected]` | `vmids`, `lxc`, `bridges`, `mist_sites`, `subnets` | Everything live. SimRack refuses any action that would touch these. |
| `[sandbox]` | `vmids`, `lxc`, `bridge_prefix`, `park_bridge` | Optional. The defaults are 320-399, 350-399, `sbx` and the prefix plus `park`. Linux caps a bridge name at 15 characters, and a cable bridge is the prefix, two vmids and two port digits, like `sbx321_322_12`, so a long prefix needs lower vmids. The park bridge is the prefix and then a word. The optional hookscript matches `sbx*`, so change it too if you change the prefix. |
| `[assistants]` | `risky` | Optional, off by default. Lets an assistant tear down sandboxes, revert, delete switches and type at a switch's console through the [MCP](#mcp) server. It only hides those tools; it does not lock the API. |

A mistake in the profile (an unknown key, a wrong type, a pool outside its
subnet) leaves the whole file out, so SimRack changes nothing, and the setup
page names the key: a typo in a protected list would otherwise leave something
live unprotected. A key from an older version counts as a mistake until the
page saves the profile again. Tokens never go in the profile.

### Tokens

The setup page's **Connect** part keeps the tokens in `state/tokens.json`
(mode 600), each with the one address it may be sent to. A new address needs
the token pasted again, and a saved token is never shown or sent anywhere else.

- **Proxmox.** The page shows `pveum` commands to run as root on the host. They
  make the Proxmox resource pool `simrack` and a `simrack@pve` token. SimRack's
  own role, just the VM privileges it checks, is granted only on that pool, and
  SimRack makes every guest in it, so Proxmox itself keeps the token off live
  guests. Everywhere else the token gets Proxmox's smallest built-in role:
  `PVEAuditor` to look at every guest, the node and `local`;
  `PVEDatastoreUser` on `local-lvm`; `PVESDNUser` on the local network zone,
  since sandbox bridges are named only when a sandbox is built. Each template
  on the node gets `PVETemplateUser`, which lets it be cloned. A build from a
  template made later is refused, naming the one command that grants it. A
  root token works too, but it may change anything on the host. The address
  defaults to the host itself, `https://127.0.0.1:8006/api2/json`; change it
  only when SimRack runs somewhere else.
- **Mist** (optional). Pick the cloud and paste an org API token. Super User or
  Network Admin lets SimRack build Mist sites; Observer only looks. A user
  token's role counts when it is held on the org, its MSP or an org group the
  org is in.

SimRack asks each token what it may do (Proxmox `GET /access/permissions`, Mist
`GET /self`, for the profile's org, plus `GET /orgs/{id}` when a role sits on an
MSP or org group) and offers only those changes. It keeps the answers for 30
seconds.

## The safety model

Everything the profile lists as live is refused by `simrack/guardrails.py`, and
the tests prove it:

- guests only in the sandbox range (320-399 by default); the profile's
  protected vmids and containers are hard-refused, even inside that range
- a clone source can never be a live guest, and must be a Proxmox template,
  made from a vJunos that has never booted (ADVICE.md, step 2)
- bridges must be `<prefix><vm>_<vm>_<ports>` (`sbx` by default); the profile's
  protected bridges are hard-refused, even when they carry the prefix
- the profile's Mist sites are hard-refused for writes
- a sandbox fabric never shares a subnet with the live lab. Its usual ranges are
  10.255.224.0/20 (underlay), 172.31.0.0/23 (router IDs) and 172.31.2.0/24
  (loopbacks); one that overlaps a protected subnet, the shape's networks or
  another fabric range steps down to the nearest free block its size, and the
  build says so in a note. The data and voice networks (10.60.10.0/24 and
  10.60.20.0/24 in the recipes) are the shape's choice and never move: a build
  that would put one on a protected subnet is refused before anything is saved
  or sent
- every fabric bridge is created at **MTU 9216** (1500 causes fabric-wide overlay
  BGP flaps) as a runtime Linux bridge (`ip link`), never through the PVE network
  API, so `/etc/network/interfaces` is never rewritten. They carry
  `group_fwd_mask 0xfff8`, and SimRack opens LACP on a switch's taps after
  every start it makes, because a start gives the guest new taps. A start from
  the Proxmox GUI keeps LACP only if the template carries the optional
  hookscript (see Deploy).
  While SimRack may change the lab, bridges a reboot removed are re-created when
  the service starts
- switches are made with only settings an API token may set. A clone copies the
  template's; a boot from an image gets `smbios1` product `VM-VEX` and
  `cpu: host`. SimRack never sets `args` or `hookscript`: Proxmox lets only
  `root@pam` set those
- boot images are limited to `<storage>:iso/*.iso` (CD-ROM on a blank disk) and
  `<storage>:import/*.qcow2|img|raw|vmdk` (imported onto a fresh disk); existing
  guest disks such as `vm-100-disk-0` are refused
- every async Proxmox task (clone, import, stop, delete, rollback) is waited on;
  running guests are stopped before delete; a teardown that cannot delete
  everything keeps the sandbox record and its Mist site so it can be retried
- a failed build step deletes only a guest that step created, never one already
  at that vmid; when Proxmox cannot list its guests, SimRack refuses rather than
  assume a vmid is free
- POSTs must be `application/json` from the same origin, and run one at a time
- memory, not a fixed count, decides how many switches fit. Each switch takes
  5 GB (Juniper's vJunos-switch minimum, one setting: `switch_mem_mb`) and the
  host keeps a 6 GB reserve, measured against the memory Proxmox reports as
  available. It is checked when building, adding a switch, starting a stopped
  switch, and reverting
- every switch gets the same ports: net0 is management, on the profile's
  management bridge, tagged with its `vlan` or untagged when the profile has
  none (only the guest's NIC is attached; the bridge itself is never changed),
  and net1–net10 are ge-0/0/0–9. A port with no cable is parked on the park
  bridge (`sbxpark` by default), a bridge with no uplink, with its link down. Ports past
  ge-0/0/9 are refused before Proxmox is asked
- management addresses are the first free one in the profile's pool across
  every sandbox, shown as "planned" until adoption reads the address DHCP gave
  fxp0. A full pool is refused before anything is built
- each sandbox has its own random root password in `state/secrets/` (folder
  700, file 600). It never appears in the state file or an API response except
  Reveal, and it is deleted when a teardown completes
- Mist snapshots are files in `state/mist-snapshots/<sandbox>/` (folder 700,
  files 600). Root passwords are taken out before saving, and so is every
  device CLI line that holds a secret; those lines are kept to read, never
  replayed. A revert puts the sandbox's own password back where one was, so a
  password set by hand in Mist comes back as the sandbox's. Snapshots are
  deleted when a teardown completes, even with `keep_mist`
- certificates are checked on every call to Mist, and to Proxmox unless its
  address is the host itself (loopback, where its self-signed certificate never
  leaves the machine). Where the network inspects TLS, Mist calls fail until
  the inspecting CA is in the host's trust store
- a token goes only to the address it was saved for: a request redirected
  anywhere else (the MCP server's to SimRack too) goes without it
- it changes nothing unless a profile is saved, it is not paused, and the
  token may make that change: the Proxmox token for the lab, a Mist token with
  Super User or Network Admin on the profile's org, its MSP or an org group it
  is in for Mist (no org, no Mist changes). No setting or environment variable
  overrides that
- tokens go only to the address saved beside them, over HTTPS, and a Mist
  token only to a Mist cloud
- shapes are local files in `state/shapes/`. Importing and deleting them works
  read-only and never calls Proxmox or Mist. Only the fabric's shape is kept
  (names, roles, pods, ports, AS numbers); addresses, subnets and port configs
  are dropped. Bodies are capped at 4 MB and shapes at 64 switches
- it binds 127.0.0.1 and refuses a public bind without `SIMRACK_TOKEN`. Without a
  token it answers only requests addressed to `127.0.0.1`, `localhost` or `::1`,
  so a web page that renames itself to that address (DNS rebinding) is refused.
  Once a token is set, every API call needs it, even on loopback

## Run it

```bash
# on the Proxmox host, from the project folder
python3 -m simrack serve          # http://127.0.0.1:8787; the setup page opens first
python3 -m simrack state          # the inventory as JSON; changes nothing
python3 -m simrack recipes        # the built-in fabric blueprints
python3 -m simrack mcp            # an MCP server for an assistant; see MCP
```

Environment, both optional. Everything else is set on the setup page.

| Variable | Meaning |
|---|---|
| `SIMRACK_STATE_DIR` | where the lab profile, the tokens, sandboxes, shapes and passwords are kept (default `/opt/simrack/state`). The unit lets SimRack write only there: point its `ReadWritePaths=` at a new folder too |
| `SIMRACK_TOKEN` | a bearer token for the page's API. Required to bind anything but 127.0.0.1; once set, every API call needs it, the MCP server's too |

The page follows the computer's light or dark setting. Day looks like the Mist
portal: navy side list, white top bar, light content. The sun or moon at the end
of the top bar switches theme and the browser remembers it; switching back to
the computer's own theme forgets the choice, so the page follows the computer
again.

## Import a fabric from Mist

Shapes → **Import**. Paste one to three Mist API responses back to back, or drop
them as saved files:

| Response | Needed | Gives |
|---|---|---|
| `GET /sites/<site>/evpn_topologies/<id>` | yes | switches, roles, pods and links (the list endpoint leaves the switches out) |
| `GET /sites/<site>/devices?type=switch` | no | names, so the plan shows which live switch each sandbox switch copies |
| `GET /sites/<site>/stats/ports/search?limit=1000&duration=1d` | no | LLDP neighbours, for the exact cabling; without them ports are guessed from the port config |

The page links all three for the first Mist site the profile protects, so a
browser signed in to Mist can open and save them; SimRack needs no Mist token
for this. Scripts can send the same thing as `POST /api/shapes` with
`{"documents": [topology, devices, ports]}`. Importing again under the same name
replaces the shape.

## Tests

One test file per requirement, against in-memory fakes, so they never touch a
lab. `tests/fixtures/lab-profile.toml` is the placeholder lab they protect.

```bash
python3 -m unittest discover -s tests -t . -v
```

| File | Case |
|---|---|
| `test_case1_mist_setup_mimic.py` | the front end mimics the Mist setup |
| `test_case2_cable_moves.py` | cable moves reach Proxmox |
| `test_case3_boot_images.py` | boot images |
| `test_case4_switch_power.py` | extra switches up and down |
| `test_case5_customer_isolation.py` | customers play without Proxmox edits |
| `test_case6_mist_revert.py` | the Mist revert button |
| `test_case7_teardown_redo_fix.py` | tear down, redo, and the runbook |
| `test_case8_review_fixes.py` | real client requests, read-only default, cabling, partial teardown, API origin/auth |
| `test_case9_shapes.py` | importing a Mist fabric as a read-only shape: LLDP cabling, guessed ports, sanitising, errors, HTTP |
| `test_case10_ui_files.py` | the page's files are served as files, without the token, and nothing else on disk is |
| `test_case11_memory_fit.py` | free memory is what Proxmox calls available; one switch size; memory, not a count, limits switches; starting and reverting check memory |
| `test_case12_build_from_shape.py` | building from a shape: switch ports and the park bridge, slices and left-out cables, the root password, the serial console, adopting into Mist, the HTTP routes and the Host check |
| `test_case13_drive.py` | building the fabric in Mist from the sandbox's cables, and checking the cabling three ways, fixing it only when asked |
| `test_case14_repo_hygiene.py` | the repository ships no real network's identifiers: placeholder UUIDs and MACs only |
| `test_case15_lab_profile.py` | the profile loads into the settings; a mistake leaves the whole file out and names the key |
| `test_case16_no_profile_read_only.py` | with no profile every write is refused, and the status says why |
| `test_case17_profile_guardrails.py` | the guardrails protect what the profile lists, and nothing is hard-coded |
| `test_case18_mgmt_pool.py` | management addresses come from the profile's pool; a full pool is refused; no vlan leaves fxp0 untagged |
| `test_case19_real_gear.py` | what real Proxmox and Mist require: only token-settable fields, LACP after each start, certificate checks, tokens that never follow a redirect, cleanup of only what a step created, template-only clones, private Mist snapshots, topologies reverted in full |
| `test_case20_token_decides.py` | the tokens decide what SimRack may change: Proxmox permissions on SimRack's pool, each template it may clone, the Mist role on the profile's org (held there, on its MSP or on an org group), no environment switch, and the pause |
| `test_case21_setup_page.py` | the setup page: what it finds on the host, saving, importing and exporting the profile, the tokens and the addresses they may go to, the token commands run against a stand-in host, and refusing a save that would strand a sandbox already built |
| `test_case22_mcp.py` | the MCP server: the handshake, tools offered by what SimRack may do, a first list that waits for SimRack to find out, the risky tick, Casper's change kinds, a cabling check that only looks, jobs that outlast a call and the MCP server, telling the assistant when the tools change, a token that stays with SimRack, the access check in Casper's contract, and the `--read-only` pin |
| `test_case23_jobs.py` | changes as jobs: `Prefer: respond-async` gets a job at once, `GET /api/jobs/{job}` waits for how it ended, SimRack's own words when it refuses, only changes become jobs, jobs taking their turn with changes from the page, the last 50 finished jobs kept, and a restart naming its jobs afresh |

## Deploy

SimRack lives at `/opt/simrack` on the Proxmox host and runs as the
`simrack` systemd unit, bound to 127.0.0.1:8787.

Optional, once per host: to keep LACP open when someone starts a switch from
the Proxmox GUI, put the hookscript on the template, as root:
`qm set <template> --hookscript local:snippets/simrack-sbx.sh`. Every clone
copies it. The deploy copies the script to `/var/lib/vz/snippets/`, and the
`local` storage must allow the `snippets` content type. SimRack itself never
sets a hookscript: Proxmox lets only `root@pam` do that, not an API token.

For the first deploy and every update:

```bash
SIMRACK_HOST=<ssh host> ./deploy/deploy.sh   # copies, runs the tests (a failure stops it), installs the unit, restarts, smoke tests
ssh -N -L 8787:127.0.0.1:8787 <ssh host>      # then open http://127.0.0.1:8787
```

On the first visit the setup page opens. Run the token commands it shows as
root on the host, paste the token it prints, check what the page ticked as live,
and save. The deploy never copies over `state/`, so the profile, the tokens and
the pause stay on the host across updates.

`deploy.sh` does not back up the copy already on the host; take one first
(`tar -czf /root/simrack.bak.tgz -C /opt simrack`). Read `ADVICE.md` before the
first build.

To listen beyond 127.0.0.1, put `SIMRACK_TOKEN=<long random string>` in
`/opt/simrack/simrack.env` (mode 600; the unit reads it when it exists), then
run `systemctl edit simrack` and give the unit an empty `ExecStart=` line
followed by its own `ExecStart=` with the new `--host`. The page asks for the
token once per browser tab and forgets it when the tab closes. An SSH tunnel is
still the better way in.

## MCP

`python3 -m simrack mcp` lets an assistant drive SimRack. It is a Model Context
Protocol server on stdin and stdout that calls SimRack's own web API, so the
service must be running. The assistant's host labels every tool as a look or a
change and asks before each change. For Casper, add this to
`~/.casper/mcp.json`, then type `/mcp connect simrack`:

```json
{"mcpServers": {"simrack": {"command": "ssh",
  "args": ["-T", "-o", "BatchMode=yes", "<ssh host>",
    "cd /opt/simrack && set -a && { [ ! -f simrack.env ] || . ./simrack.env; } && exec python3 -m simrack mcp"]}}}
```

Casper gives the server a small environment: if ssh needs your agent, add
`"env": {"SSH_AUTH_SOCK": "${SSH_AUTH_SOCK}"}`. The host reads `SIMRACK_TOKEN`
from `simrack.env` when there is one, so the token never sits in the
assistant's config.

| Option | Default | Meaning |
|---|---|---|
| `--url` | `http://127.0.0.1:8787` | where SimRack listens |
| `--wait` | 50 | seconds a tool waits for SimRack before it answers "Still running" with a job for `job_result`. Keep it under the assistant's own limit for one call: Casper's is 90 s (`"callTimeout"`) |
| `--poll` | 15 | seconds between looks at SimRack, to tell the assistant when the tools on offer change |
| `--read-only` | off | offer looks only and refuse every change, whatever SimRack allows. To let changes through again, take it out of `~/.casper/mcp.json` and reconnect |

The tools follow what SimRack may do right now, so a pause or a new token shows
within `--poll` seconds:

| Tools | Offered |
|---|---|
| `state`, `access_check`, `list_sandboxes`, `get_sandbox`, `list_recipes`, `list_shapes`, `check_cabling`, `job_result` | always |
| `mist_health`, `mist_save_point` | with a Mist token |
| `build_sandbox`, `build_from_shape`, `power_node`, `add_cable`, `move_cable`, `remove_cable`, `fix_cabling`, `save_point` | while SimRack may change the lab |
| `mist_create_site`, `mist_build_fabric`, `adopt_switch` | while it may change the lab and Mist |
| `tear_down`, `delete_node`, `revert_guests`, `console_command`, and `revert_mist` (which also needs Mist) | as above, and only when the setup page ticks **Assistants** (`[assistants] risky`) |

The **Assistants** tick decides only what the MCP server offers. It is not a
lock on SimRack's API: anything holding `SIMRACK_TOKEN`, or anything on the
host when no token is set, can still tear down through the API, and can change
the tick there too. Keep the token where the assistant cannot read it, and let
its host ask before each change.

Looks carry `readOnlyHint`: `check_cabling` only looks, and `fix_cabling` puts
back what drifted. Tools that reach Mist carry `openWorldHint`, the cabling
pair included, since both read LLDP there. Changes that throw work away carry
`destructiveHint`, and every change carries a kind in
`_meta["casper/change-kind"]`: `delete` for `tear_down`, `delete_node`,
`remove_cable` and `revert_mist`; `disruptive` for `power_node`,
`revert_guests` and `console_command`; `config` for the rest. In Casper,
writes start off (`/mcp writes simrack`), deletes stay off until
`/mcp allow simrack`, and disruptive changes ask every time. Changes run one at
a time, as they do from the page.

SimRack, not the MCP server, keeps each change as a job, so a timeout, a
restarted server or a new session loses nothing. The server sends a change with
`Prefer: respond-async`; SimRack answers `202` with the job at once and makes
the change in its turn. The server then asks `GET /api/jobs/{job}?wait=<seconds>`
(SimRack waits at most 60 s an ask) until the change ends or `--wait` runs out,
and then the tool answers "Still running" with the job. `job_result` asks
again, from this session or a new one. SimRack keeps the last 50 finished jobs
until it restarts; its page shows what was done either way. Scripts can do the
same.

With `--read-only` only the looks above are offered, so `mist_save_point` goes
too: it writes a file on the host. A change asked for anyway is refused.

`access_check` answers in Casper's `casper/access-check v2` (SimRack's
`GET /api/access`). For each token it says whether it may change things
(`read-write`), only look (`read-only`) or SimRack cannot tell (`unknown`),
and who it is. It reports the tokens, not SimRack: a pause does not change it,
and `--read-only` shows only as each product's `server_gate`. Proxmox counts
as read-only when the token lacks a privilege SimRack needs, and gives no role,
because Proxmox does not say which role granted the privileges. Mist lists in
`can_change` and `read_only` every org, site and site group the token reaches,
in any org, not only the profile's. A list Casper could not show whole, such
as one with an MSP in it, is left out.

## Agent skill

`skills/simrack/SKILL.md` is a short runbook for a coding agent: what to read
first, which tools change things (each marked `WRITE:`), and the traps. It
drives SimRack through the MCP server, uses the section layout of Casper's
network skills, stays under 6 KiB and points at this README and `ADVICE.md` for
detail, so keep the three in step. Install it by copying the folder:

```bash
mkdir -p ~/.casper/skills ~/.agents/skills
cp -R skills/simrack ~/.casper/skills/    # Casper
cp -R skills/simrack ~/.agents/skills/    # agents that read ~/.agents/skills
```

## Known gaps

Read `ADVICE.md` section 6. The important ones: nothing has been built on a
real host; Run and Drive are untested on real gear (Build fabric in Mist is
tested against a fake Mist only, and the serial login against scripted replies,
not a real vJunos console); and the setup page, the token checks and the MCP
server have not run on a real host. Watch the first build, adoption and fabric
build.

## License

MIT. See `LICENSE`.

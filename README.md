# LabFront

A sandbox front end for a **vJunos-switch + Juniper Mist lab on Proxmox VE**.

People can build, re-cable, break and rebuild a lab from a web page, without
touching Proxmox by hand and without any risk to the live lab. One file, the lab
profile, says what is live. LabFront refuses any action that would touch what it
lists, and without a profile it changes nothing.

Standard library only (Python 3.11 or later). No pip, no build step, nothing to
install on the host. The page is plain files in `labfront/static/`
(`index.html`, `app.css`, `app.js`, `theme.js`), served as they are.

## Status

LabFront grew in four stages. All four are covered by tests against in-memory
fakes; only part of it has run on a real host.

| Stage | What it adds | On a real host |
|---|---|---|
| Crawl | recipes, sandboxes, cables, power, snapshots, teardown, the safety model | read-only only: the inventory, memory and the protected lab. Nothing has been built with writes on. |
| Walk | importing a live Mist fabric as a shape | yes: a live fabric imported read-only, cabled from LLDP |
| Run | building a sandbox from a shape, adopting its switches into Mist over the serial console | **untested on real gear** |
| Drive | building the sandbox's fabric in Mist, checking the cabling three ways | **untested on real gear** |

The lab profile came last: this profile-driven version has not run on a real
host yet. Start read-only, and read `ADVICE.md` before you turn writes on.

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
| **Adopt into Mist** | Logs in over the switch's serial console, sets the sandbox's root password, turns on DHCP on fxp0 and enters the sandbox site's adoption commands. If Junos refuses a line, the change is rolled back. Needs writes, `MIST_TOKEN`, `LABFRONT_MIST_WRITES=1` and the sandbox's own Mist site. |
| **Join Mist** | A checklist on the sandbox page: the site, each switch's adoption, Build the fabric in Mist (with the values it sends, and Copy as text), then Check the cabling. **Reveal** shows the sandbox's root password; it works read-only and is never written to Activity. |

## The lab profile

One TOML file per host says what is live. Copy `lab-profile.example.toml`, fill
it in for your lab and point `LABFRONT_PROFILE` at it.

| Table | Keys | Meaning |
|---|---|---|
| `[proxmox]` | `node` (required), `api` | The node name, spelled exactly as Proxmox spells it: the API is case sensitive. Leave `api` at its default, the host itself: LabFront checks the certificate of any other address. |
| `[mist]` | `api`, `org_id` | Your org's API host (`api.mist.com`, `api.gc1.mist.com`, `api.eu.mist.com`…) and org. |
| `[management]` | `bridge`, `cidr`, `pool` (required), `vlan` | Where fxp0 goes. Leave `vlan` out when the management network is untagged. The pool must sit inside `cidr`. |
| `[protected]` | `vmids`, `lxc`, `bridges`, `mist_sites`, `subnets` | Everything live. LabFront refuses any action that would touch these. |
| `[sandbox]` | `vmids`, `lxc`, `bridge_prefix`, `park_bridge` | Optional. The defaults are 320-399, 350-399, `sbx` and `sbxpark`. The optional hookscript matches `sbx*`, so change it too if you change the prefix. |

A mistake in the profile (an unknown key, a wrong type, a pool outside its
subnet) stops start-up and names the key, because a typo in a protected list
would otherwise leave something live unprotected. Without a profile LabFront
starts read-only and the page says why. Tokens never go in the profile.

## The safety model

Everything the profile lists as live is refused by `labfront/guardrails.py`, and
the tests prove it:

- guests only in the sandbox range (320-399 by default); the profile's
  protected vmids and containers are hard-refused, even inside that range
- a clone source can never be a live guest, and must be a Proxmox template,
  made from a vJunos that has never booted (ADVICE.md, step 2)
- bridges must be `<prefix><vm>_<vm>_<ports>` (`sbx` by default); the profile's
  protected bridges are hard-refused, even when they carry the prefix
- the profile's Mist sites are hard-refused for writes
- a sandbox fabric never shares a subnet with the live lab. Sandbox fabrics use
  fixed ranges: 10.255.224.0/20 (underlay), 172.31.0.0/23 (router IDs),
  172.31.2.0/24 (loopbacks), and 10.60.10.0/24 and 10.60.20.0/24 (data and
  voice). A fabric build that would overlap a protected subnet is refused before
  anything is saved or sent, so a profile that protects any of these ranges
  blocks fabric builds
- every fabric bridge is created at **MTU 9216** (1500 causes fabric-wide overlay
  BGP flaps) as a runtime Linux bridge (`ip link`), never through the PVE network
  API, so `/etc/network/interfaces` is never rewritten. They carry
  `group_fwd_mask 0xfff8`, and LabFront opens LACP on a switch's taps after
  every start it makes, because a start gives the guest new taps. A start from
  the Proxmox GUI keeps LACP only if the template carries the optional
  hookscript (see Deploy).
  With writes on, bridges a reboot removed are re-created when the service starts
- switches are made with only settings an API token may set. A clone copies the
  template's; a boot from an image gets `smbios1` product `VM-VEX` and
  `cpu: host`. LabFront never sets `args` or `hookscript`: Proxmox lets only
  `root@pam` set those
- boot images are limited to `<storage>:iso/*.iso` (CD-ROM on a blank disk) and
  `<storage>:import/*.qcow2|img|raw|vmdk` (imported onto a fresh disk); existing
  guest disks such as `vm-100-disk-0` are refused
- every async Proxmox task (clone, import, stop, delete, rollback) is waited on;
  running guests are stopped before delete; a teardown that cannot delete
  everything keeps the sandbox record and its Mist site so it can be retried
- a failed build step deletes only a guest that step created, never one already
  at that vmid; when Proxmox cannot list its guests, LabFront refuses rather than
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
  bridge (`sbxpark`), a bridge with no uplink, with its link down. Ports past
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
- certificates are checked on every call to Mist, and to Proxmox unless `api`
  is the host itself (loopback, where its self-signed certificate never leaves
  the machine). Where the network inspects TLS, Mist calls fail until the
  inspecting CA is in the host's trust store
- it is **read-only unless** a profile is loaded and `LABFRONT_ALLOW_WRITES=1`
- shapes are local files in `state/shapes/`. Importing and deleting them works
  read-only and never calls Proxmox or Mist. Only the fabric's shape is kept
  (names, roles, pods, ports, AS numbers); addresses, subnets and port configs
  are dropped. Bodies are capped at 4 MB and shapes at 64 switches
- it binds 127.0.0.1 and refuses a public bind without a token. Without a
  token it answers only requests addressed to `127.0.0.1`, `localhost` or `::1`,
  so a web page that renames itself to that address (DNS rebinding) is refused

## Run it

```bash
# on the Proxmox host, from the project folder
export LABFRONT_PROFILE=/opt/labfront/lab-profile.toml
python3 -m labfront state          # read-only inventory
python3 -m labfront recipes        # the built-in fabric blueprints
python3 -m labfront serve          # http://127.0.0.1:8787
```

Environment:

| Variable | Meaning |
|---|---|
| `LABFRONT_PROFILE` | the lab profile; without it LabFront is read-only |
| `LABFRONT_PVE_TOKEN` | `root@pam!labfront=<secret>` |
| `MIST_TOKEN` | a Mist API token with admin on the profile's org; without it every Mist action is disabled |
| `LABFRONT_MIST_WRITES` | `1` to let the front end push fabric changes to Mist (needs a profile) |
| `LABFRONT_ALLOW_WRITES` | `1` to allow changes (needs a profile); anything else, or unset, is read-only |
| `LABFRONT_TOKEN` | bearer token; required if binding anything but 127.0.0.1 |
| `LABFRONT_STATE_DIR` | where sandboxes, shapes and passwords are kept (default `/opt/labfront/state`) |

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
browser signed in to Mist can open and save them; LabFront needs no Mist token
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
| `test_case13_drive.py` | building the fabric in Mist from the sandbox's cables, and checking the cabling three ways |
| `test_case14_repo_hygiene.py` | the repository ships no real network's identifiers: placeholder UUIDs and MACs only |
| `test_case15_lab_profile.py` | the profile loads into the settings; a mistake stops start-up and names the key |
| `test_case16_no_profile_read_only.py` | with no profile every write is refused, and the status says why |
| `test_case17_profile_guardrails.py` | the guardrails protect what the profile lists, and nothing is hard-coded |
| `test_case18_mgmt_pool.py` | management addresses come from the profile's pool; a full pool is refused; no vlan leaves fxp0 untagged |
| `test_case19_real_gear.py` | what real Proxmox and Mist require: only token-settable fields, LACP after each start, certificate checks, cleanup of only what a step created, template-only clones, private Mist snapshots, topologies reverted in full |

## Deploy

LabFront lives at `/opt/labfront` on the Proxmox host and runs as the
`labfront` systemd unit. Once per host, before the first deploy:

1. Create a Proxmox API token: `pveum user token add root@pam labfront --privsep 0`.
   It prints the secret once.
2. Write the lab profile. Keep it as `lab-profile.toml` in the project root (git
   ignores it and the deploy copies it), or create
   `/opt/labfront/lab-profile.toml` on the host.
3. Create `/opt/labfront/labfront.env` on the host, mode 600. The service does
   not start without it:

   ```sh
   LABFRONT_PROFILE=/opt/labfront/lab-profile.toml
   LABFRONT_PVE_TOKEN=root@pam!labfront=<secret>
   LABFRONT_ALLOW_WRITES=0
   # MIST_TOKEN=<org admin token>
   # LABFRONT_MIST_WRITES=0
   ```

4. Optional: to keep LACP open when someone starts a switch from the Proxmox
   GUI, put the hookscript on the template once, as root:
   `qm set <template> --hookscript local:snippets/labfront-sbx.sh`. Every clone
   copies it. The deploy copies the script to `/var/lib/vz/snippets/`, and the
   `local` storage must allow the `snippets` content type. LabFront itself never
   sets a hookscript: Proxmox lets only `root@pam` do that, not an API token.

Then, for the first deploy and every update:

```bash
LABFRONT_HOST=<ssh host> ./deploy/deploy.sh   # copies, runs the tests (a failure stops it), installs the unit, restarts, smoke tests
ssh -N -L 8787:127.0.0.1:8787 <ssh host>      # then open http://127.0.0.1:8787
```

`deploy.sh` does not back up the copy already on the host; take one first
(`tar -czf /root/labfront.bak.tgz -C /opt labfront`). It ships **read-only**
(`LABFRONT_ALLOW_WRITES=0`); flip that only when you are ready, and read
`ADVICE.md` first.

```bash
systemctl status labfront
systemctl edit labfront        # override the env if you want writes
```

## Agent skill

`skills/labfront/SKILL.md` is a short runbook for a coding agent: what to read
first, which calls change things (each marked `WRITE:`), and the traps. It uses
the section layout of Casper's network skills, stays under 6 KiB and points at
this README and `ADVICE.md` for detail, so keep the three in step. Install it by
copying the folder:

```bash
mkdir -p ~/.casper/skills ~/.agents/skills
cp -R skills/labfront ~/.casper/skills/    # Casper
cp -R skills/labfront ~/.agents/skills/    # agents that read ~/.agents/skills
```

## Known gaps

Read `ADVICE.md` section 6. The important ones: nothing has been built on a
real host with writes on; Run and Drive are untested on real gear (Build fabric
in Mist is tested against a fake Mist only, and the serial login against
scripted replies, not a real vJunos console); and the profile-driven version
has not run on a real host. Watch the first build, adoption and fabric build.

# ADVICE: running, tearing down, rebuilding and fixing a vJunos + Mist lab

For anyone running SimRack on a Proxmox VE host with vJunos-switch guests and a
Juniper Mist org. It was learned on one real lab; names, numbers and addresses
here are examples.

The short version: **stop rebuilding the live lab. Build sandboxes instead.**
Everything in this document follows from that.

---

## 1. What to build, in order

| Step | Why | Effort |
|---|---|---|
| 1. Set SimRack up on its setup page: its Proxmox token, then what is live | SimRack refuses anything ticked as live, and changes nothing until the page is saved. | 30 min |
| 2. Make a clean vJunos **template** in the sandbox range (320 by default) | Every sandbox clones from it. Never clone a booted vJunos. | 1 session |
| 3. Prove two clones get **different serials** | Mist keys on serial. If they collide, the whole plan stops here. | 30 min, do it before anything else |
| 4. Look before you change | Confirm the page lists your live lab as protected. Anything that undoes, removes or stops something asks first, and **Pause** stops every change. | 10 min |
| 5. One sandbox, `single-switch` recipe | Cheapest proof (~5 GB). | 30 min |
| 6. `collapsed-core`, then `ip-clos` | The real thing. | 1 hour |
| 7. Mist site per sandbox + revert button | The part people actually break. | 1 hour |

**Do step 3 before step 2's result is trusted.** Everything else is reversible; a
lab full of switches that all report the same serial is not.

Every change is tested against fakes, not yet on real gear, so expect step 5
onward to find something. Watch each first run.

### Step 1 in detail: the setup page

Deploy and open the page (README, Deploy); the setup page opens by itself. In
**Connect**, run the token commands it shows as root on the host, and paste the
token the last one prints. A Mist token is optional: Observer only looks, Super
User or Network Admin lets SimRack build Mist sites. Then, in **The lab**, check
that everything live is ticked:

- every live guest and container. Templates are left out: SimRack only clones
  them
- every bridge the live lab uses. Bridges defined in
  `/etc/network/interfaces.d/` are invisible to the Proxmox network API (see
  section 7), so type them into **Also protect**
- every Mist site that is not a sandbox, by site ID (listed with a Mist token)
- every subnet the live fabric routes. The page finds the subnets on the host's
  bridges; type in the ones only the fabric routes

Check the node name with `pvesh get /nodes`: case matters. Leave the VLAN blank
if the management network is untagged. The pool is where sandbox switches are
planned; adoption records the address DHCP actually gives fxp0. Save, and
**Export** a copy to keep.

### Step 2 in detail: build the template

Do not convert a live switch into a template: that would freeze the live lab.
And never start the new VM before it is a template: vJunos may write its serial
and MACs to disk on first boot, and every clone would copy them. Instead:

1. Create a new VM in the sandbox range (vmid **320**, 4 cores, 5120 MB) with
   `args: -machine accel=kvm:tcg -smbios type=1,product=VM-VEX -cpu host,kvm=on`
   and `serial0: socket`.
2. Import the vJunos-switch qcow2 as its disk
   (`qm disk import 320 vJunos-switch-<version>.qcow2 <storage>`), attach it as
   virtio0 and make it the boot disk.
3. It needs no NICs of its own: SimRack gives every clone fxp0 (net0) on the
   profile's management network and all its switch ports.
4. Optional, for starts from the Proxmox GUI:
   `qm set 320 --hookscript local:snippets/simrack-sbx.sh`. SimRack opens
   LACP itself after every start it makes; a GUI start needs the hookscript.
5. `qm template 320`, without starting it first. Each clone then has its own
   first boot, and Adopt sets its root password.

Do this as root on the host. Proxmox lets only `root@pam` set `args` and
`hookscript`, so SimRack's API token never sets them; every clone copies them
from the template, and gets a fresh `smbios1` uuid.

If 320 was still a plain VM when the setup page was first saved, the page
ticked it as live and moved the sandbox ranges past it, so every build refuses
it as a clone source. Untick it, set the ranges back if you like, and save.

### Step 3 in detail: the serial question

Each vJunos switch reports a serial, and Mist keys on it. Every clone gets a
fresh `smbios1` uuid and has its own first boot, so the clones should differ
whether vJunos takes the serial from SMBIOS or makes one on first boot. If they
are duplicates, Mist will refuse or silently merge them.

Test: clone 320 twice (321, 322), boot each on its own management IP, and
compare `show chassis hardware` (the Chassis line) on the two. Ten minutes, and
it decides whether the multi-switch sandbox works at all.

If the serials collide, the image itself fixes the serial. Booting from the
image (section 7) will not help: it starts from the same pristine disk. Stop
building more switches from that image.

---

## 2. Tearing the lab down

**Tear down a sandbox, not the lab.** The live lab (everything ticked as live on
the setup page) has no supported teardown. It is your reference build and the
thing people are shown.

In the front end: open the sandbox, **Tear down**. That deletes the sandbox
guests, its bridges and its Mist site, and leaves the template and the live lab
alone. Behind the scenes it is the same as:

```bash
# what teardown does, so you can do it by hand if the UI is down
qm stop <vmid>; qm destroy <vmid> --purge
ip link del sbx<vm>_<vm>_<ports>          # one per cable
# delete the sandbox Mist site in the UI
```

If you must reset the host networking after a bad cable change:

```bash
ifreload -a                              # apply /etc/network/interfaces
ip -br addr | grep sbx                    # sandbox bridges should be gone
```

**Never** run `ifreload -a` while the live lab is mid-change without a known-good
backup of `/etc/network/interfaces` (and anything it sources). The management
bridge carries management; a bad reload locks you out of the box.

---

## 3. Redoing the lab

Two different meanings, two different answers.

### Redo a *sandbox* (the common case)
Tear it down and build it again from the same recipe. The recipe is the source
of truth: subnets, VLANs, VRF, overlay AS and the cabling are all recorded in
`simrack/recipes.py`, so a rebuild is deterministic. Fresh vmids, fresh Mist
site, no leftovers.

### Redo the *live lab* (rare, do it deliberately)
You need two independent restore paths, and you should know both:

1. **Mist side:** before anything risky, save the live site's setting, its EVPN
   topology and every switch's device config. These are the same three things
   **Snapshot Mist** saves for a sandbox; SimRack does it for sandboxes only.
   Mist also rolls a switch back by itself when a push cuts the switch off from
   the cloud. That does not cover a push that keeps the switch connected but
   breaks the fabric.
2. **Proxmox side:** `qm snapshot <vmid> <name>` before anything risky, then
   `qm rollback <vmid> <name>`. Mist state lives in the cloud, so a disk rollback
   does **not** restore Mist; you need both.

Order matters: roll back the guests first, then push the Mist config, then verify.

---

## 4. Fixing things: symptom to action

| Symptom | Likely cause | Action |
|---|---|---|
| Fabric-wide overlay BGP flaps every ~90 s | A fabric bridge is MTU 1500 | `ip link show <bridge>`: every fabric bridge must be MTU 9216. New bridges from SimRack are 9216 by default. |
| One border's BFD stuck Init, TTL 254 | A VLAN in the EVPN overlay hairpinning back to the other border | An L2 loop. One link, one bridge, never a shared transit VLAN. |
| Billions of packets out of a router's tap, millions of input errors | L2 loop on the transit bridge | `bridge fdb show br <bridge>`: two border MACs on one port means a loop. |
| Windows client NAKs a DHCP lease | The relay's loopback (giaddr) matches no scope | Add a scope for the loopback subnet, with every address excluded. |
| Switch set static OOB and lost the cloud | `oob_ip_config` static **without** `use_mgmt_vrf: true` | `rollback 1; commit` on the switch, or wait for Mist's automatic rollback. |
| Mist pushed WAN Edge config and broke OSPF | Config management was enabled on a device with an empty template | Keep config management **off** for a vSRX run from the CLI. Mist WAN Edge does not fit a CLI OSPF/BGP design. |
| GBP tags render, counters stay 0 | vJunos does not enforce VXLAN GBP (the feature is unlicensed on vJunos) | Not a bug. Use a physical EX for a real GBP demo. |
| A sandbox switch will not join the site | Serial collision, or it was never adopted | Compare serials on every clone (step 3 above), then click **Adopt again** in the sandbox's Join Mist checklist. |
| A leaf refuses Mist's commit: "ovsdb, multicast-group, ingress-node-replication cannot be configured together" | No port on that switch uses the networks, so Mist builds no EVPN instance and the default VLAN's VNI lands outside one | **Build again** in Join Mist: it gives the switch a `clients` trunk on its last free port. By hand, give any port a usage that carries the networks. |
| `SW_CONFIG_FAILED` in Mist's events straight after **Build fabric**, yet Health says `COMMITED` a minute later | Mist pushes twice: at once when the site setting changes, before the device configs land, and again when its EVPN topology job ends, about a minute later | Judge the second push: wait about 90 s, then read Health. Each switch's commit list in Mist's device stats names the version that took. |
| "SimRack is read-only: no lab profile is loaded" | The setup page has not been saved, or the saved profile has a mistake (a key an older version used counts) | Open **Setup**. It names any mistake; fix it and save. |
| "SimRack is read-only: the Proxmox token may not change the lab" | The token lacks a privilege; the detail names each one and where | Run the setup page's token commands again, or grant what is named. SimRack asks Proxmox again within 30 s. |
| "Changes are paused" | Someone pressed **Pause**; it lasts across restarts | **Resume** in the top bar. |
| Every Proxmox call fails with HTTP 596 "certificate verify failed" | The profile's node name has the wrong case | Copy the name from `pvesh get /nodes`. See section 7. |
| Proxmox calls fail with "certificate verify failed", and the node name is right | The Proxmox address beside the token is not the host itself, and its certificate is self-signed | When SimRack runs on the host, set the address back to `https://127.0.0.1:8006/api2/json` and paste the token again. |
| Mist calls fail with "certificate verify failed" | The network inspects TLS | Add the inspecting CA to the host's trust store (`/usr/local/share/ca-certificates/`, then `update-ca-certificates`). Test with `curl -sS https://api.mist.com/api/v1/`, without `-k`. |
| Build refused: "vmid … is not a template" | The clone source is a plain VM | Make a clean template (step 2). Never template a booted vJunos. |
| LACP down on a switch started from the Proxmox GUI | A start gives the guest new taps, and the template has no hookscript | Start it from SimRack, or add the hookscript to the template (step 2). |
| Build fabric refused: "overlaps the protected subnet" | A shape network (data, voice) overlaps a subnet the profile protects; fabric ranges step clear on their own | Move that network in the shape, or take the subnet out of the profile if it is not live. |
| "The management pool … is full" | Every pool address is planned for a switch | Tear down a sandbox, or widen the management pool on the setup page. |
| "Mist changes are off: its role on this org is read" (or "it has no role on this org") | The Mist token is an Observer's, or belongs to another org | Expected for an Observer. To build Mist sites, paste a Super User or Network Admin token for the profile's org on the setup page. |
| Front end says "Not enough free memory" | Under the 6 GB reserve after the build | Stop a sandbox switch, or build the `single-switch` recipe. Do not balloon vJunos. |

### The health check after any change

On each switch, `show bgp summary`: every fabric peer Established. Then, from a
test client, ping something beyond the border. Zero established peers on the
border router means the border path is broken, not Mist.

---

## 5. Hard-won rules worth keeping

1. **Fabric bridges are MTU 9216.** No exceptions.
2. **One cable, one bridge, one /31.** Never a shared transit VLAN between
   borders and a WAN router: that is the loop.
3. **Never clone a booted vJunos.** It may bake its serial and MACs in on first
   boot.
4. **Never balloon or overcommit vJunos memory.** It goes unstable, quietly.
5. **Management stays out-of-band and static** (`use_mgmt_vrf: true`), so a
   broken fabric never locks you out. The Proxmox serial console
   (`qm terminal <vmid>`) is the last resort.
6. **One Mist site per sandbox.** Two people on one topology overwrite each
   other; that is not a bug you can config away.
7. **Snapshot before you experiment**, in both places. The revert button is only
   as good as the last snapshot.
8. **When the live lab changes, open Setup.** It marks guests found since as
   **new**; a new guest, bridge, site or subnet is unprotected until it is
   ticked and saved.

## 6. Honest gaps in this build

- One real host so far: a three-switch IP Clos slice (border, core, access) of
  an imported shape, on vJunos-switch 26.2R1.7 and a Mist cloud org. Build
  from shape, Adopt, Build fabric in Mist, Check cabling and Health ran there:
  every switch committed Mist's config, and underlay and EVPN overlay BGP came
  up. Revert Mist ran there once (a port description, Oct 2026) and the Junos
  came back clean. Tear down and Revert Proxmox have not run on real gear.
- The setup page and its token commands worked on that host; the MCP server has
  not run on a real host. Still check that the page lists your live lab as
  protected before the first build.
- Sandbox bridges are runtime-only. The service re-creates missing ones at
  start-up (while SimRack may change the lab), so start the service before
  starting sandbox guests after a host reboot.
- vmids for sandbox LXC clients (350-399 by default) share the switch range.
- Adoption is driven from the page (Join Mist → Adopt) over the serial console.
  It adopted three vJunos switches on the real host once a `delete` line that
  Junos refuses no longer stopped it (Mist's adoption commands delete things
  that may not be there). The serial socket takes one client at a time: close
  any open `qm terminal` first.
- The first live fabric build taught three things, now built in. Mist answers
  200 to a topology but ignores its `switch_configs`, so each switch's config
  is merged onto its device instead. Mist builds a switch's EVPN instance,
  VLANs and IRBs only for networks some port uses, so each switch that holds
  the networks gets a `clients` trunk on its last free port (section 4 has the
  error without it). Device rows carry no status, so Health reads Mist's device
  stats. Port stats did carry each switch's MAC.
- Collapsed-core and ESI-LAG fabrics have not been built in Mist on real gear.
  Watch the first of each; if it goes wrong, revert Mist to its
  `before-fabric-…` point and fix from there.
- No client traffic has crossed a built fabric. No recipe makes client
  containers yet, and the `clients` port has no cable, so it stays parked and
  down; the commit does not need it up.
- Tear down deletes the sandbox's Mist site, but its switches stay in the org's
  inventory, unassigned and disconnected. Release them there (Organization →
  Inventory) so they do not pile up.
- A switch booted from an image gets `smbios1` product `VM-VEX` and `cpu: host`
  instead of the root-only `args` line in step 2. Whether vJunos runs that way
  is unverified: check the first one reaches the Junos prompt.
- Revert Mist puts each topology back exactly as Mist returned it. Whether Mist
  accepts its own computed fields back is unverified; if it refuses, revert
  sends members and roles, as the fabric build does, and says so in the notes.
- Mist's PUT keeps any top-level field it is not sent (seen on a switch; the
  site setting is assumed to work the same way). The first real revert left nine
  fields the Mist page had added on save, all empty. Revert now sends a setting
  made since as `{}`, `[]` or `""`, after a 400 sends it again without the `""`
  ones, then as the snapshot alone. It leaves a yes/no or a number and names it
  in the notes, and never empties the root password or whether Mist manages a
  switch. Clearing a non-empty one has not run on real gear.
- Check cabling assumes net1 is ge-0/0/0. LLDP confirmed it on vJunos-switch
  26.2R1.7; another image may map differently. When LLDP disagrees on a cable
  that Proxmox has right, it says so; that mapping is the first thing to check.
- The front end has no authentication of its own beyond a bearer token. It binds
  127.0.0.1 by default; use an SSH tunnel, not a public bind.
- The setup page's Proxmox token may change only guests in the Proxmox resource
  pool `simrack`, where SimRack makes every guest, so Proxmox itself refuses it
  a live guest. It may still put a sandbox NIC on any bridge in the local
  network zone, since sandbox bridges are named only when a sandbox is built:
  SimRack's guardrails, not Proxmox, keep sandbox NICs off live bridges. The
  service itself runs as root: it makes bridges with `ip link` and opens the
  serial console.

## 7. Host gotchas

**1. The node name is case sensitive.** If the node is `PVE-01`, asking for
`pve-01` returns HTTP 596 with `certificate verify failed` on every
`/nodes/pve-01/...` call, while `/nodes` itself works. The error sends you
looking at certificates when the real problem is the name. Copy the setup
page's Proxmox node from `pvesh get /nodes`.

**2. Bridges in sourced files are invisible to the PVE network API.** Bridges
defined in a file under `/etc/network/interfaces.d/` are not read by PVE, so
`GET /nodes/<node>/network` never lists them, and the setup page cannot find
them. The only thing stopping SimRack from creating a bridge with the same name
is a protected bridge: type every live one into the setup page's **Also
protect**, and add new ones as the lab grows.

SimRack therefore never uses the PVE network API for bridges: applying it ends
in an `ifreload` that rewrites `/etc/network/interfaces`. Sandbox bridges are made
with `ip link` instead and are never written to any file.

**Image boots need no template.** `image=local:import/vJunos-switch-<version>.qcow2`
imports a pristine disk per switch, the same disk a clone of a never-booted
template starts from. Put the qcow2 in `/var/lib/vz/import/` first.

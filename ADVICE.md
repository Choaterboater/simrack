# ADVICE: running, tearing down, rebuilding and fixing a vJunos + Mist lab

For anyone running LabFront on a Proxmox VE host with vJunos-switch guests and a
Juniper Mist org. It was learned on one real lab; names, numbers and addresses
here are examples.

The short version: **stop rebuilding the live lab. Build sandboxes instead.**
Everything in this document follows from that.

---

## 1. What to build, in order

| Step | Why | Effort |
|---|---|---|
| 1. Write the lab profile | LabFront refuses anything it lists, and changes nothing without it. | 30 min |
| 2. Make a clean vJunos **template** in the sandbox range (320 by default) | Every sandbox clones from it. Never clone a booted vJunos. | 1 session |
| 3. Prove two clones get **different serials** | Mist keys on serial. If they collide, the whole plan stops here. | 30 min, do it before anything else |
| 4. Create the Proxmox API token for LabFront | Without it LabFront cannot create guests. | 10 min |
| 5. Run LabFront read-only first | Confirm the page lists your live lab as protected. | 10 min |
| 6. One sandbox, `single-switch` recipe | Cheapest proof (~5 GB). | 30 min |
| 7. `collapsed-core`, then `ip-clos` | The real thing. | 1 hour |
| 8. Mist site per sandbox + revert button | The part people actually break. | 1 hour |

**Do step 3 before step 2's result is trusted.** Everything else is reversible; a
lab full of switches that all report the same serial is not.

Every write path is tested against fakes, not yet on real gear, so expect step 6
onward to find something. Watch each first run.

### Step 1 in detail: the lab profile

Copy `lab-profile.example.toml` and list everything live:

- every live guest under `vmids` and every live container under `lxc`
- every bridge the live lab uses, including ones defined in
  `/etc/network/interfaces.d/` (the Proxmox network API cannot see those; see
  section 7)
- every Mist site that is not a sandbox, by site ID
- every subnet the live fabric routes

Check the node name with `pvesh get /nodes`: case matters. Leave `vlan` out of
`[management]` if the management network is untagged. The pool is where sandbox
switches are planned; adoption records the address DHCP actually gives fxp0.

### Step 2 in detail: build the template

Do not convert a live switch into a template: that would freeze the live lab.
Instead:

1. Create a new VM in the sandbox range (vmid **320**, 4 cores, 5120 MB) with
   `args: -machine accel=kvm:tcg -smbios type=1,product=VM-VEX -cpu host,kvm=on`
   and `serial0: socket`.
2. Import the vJunos-switch qcow2 as its disk
   (`qm disk import 320 vJunos-switch-<version>.qcow2 <storage>`), attach it as
   virtio0 and boot from it.
3. Boot it with **no fabric NICs**: one NIC on the management bridge (with its
   VLAN tag, if any) if you want console access. Answer the first-boot questions
   in the console.
4. Record the serial: `show chassis hardware` (the Chassis line).
5. `qm template 320`.

The first boot is what bakes the serial and MAC. That is the whole reason for
this procedure.

### Step 3 in detail: the serial question

Each vJunos switch reports a serial. Proxmox gives every clone a fresh `smbios1`
uuid, so *if* vJunos derives the serial from SMBIOS the clones will differ and
everything works. If it bakes the serial into the disk image, every clone will be
a duplicate and Mist will refuse or silently merge them.

Test: clone 320 twice (321, 322), boot each on its own management IP, and
compare `show chassis hardware` on all three. Ten minutes, and it decides
whether the multi-switch sandbox works at all.

If the serials collide, the fallback is **one full disk copy per switch** rather
than clones: `qemu-img convert` a pristine image per switch, or run the first
boot per switch. Slower and disk-hungry, but it works.

---

## 2. Tearing the lab down

**Tear down a sandbox, not the lab.** The live lab (everything in the profile's
`[protected]` table) has no supported teardown. It is your reference build and
the thing people are shown.

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
`labfront/recipes.py`, so a rebuild is deterministic. Fresh vmids, fresh Mist
site, no leftovers.

### Redo the *live lab* (rare, do it deliberately)
You need two independent restore paths, and you should know both:

1. **Mist side:** before anything risky, save the live site's setting, its EVPN
   topology and every switch's device config. These are the same three things
   **Snapshot Mist** saves for a sandbox; LabFront does it for sandboxes only.
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
| Fabric-wide overlay BGP flaps every ~90 s | A fabric bridge is MTU 1500 | `ip link show <bridge>`: every fabric bridge must be MTU 9216. New bridges from LabFront are 9216 by default. |
| One border's BFD stuck Init, TTL 254 | A VLAN in the EVPN overlay hairpinning back to the other border | An L2 loop. One link, one bridge, never a shared transit VLAN. |
| Billions of packets out of a router's tap, millions of input errors | L2 loop on the transit bridge | `bridge fdb show br <bridge>`: two border MACs on one port means a loop. |
| Windows client NAKs a DHCP lease | The relay's loopback (giaddr) matches no scope | Add a scope for the loopback subnet, with every address excluded. |
| Switch set static OOB and lost the cloud | `oob_ip_config` static **without** `use_mgmt_vrf: true` | `rollback 1; commit` on the switch, or wait for Mist's automatic rollback. |
| Mist pushed WAN Edge config and broke OSPF | Config management was enabled on a device with an empty template | Keep config management **off** for a vSRX run from the CLI. Mist WAN Edge does not fit a CLI OSPF/BGP design. |
| GBP tags render, counters stay 0 | vJunos does not enforce VXLAN GBP (the feature is unlicensed on vJunos) | Not a bug. Use a physical EX for a real GBP demo. |
| A sandbox switch will not join the site | Serial collision, or it was never adopted | Compare serials on every clone (step 3 above), then click **Adopt again** in the sandbox's Join Mist checklist. |
| Page says "no lab profile is loaded" | `LABFRONT_PROFILE` is unset | Set it in `labfront.env` and restart. |
| Start-up fails naming a profile key | A mistake in the profile | Fix that key. LabFront will not start on a profile it cannot trust. |
| Every Proxmox call fails with HTTP 596 "certificate verify failed" | The profile's node name has the wrong case | Copy the name from `pvesh get /nodes`. See section 7. |
| Build fabric refused: "overlaps the protected subnet" | A sandbox range overlaps a subnet the profile protects | The sandbox ranges are fixed (see README.md); free them in the live lab, or build the fabric elsewhere. |
| "The management pool … is full" | Every pool address is planned for a switch | Tear down a sandbox, or widen `[management] pool`. |
| Front end says "Mist writes are disabled" | `LABFRONT_MIST_WRITES` is not 1, or no profile is loaded | Expected. Take a snapshot, then set it. |
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
3. **Never clone a booted vJunos.** Serial and MAC are baked on first boot.
4. **Never balloon or overcommit vJunos memory.** It goes unstable, quietly.
5. **Management stays out-of-band and static** (`use_mgmt_vrf: true`), so a
   broken fabric never locks you out. The Proxmox serial console
   (`qm terminal <vmid>`) is the last resort.
6. **One Mist site per sandbox.** Two people on one topology overwrite each
   other; that is not a bug you can config away.
7. **Snapshot before you experiment**, in both places. The revert button is only
   as good as the last snapshot.
8. **When the live lab changes, change the profile.** A new guest, bridge, site
   or subnet is unprotected until the profile lists it.

## 6. Honest gaps in this build

- Nothing has been built on a real host with writes on. Every write path,
  from Build sandbox to Tear down, is tested against fakes only.
- The profile-driven version has not run on a real host yet. Check that the
  page lists your live lab as protected before turning writes on.
- Sandbox bridges are runtime-only. The service re-creates missing ones at
  start-up (writes on), so start the service before starting sandbox guests
  after a host reboot.
- vmids for sandbox LXC clients (350-399 by default) share the switch range.
- Adoption is driven from the page (Join Mist → Adopt) over the serial console.
  The console script is tested against scripted Junos replies only, so watch
  the first real adoption. The serial socket takes one client at a time: close
  any open `qm terminal` first.
- Build fabric in Mist is tested against a fake Mist only. Until the first live
  build, these are unverified: whether Mist accepts the topology's
  `switch_configs` and the IRBs, how it represents collapsed-core and ESI-LAG
  links, whether a device PUT replaces or merges, and whether port stats carry
  each switch's MAC. Watch the first build; if it goes wrong, revert Mist to
  its `before-fabric-…` point and fix from there.
- Check cabling assumes net1 is ge-0/0/0. When LLDP disagrees on a cable that
  Proxmox has right, it says so; that mapping is the first thing to check.
- The front end has no authentication of its own beyond a bearer token. It binds
  127.0.0.1 by default; use an SSH tunnel, not a public bind.
- The Proxmox API token is a **root** token with `privsep=0`, so it carries full
  root privileges. That matches the fact that the service runs as root (it needs
  the serial console), but a dedicated `labfront@pve` user with a custom role
  limited to the sandbox range plus network modify is the correct hardening. Not
  done yet.

## 7. Host gotchas

**1. The node name is case sensitive.** If the node is `PVE-01`, asking for
`pve-01` returns HTTP 596 with `certificate verify failed` on every
`/nodes/pve-01/...` call, while `/nodes` itself works. The error sends you
looking at certificates when the real problem is the name. Copy `[proxmox] node`
from `pvesh get /nodes`.

**2. Bridges in sourced files are invisible to the PVE network API.** Bridges
defined in a file under `/etc/network/interfaces.d/` are not read by PVE, so
`GET /nodes/<node>/network` never lists them, and the only thing stopping
LabFront from creating a bridge with the same name is the profile's
`[protected] bridges`. List every live bridge there, and add new ones as the
lab grows.

LabFront therefore never uses the PVE network API for bridges: applying it ends
in an `ifreload` that rewrites `/etc/network/interfaces`. Sandbox bridges are made
with `ip link` instead and are never written to any file.

**Image boots need no template.** `image=local:import/vJunos-switch-<version>.qcow2`
imports a pristine disk per switch, which is also the fallback for the serial
question in step 3. Put the qcow2 in `/var/lib/vz/import/` first.

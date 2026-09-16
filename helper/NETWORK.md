# Putting the controller and the VMs on one network

Goal: the 4 targets and the controller reach each other over one private network
with no constraints. **Chosen approach: a VPC LAN** — the controller runs inside
the same AWS VPC as the targets, so everything talks over private addresses with
no NAT, no VPN, and nothing exposed to the public internet.

The helper still **dials out** to the controller (the locked design): the
controller listens on a port, each in-VM helper connects to the controller's
private IP. In a VPC LAN that connection is trivial — the only thing to get right
is the security group.

---

## VPC LAN (the plan)

### 1. Controller in the VPC

Run the controller (Ollama + the vision model + the Phase 2 listener) on an EC2
instance in the **same VPC** as the 4 targets. Ideally the same subnet; any
subnet in the VPC works as long as the route tables/NACLs allow intra-VPC
traffic (the default VPC allows all of it).

Sizing: `qwen2.5vl:7b` on CPU needs ~16 GB RAM and runs at ~35–75 s/step, so a
`c5.2xlarge` / `t3.xlarge`-class instance (8 vCPU, ~16 GB) is the floor. This is
the cost of moving the model into AWS; if you would rather keep the model on your
local box, use the mesh-VPN fallback below instead.

Find the controller's private address:

```powershell
ipconfig    # the 10.x.y.z / 172.31.x.y on the VPC NIC  -> CONTROLLER_PRIV_IP
```

It is stable for the instance's life. For something that survives stop/start,
use the instance's private DNS name (`ip-10-0-x-y.<region>.compute.internal`) or
give it a fixed private IP.

### 2. Security group: allow the helper port inbound to the controller

Security groups are stateful and default-deny inbound. On the **controller's**
security group, add one inbound rule:

| Type       | Port | Source |
|------------|------|--------|
| Custom TCP | 8765 | the **targets' security group** (preferred) or the VPC CIDR (e.g. `10.0.0.0/16`) |

Sourcing from the targets' SG (not `0.0.0.0/0`) means only the 4 VMs can reach
the listener, even though the port is bound. The targets' own SG needs no change
for this — outbound is open by default. Add an RDP rule (3389) only from your
admin IP if you RDP the VMs directly; it does not need to be public.

### 3. Point the helpers at the controller's private IP

The listener binds all interfaces on the controller; each VM's helper dials the
private IP:

```powershell
# on each VM (Phase 2 dial-out helper)
helper.exe --connect CONTROLLER_PRIV_IP:8765 --token <SHARED_TOKEN> --host-label vm-1
```

Nothing in the code changes — VPC LAN just makes `CONTROLLER_PRIV_IP` reachable.

### 4. Verify

From a VM:

```powershell
Test-NetConnection CONTROLLER_PRIV_IP -Port 8765    # TcpTestSucceeded : True
```

`True` means the SG and routing are right and the helpers will connect.

### Notes

- Ollama binding to `127.0.0.1` is fine — the model stays local to the
  controller; only the helper port (8765) is reached over the LAN.
- Phase 0 needs none of this: it is loopback on one VM with `poke.exe`. The VPC
  LAN only becomes load-bearing at Phase 2.
- Hardening: source the SG rule from the targets' SG (not the whole CIDR), keep
  the shared token secret and rotate it when a VM is retired, and keep the helper
  port off any public-facing subnet/SG.

---

## Fallback: mesh VPN (only if the controller is NOT in the VPC)

If you keep the controller on a box outside AWS (e.g. your local machine behind
NAT), a mesh VPN gives every node a stable `100.x` address reachable both ways
with no inbound rule and no port-forward. Tailscale (WireGuard) is the least
effort; its free tier covers the 5 machines.

- Controller: `winget install --id Tailscale.Tailscale -e`; `tailscale up`;
  `tailscale ip -4` → the address helpers dial.
- Each VM: install Tailscale, then `tailscale up --auth-key tskey-...` with a
  reusable, tagged key from the admin console (no per-VM browser login).
- Verify with `tailscale ping` both ways; then `helper.exe --connect
  <controller-100.x>:8765 --token ...`.
- Harden with a tailnet ACL that lets only the controller and `tag:lab` reach the
  helper port; optionally route RDP over the tailnet and drop public 3389.

The VPC LAN above is simpler and is the chosen path; this is here only for the
off-AWS-controller case.

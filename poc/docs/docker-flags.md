# What each Docker flag does

Every flag `sandbox_controller` passes when it creates a sandbox, what it
defends against, and which test fails if the flag is removed. "Removed →
fails" was checked by deleting the flag and re-running the test (see
[mutation checks](#how-this-was-checked)).

The command is built in `sandbox_controller/controller.py::_run_args`.

## Filesystem

| Flag | What it does | What it stops | Proved by |
|---|---|---|---|
| `--read-only` | Mounts the container's root filesystem read-only. | Code replacing binaries (`/usr/bin/python3`, `/usr/local/bin/pandoc`), editing `/etc`, or planting persistence in the image layers. Holds even for uid 0, because it is a property of the mount, not of file permissions. | `test_root_filesystem_is_read_only` (run as both uid 65534 and uid 0) |
| `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=Nm` | Gives the sandbox one scratch directory in RAM, writable, and capped at N MB. `noexec`: files there cannot be executed directly. `nosuid`: setuid bits are ignored. `nodev`: device nodes are ignored. | Disk-fill on the host through scratch space; dropping and running a binary from `/tmp`; the sandbox having *no* writable place at all (which `--read-only` would otherwise cause). Vanishes when the container dies. | `test_tmpfs_is_size_capped` |
| `--mount type=bind,src=<workdir>,dst=/work` | Makes one host directory visible, read-write, at `/work`. This is the only host path the sandbox sees. | Nothing by itself: it is the deliberate hole. Its safety is that it is exactly one per-sandbox directory, never a parent, and never `docker.sock`. **Not limited in size** (see README gaps). | `test_sandbox_cannot_read_host_files`, `test_sandbox_cannot_read_another_sandboxs_workdir` |
| `--mount type=bind,src=<ca.pem>,dst=/etc/ssl/proxy-ca.pem,readonly` | Installs the proxy's *public* certificate, read-only. Networked sandboxes only. | The sandbox tampering with its own trust anchor. Only the public cert is mounted; the proxy's private key is never on a path the sandbox can see. | `test_proxy_private_key_is_not_in_the_sandbox` |
| `--workdir /work` | Starts commands in `/work`. | Convenience only. | — |

## Identity and privilege

| Flag | What it does | What it stops | Proved by |
|---|---|---|---|
| `--user 65534:65534` | Runs every process as `nobody`, not root. | Root inside the container (which is also root on the kernel's side of most container escapes, and can write anywhere `--read-only` doesn't cover). | `test_hardening_is_in_effect` |
| `--cap-drop ALL` | Empties the Linux capability *bounding set*: no `CAP_SYS_ADMIN` (mount, namespaces), no `CAP_NET_RAW` (raw sockets), no `CAP_NET_ADMIN` (reconfigure the network), no `CAP_CHOWN`, and so on, and no way to get them back. | A process regaining privilege even if it becomes root. Test asserts `CapBnd == 0`; asserting `CapEff` would prove nothing, since any non-root process already has an empty effective set. | `test_hardening_is_in_effect` |
| `--security-opt no-new-privileges` | Sets the kernel `no_new_privs` bit: `execve` of a setuid or file-capability binary can no longer raise privilege. Inherited by every child. | `sudo`-style and setuid escalation. | `test_hardening_is_in_effect` (`NoNewPrivs: 1`) |

Not used, on purpose: `--privileged`, `--cap-add`, `--pid=host`,
`--net=host`, `--ipc=host`, `-v /var/run/docker.sock`. Any one of them would
void the rest. Docker's default seccomp profile applies automatically (it is
not set by a flag and this PoC does not test it separately).

## Resource limits (cgroups)

| Flag | What it does | What it stops | Proved by |
|---|---|---|---|
| `--cpus N` | CPU quota: at most N cores' worth of time, enforced by throttling. | A busy loop starving other sandboxes or the host. Throttles; does not kill. | `test_cpu_limit_throttles_a_busy_loop` (a spinning loop with `--cpus 0.5` gets 0.3–0.7 of a core) |
| `--memory Nm` | Hard memory cap for the whole container (all processes combined). Exceeding it makes the kernel OOM-killer send SIGKILL. | A memory hog taking the host down. Test sees exit code 137 (128 + SIGKILL). | `test_memory_hog_is_killed_by_the_memory_limit` |
| `--memory-swap Nm` | Total memory **plus swap**. Set equal to `--memory`, it means *no swap*. | The cap being quietly bypassed by swapping to disk (slow, and a disk-fill vector). Without this, Docker defaults swap to the same size again, doubling the effective limit. | *(same test; swap may be absent on the test host, so this one is not independently proved)* |
| `--pids-limit N` | Maximum number of processes/threads in the container. `fork()` fails past it. | Fork bombs. | `test_fork_bomb_is_contained_by_pids_limit`, `test_classic_bash_fork_bomb_is_bounded` |

## Lifecycle

| Flag / argument | What it does | What it stops |
|---|---|---|
| `sleep <deadline_s>` (the container's main process) | The container exists only as long as `sleep` does. When it exits, the container stops and the kernel kills everything in it. | The deadline being a promise made by the host process. If the controller, executor or host crashes, the sandbox still dies on schedule. |
| `docker kill` on exec timeout | The controller kills the whole container, not just the `docker exec` client. | A timed-out command surviving: killing only the client leaves the process running in the container. Proved by `test_script_past_its_timeout_is_killed` (container is gone afterwards). |
| `--rm` | Removes the container once it stops. | Leaked stopped containers; the name being reused. |
| `--pull never` | Never fetches the image from a registry. | A missing local sandbox image silently becoming a pull of that name from a public registry. It is an error instead (`No such image`). |
| `--init` | Runs a tiny init (tini) as PID 1. | Zombie processes piling up inside a long-lived sandbox; signals not being forwarded. |
| `--name`, `--label agent-sandbox=1` | Names it; labels it so it can be listed (`docker ps --filter label=agent-sandbox=1`). | Orphans being untraceable. |
| `--env HOME=/tmp`, `--env PYTHONDONTWRITEBYTECODE=1` | Points `$HOME` at the writable scratch; stops Python trying to write `.pyc` files onto the read-only root. | Tools failing for want of a writable home. |

## Network

| Setting | What it does | What it stops | Proved by |
|---|---|---|---|
| `--network none` (no-proxy mode) | No network interface except loopback. | All network access. Used by the isolation tests, which need none. | `test_isolation.py` |
| `docker network create --internal <per-sandbox>` then `--network <per-sandbox>` | Every sandbox gets its **own** network, declared `--internal`: containers on it can talk to each other, but there is no gateway, so no route off it. Only the sandbox and the proxy are attached to it (`docker network connect --alias proxy`). The network is removed when the sandbox stops. | Direct connections to the internet or to the stub (which lives only on the proxy's other network); DNS for outside names; and, because no other sandbox is on the network, **sandbox-to-sandbox traffic**. A first version put all sandboxes on one shared internal network, which let a tenant-b sandbox read data from a tenant-a sandbox. Switching off container-to-container traffic on a shared network is not an option: it would also block the sandbox's route to the proxy. | `test_direct_request_to_the_stub_fails`, `test_no_route_to_the_internet_and_no_dns`, `test_sandboxes_cannot_reach_each_other`, `test_stopping_a_sandbox_removes_its_network` |
| `--env HTTPS_PROXY=… SSL_CERT_FILE=… GH_TOKEN=<placeholder>` | Tells well-behaved tools to use the proxy, to trust its certificate, and gives them a placeholder token. | Nothing: these are conveniences, not a control. The control is the network above; a tool that ignores `HTTPS_PROXY` simply has no route. | `test_the_sandbox_holds_only_the_placeholder` |

The proxy and stub containers (compose) carry the same hardening:
`read_only`, `cap_drop: [ALL]`, `no-new-privileges`, uid 65534.

## How this was checked

"Proved by" means the named test passes with the flag and fails without it.
That was verified by deleting the flag from the controller and re-running the
test. It is what showed that the first draft of the capability test was
vacuous (it passed with `--cap-drop` removed, because it asserted `CapEff`;
it now asserts `CapBnd`). The proxy's checks were verified the same way, by
breaking the addon and rebuilding the proxy image.

Checked by removal, the test went red every time:

- `--read-only`, the `--tmpfs` size cap, `--user`, `--cap-drop ALL`,
  `no-new-privileges`, `--cpus`, `--memory`, `--pids-limit`
- kill-the-container-on-timeout
- the `--internal` network (made routable: both network tests failed) and the
  one-network-per-sandbox rule (made shared: the sandbox-to-sandbox test failed)

**Not** checked that way, so "proved by" is weaker for them: `--memory-swap`
(the test host may have no swap at all), the `/work` and CA bind mounts (the
tests show what is *not* visible, not that removing the flag changes it),
`--init`, `--rm`, `--workdir`.

## Docker Desktop on a Mac

Docker Desktop runs the Linux kernel inside a lightweight VM. These flags
configure *that* kernel's namespaces and cgroups, so they behave the same as
on Linux, but the sandbox boundary is a plain container inside a VM: there
is no gVisor or Kata, and a kernel exploit from inside a sandbox would
reach the VM's kernel, not a user-space kernel like gVisor's. The VM does
add a second boundary between a sandbox and your Mac. This was developed and
tested on Linux (Docker Engine 29.9.3, runc, cgroup v1); it has **not** been
run on Docker Desktop.

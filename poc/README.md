# Sandboxed executor: proof of concept

The part of the platform that runs an agent's Bash/Python and CLIs safely:
a locked-down sandbox, hard limits, and a network path whose only exit is a
proxy that holds the credentials, so code can *use* a credential without
being able to *read* it. Safety claims are backed by tests that run real
containers. Design context: [../docs/service-architecture.md](../docs/service-architecture.md).

```bash
pip install pytest      # the only host dependency besides Docker and Python 3.10+
make demo               # scripted agent: pandoc, a credentialed call, a denied call, an escape attempt
make test               # 36 tests, ~45 s
make bench              # cold start, 50 runs per configuration
make down               # stop the proxy and stub
```

If your network intercepts TLS during `docker build`, set
`BUILD_CA_BUNDLE=/path/to/ca.pem` first (only used for the proxy image's `pip install`).

## Layout: one folder per box in the architecture diagram

| Path | Architecture box | Status |
|---|---|---|
| `agent_executor/executor.py` | agent-executor | real loop, scripted "model" |
| `sandbox_controller/controller.py` | sandbox-controller | real |
| `image/` | the sandbox image (python, bash, pandoc) | real |
| `agent_network_proxy/addon.py`, `registry.py`, `Dockerfile` | agent-network-proxy + caller registry | real (mitmproxy) |
| `config/grants.json` | agent-crud-service grants | **faked** (a file) |
| `config/secrets.json` | secret-manager | **faked** (a file; fake tokens) |
| `stubs/github_stub.py` | External service (GitHub) | **faked** (prints headers it receives) |
| `docker-compose.yml`, `stack.py` | the always-on parts and the two networks | real |
| `.state/` (generated) | session log, audit store, registry | files |
| `tests/`, `demo.py`, `bench/` | | |

Not present at all: session-manager, ingress, auth, llm-gateway, object store, rollup.

## What is real

- **Sandbox:** read-only root filesystem, size-capped tmpfs scratch, non-root user, all capabilities dropped, no-new-privileges, CPU / memory (no swap) / process-count limits, a hard deadline that is the container's own lifetime, a mounted work dir, output capped. Every flag is explained in [docs/docker-flags.md](docs/docker-flags.md).
- **Timeout kill:** a command past its timeout takes the whole sandbox down (verified gone, not just the client).
- **Network:** the sandbox is on a Docker `internal: true` network. It has no route to the internet or to the stub, and cannot resolve outside names. The proxy is the only thing it can reach.
- **Proxy (real mitmproxy):** identifies the caller by source address from a registry file (never from request contents); rejects a stale epoch; checks the destination against the agent's grants; applies a per-tenant sliding-window rate limit; swaps the placeholder token for the real one; scrubs the real value from the response; writes an audit line holding the credential's *name*. Anything doubtful is denied.
- **Credential handling:** the sandbox gets a placeholder token and the proxy's *public* certificate only. The real token reaches the stub; it is not in the sandbox's environment, filesystem or output.
- **Fencing:** starting a sandbox with a newer epoch stops the old one; an older epoch is refused; a zombie with a stale epoch is rejected by the proxy.
- **Executor:** checks each tool call against the agent's grants and a step cap, creates the sandbox lazily, appends every step to a session log.
- **Tests were broken on purpose.** For each protection I removed it and confirmed its test fails ([docker-flags.md](docs/docker-flags.md#how-this-was-checked)). That caught two tests that passed vacuously.

## What is faked

- **The model:** a scripted list of `(tool, code)` calls.
- **secret-manager, grants:** JSON files read by the proxy (grants also by the controller and executor). Tokens are obviously fake strings.
- **GitHub:** a local HTTPS stub (`api.github.test`) that prints and records the headers it gets, and echoes the `Authorization` header in its body so response scrubbing has something to scrub.
- **`gh`:** the tests and demo call the API with Python `urllib`, which honours `HTTPS_PROXY` / `SSL_CERT_FILE`. **`gh` itself was not tried.**
- **Session manager:** the caller supplies `(session, agent, tenant, epoch)` directly.

## Properties and the tests that show them

| Property | Test |
|---|---|
| Root filesystem not writable (also as uid 0) | `test_root_filesystem_is_read_only` |
| Non-root, no capabilities, no-new-privileges | `test_hardening_is_in_effect` |
| Past-timeout script killed, container gone | `test_script_past_its_timeout_is_killed` |
| Fork bomb contained | `test_fork_bomb_is_contained_by_pids_limit`, `test_classic_bash_fork_bomb_is_bounded` |
| Memory hog killed | `test_memory_hog_is_killed_by_the_memory_limit` |
| CPU throttled | `test_cpu_limit_throttles_a_busy_loop` |
| Cannot read host files / another sandbox's work dir (incl. via symlink) | `test_sandbox_cannot_read_host_files`, `test_sandbox_cannot_read_another_sandboxs_workdir` |
| pandoc converts md → docx/html inside the sandbox | `test_pandoc_converts_markdown_inside_the_sandbox` |
| Direct request to the stub fails; no internet, no DNS | `test_direct_request_to_the_stub_fails`, `test_no_route_to_the_internet_and_no_dns` |
| Ungranted destination denied (and audited) | `test_destination_not_in_grants_is_denied` |
| Identity from address, not claims | `test_caller_is_identified_by_address_not_by_claims` |
| Real token in the stub, nowhere in the sandbox | `test_stub_receives_the_real_token_but_the_sandbox_never_sees_it`, `test_real_token_is_nowhere_in_the_sandbox`, `test_proxy_private_key_is_not_in_the_sandbox` |
| Per-tenant credentials; audit has names only | `test_each_tenant_gets_its_own_credential`, `test_audit_names_the_credential_and_never_holds_a_value` |
| Rate limit | `test_rate_limit_is_per_tenant_and_a_window` |
| Stale epoch rejected; new epoch stops the old sandbox | `test_zombie_with_a_stale_epoch_is_rejected_by_the_proxy`, `test_new_epoch_stops_the_old_sandbox`, `test_stale_epoch_cannot_start_a_sandbox` |
| Pinned-certificate tool fails closed, no credential | `test_a_tool_that_pins_the_certificate_fails_closed_with_no_credential` |
| Executor: grants, step cap, lazy sandbox, log | `tests/test_executor.py` |

Cold start (measured; see [docs/cold-start.md](docs/cold-start.md) for the table and caveats): a fresh
sandbox is ready in about 0.25 s (no proxy) to 0.31 s (proxied) at p50, p95 0.27–0.33 s, on a
Linux VM. Pandoc in the image made no measurable difference to start time. These are local-image
numbers: no pull, no Kubernetes.

## What is not built (gaps)

**Isolation strength**
- **Docker Desktop on a Mac:** it runs containers in a Linux VM, so this is plain-container isolation (namespaces, cgroups, capabilities) inside a VM, **not gVisor or Kata**. A kernel exploit from inside a sandbox would reach that VM's kernel. The design's PoC rule matches the architecture: the sandbox reaches only the proxy.
- **Not run on a Mac.** Built and tested on Linux (Docker Engine 29.9.3, runc, cgroup v1, amd64). Untested: Docker Desktop's file sharing and permissions, Apple silicon (the arm64 pandoc is hash-pinned but was never executed).
- **Escape coverage is specific, not exhaustive.** Tested: write to root fs, host and cross-sandbox file reads, fork bomb, memory hog, no network route, no capabilities. Not tested: user-namespace tricks, ptrace, `/proc` and `/sys` probing, kernel exploits, side channels. Passing these tests shows those attempts fail, not that escape is impossible.
- Docker's default seccomp profile applies but is not tested or tuned; no AppArmor/SELinux profile.

**Credentials and the proxy**
- **API-tool calls from the executor** (explicit session id + epoch, the proxy's other identification path) are not built. Only calls from a sandbox, identified by address, exist.
- Only the proxy-configured mechanism works. Transparent interception for hardcoded endpoints is not built; such a tool has no route at all. Client-certificate (mTLS) tools were not tested; only certificate pinning was. gRPC and raw TCP are not handled (PROXY-1).
- Placeholder swap covers headers and query string, not bodies. Response scrubbing is exact-match only: an encoded or transformed echo of a secret gets through (AUDIT-1).
- The proxy trusts only the stub's self-signed cert upstream, has one CA for all tenants, and keeps its key on disk (D21, D22).
- Rate limit is in the memory of one proxy; N replicas would allow N times the limit.
- The audit log is a local file with no append-only enforcement, retention or tamper evidence; the registry's epoch table is never pruned.

**Platform**
- **Kubernetes:** none. The controller drives Docker, and holds `exec` as well (D5); on Kubernetes the network rule becomes a NetworkPolicy and the executor would reach the sandbox itself.
- No size limit on the work dir; no object-store restore/flush.
- Durability: the session log is written but there is no resume, no heartbeat, no reconciler. The controller's session map is in memory.
- Single process, no concurrency control. Nothing here shows the 1000-agent / 10k-calls-per-minute target.
- No usage metering, cost rollup or tracing.
- Resource limits are assumptions (D6), not tuned values.

## Why this slice

The architecture's central claim is that agent-supplied code can run, and can use
a tenant's credentials, without escaping, harming a neighbour, or ever seeing a secret.
Everything else in the design (ingress, CRUD, session state) is conventional service plumbing
that does not change if the claim is false; this slice is where the claim is true or not. It
also makes the design's most criticised split concrete: a controller with infrastructure rights
and no secrets, a proxy with secrets and no infrastructure rights, and a sandbox with neither.
What it proves is narrow and checked: the listed containment properties hold for plain containers
under Docker, a placeholder-to-credential swap through a TLS-intercepting proxy works end to end
with the real value never entering the sandbox, caller identity and epoch fencing are enforced by
the proxy rather than claimed by the caller, and pinned-certificate tools fail closed. What it does
not prove: adversarial-grade isolation (that needs gVisor, Firecracker or Kata and a real escape
exercise), behaviour on Docker Desktop or Kubernetes, `gh` specifically, scale, durability, or that
the cost of running a proxy hop and a sandbox per session is acceptable at 10k calls a minute.

## Other documents

- [docs/docker-flags.md](docs/docker-flags.md): what each flag does and which test shows it
- [docs/design-choices.md](docs/design-choices.md): every choice I made, numbered, with what overriding it costs
- [docs/cold-start.md](docs/cold-start.md): measured cold start

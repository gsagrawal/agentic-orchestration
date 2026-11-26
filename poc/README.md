# Sandboxed executor: proof of concept

The part of the platform that runs an agent's Bash/Python and CLIs safely:
a locked-down sandbox, hard limits, and a network path whose only exit is a
proxy that holds the credentials, so code can *use* a credential without
being able to *read* it. Safety claims are backed by tests that run real
containers. Design context: [../docs/service-architecture.md](../docs/service-architecture.md).

```bash
pip install pytest      # the only host dependency besides Docker and Python 3.10+
make demo               # scripted agent: pandoc, a credentialed call, a denied call, an escape attempt
make test               # 116 tests, ~2.5 min
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
- **Network:** every sandbox gets its own Docker `--internal` network, shared with nothing but the proxy. It has no route to the internet, the stub or any other sandbox, and cannot resolve outside names or other sandboxes' names. The network is removed when the sandbox stops.
- **Proxy (real mitmproxy):** identifies the caller by source address from a registry file (never from request contents); rejects a stale epoch or an agent/tenant mismatch; checks host, port and HTTPS against the agent's grants; applies a per-tenant sliding-window rate limit; writes the audit line *before* using the credential, then swaps the placeholder for the real token; scrubs the real value from the response; audits the outcome, including failures. Names the credential, never its value. Anything doubtful, including an exception inside the proxy, is denied. Only HTTP request/response traffic is understood, so WebSocket upgrades are denied and mitmproxy's raw-TCP fallback is switched off (it would relay bytes that nothing audits, limits or scrubs).
- **Credential handling:** the sandbox gets a placeholder token and the proxy's *public* certificate only. The real token reaches the stub; it is not in the sandbox's environment, filesystem or output.
- **Fencing:** starting a sandbox with a newer epoch stops the old one; an older epoch is refused; a zombie with a stale epoch is rejected by the proxy.
- **Executor:** checks each tool call against the agent's grants, a step cap and a session deadline, creates the sandbox lazily, replaces a sandbox that vanished (only when the command never started), appends every step to a session log.
- **Tests were broken on purpose.** For each protection I removed it and confirmed its test fails ([docker-flags.md](docs/docker-flags.md#how-this-was-checked)). That caught tests that passed vacuously, and later a code review found real bugs, including a cross-tenant network path and a fail-open proxy; each now has a test that was seen failing before the fix.

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
| One sandbox cannot reach another; networks are cleaned up | `test_sandboxes_cannot_reach_each_other`, `test_stopping_a_sandbox_removes_its_network` |
| Plaintext HTTP and ungranted ports are not forwarded | `test_plaintext_http_and_other_ports_are_not_forwarded` |
| Non-HTTP bytes and WebSocket upgrades are not relayed | `test_non_http_traffic_inside_a_granted_tunnel_is_not_relayed`, `test_a_websocket_upgrade_is_denied` |
| A proxy error denies rather than forwards, even with a corrupt registry | `test_a_policy_error_denies_instead_of_forwarding`, `test_a_corrupt_registry_denies_everything` |
| Query strings pass through untouched except for the placeholder | `test_query_strings_are_left_alone_except_for_the_placeholder` |
| Failed upstream calls are still audited | `test_a_failed_upstream_call_is_still_audited` |
| Agent/tenant mismatch refused (controller and proxy) | `test_controller_refuses_an_agent_tenant_mismatch`, `test_proxy_rejects_an_agent_tenant_mismatch` |
| Scrubbing leaves repeated headers and HEAD responses intact | `test_response_scrubbing_keeps_headers_intact` |
| The leak detector itself works (no vacuous "not found") | `test_the_leak_detector_detects_a_planted_leak` |
| Registry: no lost updates, no stale reads, a wrongly shaped file is an error | `tests/test_registry.py` |
| `reap()` recovers even from a corrupt registry | `test_reap_recovers_even_from_a_corrupt_registry`, `test_reap_distrusts_every_address_even_if_the_daemon_is_down` |
| A command killed by the sandbox's own deadline says so | `test_a_command_killed_by_the_sandbox_deadline_says_so` |
| A query is forwarded byte for byte; an undecodable response is refused; a stub restart does not break the proxy | `test_a_query_is_forwarded_byte_for_byte_apart_from_the_placeholder`, `test_a_response_the_proxy_cannot_decode_is_not_passed_through`, `test_the_stub_can_restart_without_breaking_the_proxy` |
| Two controllers cannot both win one session, and the loser keeps no sandbox; a failed fence leaves no claim; a failed create says what it left behind | `test_two_controllers_racing_for_one_session_cannot_both_win`, `test_a_controller_that_loses_the_fence_race_does_not_keep_its_sandbox`, `test_a_failed_fence_does_not_leave_its_claim_behind`, `test_a_create_that_fails_and_cannot_clean_up_says_what_it_left_behind` |
| A work dir belongs to one session; its marker is not followed | `test_a_workdir_belongs_to_one_session_and_its_marker_is_not_followed` |
| Basic-auth placeholders are swapped and an echoed Basic header is scrubbed; unswappable ones (path, body, query name) are refused | `test_basic_auth_placeholders_are_swapped_too`, `test_an_echoed_basic_credential_is_scrubbed_too`, `test_a_placeholder_where_it_cannot_be_swapped_is_refused_not_forwarded`, `test_a_placeholder_in_a_query_parameter_name_is_refused` |
| Ungranted destination denied (and audited) | `test_destination_not_in_grants_is_denied` |
| Identity from address, not claims | `test_caller_is_identified_by_address_not_by_claims` |
| Real token in the stub, nowhere in the sandbox | `test_stub_receives_the_real_token_but_the_sandbox_never_sees_it`, `test_real_token_is_nowhere_in_the_sandbox`, `test_proxy_private_key_is_not_in_the_sandbox` |
| Per-tenant credentials; audit has names only | `test_each_tenant_gets_its_own_credential`, `test_audit_names_the_credential_and_never_holds_a_value` |
| Rate limit | `test_rate_limit_is_per_tenant_and_a_window` |
| Stale epoch rejected; new epoch stops the old sandbox | `test_zombie_with_a_stale_epoch_is_rejected_by_the_proxy`, `test_new_epoch_stops_the_old_sandbox`, `test_stale_epoch_cannot_start_a_sandbox` |
| Pinned-certificate tool fails closed, no credential | `test_a_tool_that_pins_the_certificate_fails_closed_with_no_credential` |
| Executor: grants, step cap, lazy sandbox, log, session deadline, lost-sandbox replacement, no-proxy controller, no leak on unexpected failure, rejects tools it cannot run | `tests/test_executor.py` |
| Controller refuses: a comma in the workdir or CA path, a non-dedicated workdir, one env var for two placeholders, malformed grants | `test_a_comma_in_the_workdir_cannot_inject_mount_options`, `test_one_env_var_cannot_stand_for_two_different_placeholders` |

Cold start (measured; see [docs/cold-start.md](docs/cold-start.md) for the table and caveats): a fresh
sandbox is ready in about 0.22 s without the proxy and about 0.33 s through it at p50 (p95 0.26 s and 0.38 s), on a
Linux VM. Stopping a proxied sandbox takes about 0.38 s. Pandoc in the image made no measurable difference to start
time. These are local-image numbers: no pull, no Kubernetes, and run-to-run variation is about 10%.

## What is not built (gaps)

**Isolation strength**
- **Docker Desktop on a Mac:** it runs containers in a Linux VM, so this is plain-container isolation (namespaces, cgroups, capabilities) inside a VM, **not gVisor or Kata**. A kernel exploit from inside a sandbox would reach that VM's kernel. The design's PoC rule matches the architecture: the sandbox reaches only the proxy.
- **Not run on a Mac.** Built and tested on Linux (Docker Engine 29.9.3, runc, cgroup v1, amd64). Untested: Docker Desktop's file sharing and permissions, Apple silicon (the arm64 pandoc is hash-pinned but was never executed).
- **Escape coverage is specific, not exhaustive.** Tested: write to root fs, host and cross-sandbox file reads, fork bomb, memory hog, no network route, no capabilities. Not tested: user-namespace tricks, ptrace, `/proc` and `/sys` probing, kernel exploits, side channels. Passing these tests shows those attempts fail, not that escape is impossible.
- Docker's default seccomp profile applies but is not tested or tuned; no AppArmor/SELinux profile.

**Credentials and the proxy**
- **API-tool calls from the executor** (explicit session id + epoch, the proxy's other identification path) are not built. Only calls from a sandbox, identified by address, exist.
- Only the proxy-configured mechanism works. Transparent interception for hardcoded endpoints is not built; such a tool has no route at all. Client-certificate (mTLS) tools were not tested; only certificate pinning was. gRPC and raw TCP are not handled (PROXY-1).
- Placeholder swap covers header values (including inside HTTP Basic credentials, which is how git sends a token) and query values. A placeholder in the URL path or in a request body cannot be swapped, so such a request is **refused** (`placeholder in an unsupported position`) instead of going out with a fake token. Cookies and other encodings are not handled. Response scrubbing removes the secret, its base64 and what the proxy rewrote, but a destination that reflects a credential through further transformations is not defeated (AUDIT-1); it is defence in depth for a trusted destination, and the real control is to grant only destinations that do not echo credentials.
- The proxy trusts only the stub's self-signed cert upstream, has one CA for all tenants, and keeps its key on disk (D21, D22).
- Rate limit is in the memory of one proxy (N replicas would allow N times the limit) and counts requests, not `CONNECT`s, so a sandbox can open tunnels to a granted host without being limited.
- The state directories are meant for a single-user machine such as a laptop. The registry directory is never world-writable (it is the proxy's trust root). The audit, stub and proxy-conf directories are handed to uid 65534 with mode 0750 when `stack.py` runs as root, but **0777 when it runs as a normal user** (chown is not allowed), because the containers write them as uid 65534. In that case, on a shared host, another user could rewrite the audit log or replace the proxy CA certificate that is mounted into every sandbox.
- The audit log is a local file with no append-only enforcement, retention or tamper evidence; the registry's epoch table is never pruned, and its lock is a single-host `flock`.

**Platform**
- **Concurrent sandboxes are capped by Docker's address pools.** Each sandbox has its own network, and on this host the daemon refused the 26th one (`all predefined address pools have been fully subnetted`), measured with otherwise-default settings. A daemon with larger `default-address-pools` raises it; on Kubernetes a NetworkPolicy replaces the per-sandbox network and the limit goes away. Nothing in the PoC works around this.
- **Kubernetes:** none. The controller drives Docker, and holds `exec` as well (D5); on Kubernetes the network rule becomes a NetworkPolicy and the executor would reach the sandbox itself.
- No size limit on the work dir, which must be dedicated to its sandbox (the controller makes it world-writable); no object-store restore/flush.
- Replay edge case: the executor re-runs a command in a fresh sandbox only when `docker exec` could not start it, which it recognises from the daemon's error text plus an inspect (D30). A command that itself prints that exact text, exits non-zero, and runs in a sandbox that dies at the same instant would be replayed once. No privilege is gained (the agent could simply call the tool twice), but it is not strictly "exactly once".
- Durability: the session log is written but there is no resume, no heartbeat, no reconciler. The controller's session map is in memory.
- One controller instance is the supported shape (D16). Fencing is atomic across controllers on one host: the epoch check, the bump and a claim on the session happen inside one registry transaction (a file lock), and the race test runs two controllers against one session. A claim made before its sandbox exists expires after 60 s if the claimant crashes. The lock is a single-host `flock`, so this does not extend to controllers on different machines.
- Single process, no concurrency control. Nothing here shows the 1000-agent / 10k-calls-per-minute target.
- The proxy is attached to each sandbox network by hand. Recreating the proxy container (`make demo` or `python3 stack.py up` after a config change) while sandboxes are running detaches them from it, and they then fail with network errors until they are recreated. Do not restart the stack under live sandboxes.
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

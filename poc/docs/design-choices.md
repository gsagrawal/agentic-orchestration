# Design choices

Choices I made where the brief or the architecture left room. Disagree with
any of them by number. "Assumption" means a value I picked, not one I
measured or was given.

## Sandbox and controller

| # | Choice | Why | If you override it |
|---|---|---|---|
| D1 | Drive Docker by shelling out to the `docker` CLI, not the Python SDK. | No dependency; every call is visible as the command it is; the flags map 1:1 to [docker-flags.md](docker-flags.md). | SDK gives typed errors and no subprocess cost. Mechanical swap inside `controller.py`. |
| D2 | One long-lived container per session (`sleep <deadline>` as PID 1) plus `docker exec` per command. | Matches the architecture: the sandbox is created lazily and kept so workspace and scratch survive between calls. Also cheaper per call than a container per command (see cold-start numbers). | One container per command is simpler and gives a clean slate every time, at the cost of one cold start per call and no shared `/tmp`. |
| D3 | A command that times out kills the **whole sandbox** (`docker kill`), not just that command. | Killing only the `docker exec` client leaves the process running inside. Killing the container is the one kill that cannot be dodged. | Cost: background processes and `/tmp` are lost; `/work` survives, and the executor makes a fresh sandbox for the next call. A finer kill would need a supervisor inside the sandbox, which is code agent-supplied code could target. |
| D4 | The deadline is the container's own lifetime (PID 1 is `sleep N`). | Independent of the controller, executor or host staying alive. | — |
| D5 | `controller.exec()` lives in the controller. | Architecture says the executor talks straight to the sandbox and the controller only creates and stops. In this PoC `docker exec` needs the Docker socket, which is exactly the privilege the executor must not hold. So the controller carries it. | On Kubernetes the executor would reach a small agent inside the sandbox over the network, and the controller would shrink back to create/stop. This is the biggest deviation from the diagram. |
| D6 | Limits: 1 CPU, 256 MB RAM (no swap), 64 pids, 64 MB `/tmp`, 300 s lifetime, 30 s per command, 1 MiB output cap per stream. | **Assumptions.** Nothing in the brief gives numbers. | All in `Limits` / executor args. |
| D7 | The work dir is a host directory bind-mounted at `/work`, made world-writable (`0o777`). | Needed so uid 65534 can write to it on both Linux and Docker Desktop, where host chown is unreliable. The per-sandbox directory is the only exposed path. | Cost: no size limit on it (a sandbox can fill the host disk through `/work`), and any other host user can read it. A named volume with a quota, or an object-store sync (as the architecture says), fixes both. |
| D8 | Image = `python:3.12-slim-bookworm` (pulled from `mirror.gcr.io`) + checksum-pinned static pandoc 3.1.12.3. | Small, reproducible, no apt at build. The mirror is because Docker Hub returned HTTP 429 in my environment; override with `SANDBOX_BASE`. | Both architectures are pinned, but only **amd64 was built and run**; the arm64 pandoc (Apple silicon) is hash-pinned and untested. |
| D9 | The output cap drops the excess rather than failing the command. | A noisy command should not be able to exhaust host memory or hang on a full pipe. `ExecResult.truncated` says it happened. | — |

## Network and proxy

| # | Choice | Why | If you override it |
|---|---|---|---|
| D10 | Sandbox network = a Docker `internal: true` network; the proxy is on that network and on a second, external one. | "No route out" is a property of the network, not of anything in the sandbox. | On Kubernetes this becomes a NetworkPolicy. |
| D11 | Sandboxes find the proxy by being **configured** (`HTTPS_PROXY`, `SSL_CERT_FILE`, …), not by transparent redirection. | This is the settled design's first mechanism (tool pointed at the proxy) combined with the proxy-signed certificate. Transparent interception (iptables redirect) needs `NET_ADMIN` in the sandbox's network namespace, which I did not want to grant. | Consequence: a tool that ignores the proxy variables gets no route at all, rather than a route with no credential. Both fail closed; the error is just less informative. |
| D12 | The demo and tests call the API with Python `urllib`, not `gh`. | `gh` is not in the image. `urllib` honours `HTTPS_PROXY` and `SSL_CERT_FILE` the way `gh` is expected to, but **`gh` itself was not tried**; the architecture already flags whether `gh` accepts this as unconfirmed. | Adding `gh` to the image and swapping one line in the demo is the next experiment. |
| D13 | The proxy identifies a caller by the connection's source IP, looked up in `registry.json`. Request headers are ignored for identity. | Per the architecture. Tested by sending forged `X-Tenant` headers. | — |
| D14 | The registry holds `agent` as well as session, tenant, epoch. | The proxy needs the agent to find its grants; the brief's list (session, tenant, epoch) wasn't enough. | Could be looked up from the session manager instead. |
| D15 | The registry also keeps the newest epoch ever seen per session. The controller fences in this order: record the new epoch → unregister the old sandbox → kill it. | A stale sandbox is rejected by the proxy *before* it is dead, so there is no window where both are trusted. | The epoch table grows forever (never pruned). |
| D16 | The controller remembers session → sandbox in memory. | Simplest way to find "the old one". | Lost on a controller restart; the registry file survives, but the controller wouldn't know to kill the old sandbox. A real controller would list containers by label. |
| D17 | One `grants.json`: per **agent** (tools, destinations with credential name, placeholder, env var) and per **tenant** (rate limit). One `secrets.json` keyed by tenant then credential name. | The proxy can then only ever fetch a credential from the caller's own tenant's section. | — |
| D18 | The placeholder is swapped in request **headers and query string**, not the body. | Covers `Authorization` and `?token=`; a body swap adds parsing risk for little PoC value. | One more loop in `addon.request`. |
| D19 | Response scrubbing = exact-match replacement of the real value in headers and body. | Enough to show the mechanism. | Does not catch an encoded or transformed echo (base64, split across chunks). This is the AUDIT-1 residual risk already in poc-vs-production.md; I did not solve it. |
| D20 | Rate limit: sliding window, per tenant, in the proxy's memory, counted per HTTP request (not per CONNECT). | Simple and testable with a 3-requests-per-5-s tenant. | Per replica only: with N proxies the real limit is N× higher. Needs a shared store at scale. |
| D21 | The proxy verifies its upstream against **only the stub's self-signed certificate**. | Lets TLS to the upstream be real in the PoC. | Production uses the system's public roots, not a single pinned cert. |
| D22 | The proxy's CA private key sits in `.state/proxy-conf/` on the host, one CA for all tenants. | PoC convenience. The key is mounted only into the proxy container. | Production: per-tenant or per-sandbox CA, key in an HSM/KMS, rotation. Already PROXY-1. |
| D23 | Any doubt is a denial: unknown caller, stale epoch, ungranted destination, rate limit. Denials at CONNECT time are audited too. | Fail closed. | — |

## Executor and tests

| # | Choice | Why |
|---|---|---|
| D24 | The "model" is a scripted list of `ToolCall(tool, code)`; tools are `bash` and `python`. | Brief says to fake the model. The loop around it is real. |
| D25 | A tool call that isn't granted is returned to the "model" as an error result and the loop continues; hitting the step cap ends the loop. | A real model should see the refusal and adapt. The cap is the runaway backstop. |
| D26 | Tests use real Docker, real mitmproxy and real containers. No mocks. | A mock cannot demonstrate isolation. They need Docker, and take ~40 s. |
| D27 | Tests check that a protection **fails when removed**, not only that things pass. | Two of my first drafts passed vacuously (see docker-flags.md). |
| D28 | Session ids in tests carry a random suffix. | The registry remembers epochs across runs on purpose, so reusing an id would fail on the second run. This was a real test bug I hit and fixed. |
| D29 | Cold start is measured as: `create()` (docker run + network attach + registry write) + the first `exec` of `true`, nearest-rank percentiles, 1 warm-up run discarded. | One definition, stated. |

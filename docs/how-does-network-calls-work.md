# How network calls work

## An HTTP API tool (configured on the agent)

The agent has an API tool, such as "create ticket", with a destination URL and a credential stored in secret-manager.

1. The model asks for the tool. The executor checks the grant and step cap and logs the call.
2. The executor sends the request to agent-network-proxy with the session id and epoch.
3. The proxy identifies the caller, checks the session is active and the destination is granted, and checks the tenant's rate limit.
4. The proxy fetches the credential, adds it to the request, calls the service over HTTPS, scrubs the response and writes the audit record.
5. The result goes back to the executor, then to the session log and the model.

No sandbox is involved, and the executor never sees the credential.

## HTTP from code in the sandbox (for example curl or Python requests)

This only gets a credential if the code or tool is configured to send its request to the proxy.

If it sends an ordinary HTTPS request to the real host, the proxy can allow or deny the destination but can't add a credential. The call goes out without one and usually fails.

## A gh call

### Solution 1: point gh at the proxy (per-tool configuration)

1. At sandbox start, the controller sets gh to use the proxy's address as its GitHub host, with a placeholder token. The real token is not in the sandbox.
2. The agent runs `gh issue list`. gh sends its request to the proxy over plain HTTP inside the sandbox's private network.
3. The proxy identifies the session and tenant from the sandbox's network address, which the controller registered.
4. The proxy checks the grant for GitHub and the repository, and checks the tenant's rate limit.
5. The proxy fetches the real token from secret-manager, adds it, and opens its own HTTPS connection to api.github.com.
6. The proxy scrubs the response, writes the audit record and returns the result to gh.

- **Needs:** gh must accept a custom host over plain HTTP with a placeholder token. I haven't confirmed that.
- **Breaks for:** any tool that hardcodes its endpoint, because it bypasses the proxy and gets no token.
- No certificate is installed in the sandbox.

### Solution 2: the proxy decrypts the traffic and swaps the placeholder

1. At sandbox start, the controller sets a placeholder token and installs the proxy's public certificate in the sandbox's trust store. It also sets the proxy address in the network settings.
2. The agent runs `gh issue list`. gh calls api.github.com as normal, and the request is sent to the proxy.
3. The proxy decrypts it using a certificate for that host that it signs with its own key, which never leaves the proxy.
4. The proxy identifies the session and tenant from the sandbox's network address, checks the grant for the host and path, and checks the rate limit.
5. The proxy replaces the placeholder in the Authorization header with the real token from secret-manager and makes its own HTTPS call to GitHub.
6. The proxy scrubs the response, writes the audit record and returns the result.

- **Needs:** the tool must read the system trust store, or take an environment variable that points to the certificate.
- **Breaks for:** tools that pin certificates or ignore those settings. They fail closed.
- Works for hardcoded endpoints, since the proxy sees whatever request is sent.

### How they differ

Solution 1 avoids decrypting anything but only works for tools you can configure. Solution 2 works for any HTTPS tool that trusts the certificate, but the proxy decrypts all sandbox HTTPS traffic.

## One MCP tool call, step by step

The agent is configured with an MCP server (its URL) and the tools it may use. The credential for that server is stored in secret-manager under the tenant.

1. At the start of a session, the executor asks the MCP server which tools it offers, and passes those the agent is granted to the model.
2. The model asks for a tool, for example `create_issue`.
3. The executor checks that the agent is granted that tool and that the session hasn't passed its step cap, then logs the call.
4. The executor sends the MCP request to agent-network-proxy, which is the same path as an API tool.
5. The proxy checks that the destination is the granted MCP server, fetches the credential from secret-manager, adds it to the request, forwards it, scrubs the response and writes the audit record.
6. The result goes back to the executor, then into the session log and the model's context.

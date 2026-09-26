# Self-hosted host readiness

Run the read-only host report on the machine that will run Keel:

```bash
python3 tools/check_source_host.py
python3 tools/check_source_host.py --config /absolute/path/host-config.json --detailed
```

The default report invokes no subprocesses. The detailed report opts into two
fixed, three-second Node probes: `node --version` and a script that resolves the
installed Playwright entry point and reads its package metadata. It does not
import the Playwright package, launch Chromium, start Ollama, inspect a remote
endpoint, download weights, install software, or modify canonical state. Use a
trusted Node binary and operator-owned configuration. The probes drop Node
preload options and unrelated environment secrets, and invoke no shell.

The source CLI also exposes `python3 -m keel_sources doctor`. This checker is
independent of the source exporter: an installed browser cannot supply missing
source records, and complete evidence cannot prove that inference works.

The report separates these states:

| State | Meaning |
| --- | --- |
| AVAILABLE | The specific reported observation succeeded, such as an executable file existing. |
| MISSING | The searched executable or supplied configuration was absent. |
| NOT_CHECKED | The check was not performed. This is not a failed model or browser test. |
| UNVERIFIED | The evidence does not establish the claimed capability, or a probe was inconclusive. |

Overall readiness remains **UNVERIFIED**, and `execution_authorized` stays false.
Even if every dependency is AVAILABLE, real inference and the rendered local
fixture remain NOT_CHECKED. An exit status of zero means the report was produced;
it does not mean deployment is ready.

## Configuration

The `reviewers` list uses the existing 0.7 `ReviewerConfig` schema. Two to sixteen
unique reviewer IDs are required. Only literal loopback IPs, explicit ports, and
the backend's exact chat path are accepted. Invalid roster contents are not
echoed into the report. Validation says that the configuration is well formed;
it does not establish independent weights, quality, licensing, or a running server.

```json
{
  "reviewers": [
    {
      "reviewer_id": "reviewer_a",
      "backend": "ollama",
      "endpoint": "http://127.0.0.1:11434/api/chat",
      "model": "installed-model-a:tag"
    },
    {
      "reviewer_id": "reviewer_b",
      "backend": "llama_cpp",
      "endpoint": "http://127.0.0.1:8080/v1/chat/completions",
      "model": "installed-model-b"
    }
  ],
  "browser": {
    "node_path": "/absolute/path/to/node",
    "playwright_module": "/absolute/path/to/node_modules/playwright",
    "chromium_path": "/absolute/path/to/chromium"
  }
}
```

These model names and paths are placeholders, not installed dependencies. Omit
`browser` keys that you have not configured. With no `node_path`, the checker
looks for Node on PATH. With no module path, the detailed probe resolves the
package named `playwright` from the current working directory. With no Chromium
path, Chromium availability stays NOT_CHECKED; the checker does not guess a
version-specific cache path. The browser adapter can use its normal installed
Playwright browser selection separately.

The report reads the running Python version, OS, logical CPU count, affinity
where available, physical RAM, selected Linux root cgroup memory counters, and
free space on the current working filesystem. Root cgroup counters may not show
nested limits; available RAM, CPU quotas, GPU compatibility, and model fit remain
unverified. These numbers are observations, not hardware recommendations. Do not
select a model merely because its weight file is smaller than reported RAM:
runtime and context memory also matter.

## Complete the host-only checks

1. Select appropriately licensed model weights after checking the actual machine's
   available RAM, GPU memory, intended context size, and model requirements.
   Configure installed names, then run a real review against a small verified
   fixture. Preserve accuracy, latency, resource use, and failure results. The
   existing synthetic model fixture does not establish model quality.
2. Disable cloud behavior in the running model service. For Ollama,
   `OLLAMA_NO_CLOUD=1` must be in the server process environment; verify the
   server's configuration/logs after restarting that service. Setting it only in
   the shell that runs this doctor is insufficient. The report always marks the
   server setting UNVERIFIED. A loopback address cannot prove that its server
   does not forward requests; enforce outbound restrictions for an offline host.
   See [Ollama's local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features).
3. Install the intended Node/Playwright version and its matching Chromium build
   on the host using the official instructions. Playwright versions need matching
   browser binaries. In a project that already has Playwright installed, its
   browser installer is `npx --no-install playwright install chromium`. This
   manual command downloads browser files; the doctor never runs it. See
   [Playwright browser installation](https://playwright.dev/docs/browsers).
4. Run the existing rendered fixture rehearsal before adapting a real page:
   `python3 tools/local_application_fixture.py --rehearse`.
   Set `KEEL_PLAYWRIGHT_MODULE` and `KEEL_CHROMIUM_EXECUTABLE`, or pass
   `--node-path`, `--playwright-module`, and `--chromium-path` as needed. Preserve the
   actual result. A package manifest or executable file is not evidence that a
   page rendered, fields read back correctly, or a receipt matched.

No paid API, subscription, cloud browser, or model-download service is required
by this checker. Hardware, storage, electricity, and the selected software/model
licenses remain the host operator's responsibility.

# Coverage Brain · AgentForge on Daytona

Four agents help a broker review incoming client changes against dated policy evidence. **Watcher → Matcher → Closer → Scribe** are four separate model invocations. Each receives the original case evidence and every validated preceding packet.

The demo uses fictional clients and documents. It prepares a review packet; it does not determine coverage, send correspondence, or update a customer record.

## The workflow

| Agent | Responsibility | Handoff |
| --- | --- | --- |
| Watcher | Extract reported changes and unanswered questions | Dated events with original citations |
| Matcher | Compare those events against current and historical policy evidence | Comparisons, unresolved conflicts, verification requests |
| Closer | Draft the follow-up needed to resolve missing information | Unsent correspondence and a broker checklist |
| Scribe | Prepare record and learning proposals | Uncommitted proposals and explicit approval decisions |

The controller validates case/run/source identities, exact quotations, source dates, stage order, and draft/write status before advancing. A blocked stage stops the chain. Unresolved facts remain unresolved through downstream stages. Citation checks establish provenance; a person must still review the interpretation. Conflict propagation is conservative at the source-document level, so a proposal can remain unresolved even when its specific factual field is uncontested.

## A personal-agent workspace

The home screen is an **Updates** briefing: what needs attention, which drafts are ready, and what remains for the broker to decide. Client files and the four-agent activity are separate views; original evidence and full handoffs open only when needed.

The application monitors its configured demo case file every 30 seconds while the server is running. New source changes and completed review results appear in the feed automatically. Read/unread and one-hour snooze choices persist across restarts. Monitoring can be paused, and it does not start paid model runs. See [update behavior and design references](docs/UPDATES.md).

## Run locally

Requires Python 3.10+ and Node.js supported by the Codex CLI. The Python application has no external dependencies.

```sh
python3 -m unittest discover -s tests -v
python3 -m broker.cli preflight
npm install --prefix "$HOME/broker-tools" @openai/codex@0.163.0
"$HOME/broker-tools/node_modules/.bin/codex" login
python3 -m broker.server
```

Open `http://localhost:8765`. The `preflight` command performs deterministic evidence checks only. In **Agent activity**, use **Run a new review** to invoke the four agents and save the result. For a terminal-only alternative, `python3 -m broker.cli review` prints a result; it does not add a run to the dashboard. No canned agent output is substituted when a model request fails.

## Run in Daytona

Install the [official Daytona CLI](https://www.daytona.io/docs/en/tools/cli/) and authenticate through `daytona login`. This project uses the CLI's browser login rather than requiring a Daytona API key in the repository.

```sh
daytona login
python3 scripts/daytona_workspace.py create
python3 scripts/daytona_workspace.py sync
daytona exec broker-agent-demo -- 'npm install --prefix /home/daytona/broker-tools @openai/codex@0.163.0'
daytona ssh broker-agent-demo
```

Inside the sandbox, sign Codex in to your own eligible ChatGPT account:

```sh
/home/daytona/broker-tools/node_modules/.bin/codex login --device-auth
/home/daytona/broker-tools/node_modules/.bin/codex login status
exit
```

Then, back on your computer:

```sh
python3 scripts/daytona_workspace.py run preflight
daytona preview-url broker-agent-demo --port 8765 --expires 3600
```

Copy the returned origin (without a trailing slash) into `BROKER_PUBLIC_ORIGIN` below. Daytona forwards a different internal Host header; this explicit origin allows your dashboard's review requests while rejecting unrelated sites.

```sh
daytona exec broker-agent-demo -- 'cd /home/daytona/broker-agent && BROKER_RUNTIME=daytona BROKER_PUBLIC_ORIGIN=https://YOUR-PREVIEW-HOST nohup python3 -m broker.server --host 0.0.0.0 --port 8765 > /home/daytona/broker-server.log 2>&1 < /dev/null &'
```

The preview is private and its signed URL expires. Keep that URL out of commits. The sandbox stops after 10 idle minutes; use `daytona start broker-agent-demo` to resume it and restart the server if needed. Stop it explicitly with `daytona stop broker-agent-demo` when finished. Compute, storage, and ChatGPT plan limits still apply.

[Codex device authentication](https://learn.chatgpt.com/docs/auth) must be enabled for your account/workspace. You authorize this sandbox yourself; the deployment script never copies local Codex authentication files. A subscription sign-in uses that account's plan limits, not free or unlimited model access. API-based deployments can configure Codex separately; no provider secret belongs in this repository.

## What is shared and persisted

All roles share the selected case's versioned evidence and complete preceding handoffs. The web application persists run packets and review decisions under ignored `artifacts/`. Approval is a single packet-level review decision; separate per-draft and per-proposal decisions are not implemented. Approving a packet records the broker's review; email remains unsent and proposed customer-record changes remain unapplied. There is no live Cognee, insurer, email, CRM, or Arize connection in this version.

The included AP-001 regression fixture distinguishes the original roster from an endorsement effective before its receipt, and distinguishes reached start dates from planned ones. Its normalized-record grammar is deliberately narrow; it is not a general PDF/email extraction model. Real-document ingestion is a separate next step.

## Repository and deployment boundaries

- Source sync permits only selected code, prompts, static assets, and fictional fixtures, and verifies file hashes after transfer.
- Secrets, auth files, dotfiles, symlinks, local runtime data, and credentials are excluded.
- Model role calls run without shell, browser, or app action tools.
- The web server is a private single-user hackathon demo, not a public multi-tenant service.

## Provenance

The four-role concept, original handoff contract, and fictional broker-case direction come from the earlier AgentForge / Coverage Brain prototype and demo. This iteration implements a new Daytona runtime, Codex subscription sign-in path, strict four-stage controller, regression checks, and review interface. Prior work should be disclosed in the hackathon submission according to the organizer's reuse rules.

See the [three-minute demo guide](docs/DEMO.md) for the presentation flow and judging evidence.

MIT licensed. See [LICENSE](LICENSE).

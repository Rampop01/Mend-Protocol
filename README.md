# Mend Protocol

Mend fixes production crashes with a swarm of repair agents that are paid from an on-chain bounty escrow on NEAR. A human approves each fix from Telegram, the dashboard or the CLI, and nothing is paid or applied before that.

Built for NEAR HACKATHON: Build Beyond Boundaries. Tracks: **AI Swarms**, **Agent Economy**, **AI Security & Defense**.

![Dashboard showing a resolved incident, the guard's checks and the on-chain timeline](docs/dashboard.png)

## The problem

When a service crashes at night, monitoring tools page a human, who then has to find a laptop. Coding assistants can write the fix, but nothing ties their work to proof that the fix is correct, to a decision by the person accountable, or to payment for the work.

## What Mend does

1. The **SDK** (`mend.sdk`, no dependencies) reports an unhandled exception to the Mend server.
2. The **operator** account opens an incident on the escrow contract, which reserves a bounty from the repository's treasury.
3. The **recon agent** finds the failing statement in the AST and runs the test suite unpatched to record which tests already fail. It commits the hash of its report on-chain.
4. Two **forge agents** compete to fix it. `forge-rules` applies deterministic AST rewrites and declines when no rule fits. `forge-llm` asks a language model (NEAR AI Cloud by default) for exact search/replace edits. Each commits its patch hash and test results on-chain from its own account.
5. The **guard agent** screens each patch statically before it is ever executed, then re-runs the test suite itself in a fresh copy of the project and attests a score on-chain.
6. A **human** sees the winning diff and the evidence, and approves. The owner account calls `approve`, the contract pays the forge, guard and recon agents (70 / 15 / 15), the patch is applied, and a pull request is opened.

If no patch passes, or the human rejects, the bounty returns to the treasury.

## What NEAR does here

- **The escrow contract** ([contracts/bounty_escrow/src/lib.rs](contracts/bounty_escrow/src/lib.rs)) holds each repository's treasury, enforces a per-incident spending limit, and only pays when three conditions hold on-chain: the forge agent reported a clean test run, the guard attested a pass for that exact patch hash, and the repository owner approved that same hash.
- **Each agent is its own account** (`recon.<owner>`, `forge-rules.<owner>`, `forge-llm.<owner>`, `guard.<owner>`), so the chain records who did what and who earned what. The contract rejects a guard that is also a forge agent.
- **Agent keys are function-call access keys** limited to one contract method each with a gas allowance. A leaked agent key cannot move the agent's earnings or touch the treasury. The server's operator key can open and reject incidents but cannot approve a payout.

Live deployment on testnet: [`escrow.mend-cb4c33d5.testnet`](https://testnet.nearblocks.io/address/escrow.mend-cb4c33d5.testnet). Accounts and setup transactions are in [deployments/testnet.json](deployments/testnet.json).

A complete incident from that deployment, `INC-AB17448DA3`:

| Step | Signed by | Transaction |
| --- | --- | --- |
| Open incident, reserve 0.25 NEAR | operator | [C6RDsi…](https://testnet.nearblocks.io/txns/C6RDsiRpBQo5yWdgTg27R7LKouriAh45gRBdJKQ8VGKG) |
| Commit recon report hash | recon | [DDKBB2…](https://testnet.nearblocks.io/txns/DDKBB2JzWL6CTG4SrVksaryVyZWkHEheHnZ5oK4BrtsV) |
| Commit patch hash and test results | forge-rules | [2LKrNQ…](https://testnet.nearblocks.io/txns/2LKrNQPsc65vdTr1ubBq6PBX5TnTCMsg2t7QXZ25XPmz) |
| Attest the patch, score 100 | guard | [2UCF77…](https://testnet.nearblocks.io/txns/2UCF77HyNRMz8i33QrLTqjRay6x7TwkV2JgMX5h7cWVj) |
| Approve and pay 0.175 / 0.0375 / 0.0375 NEAR | owner | [43RSkH…](https://testnet.nearblocks.io/txns/43RSkHs7tYdmpi6UqVJnwNe2anhf9DVj2ZvChYCK7Wkk) |

## Try it

Requires Python 3.9+ on macOS or Linux. Rust is only needed to rebuild the contract; a built wasm is included.

```bash
git clone https://github.com/Rampop01/mend-protocol.git && cd mend-protocol
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Run the tests

```bash
python -m unittest discover -s tests -t .
```

Expect `Ran 52 tests` and `OK`. These run the whole pipeline against a temporary project, including a hostile LLM patch that must be blocked before it executes.

### 2. Create your own testnet deployment

```bash
python -m mend setup
```

Expect about ten lines ending in `Setup complete. Contract: https://testnet.nearblocks.io/address/escrow.mend-….testnet`. This creates a funded owner account from the testnet faucet, deploys the contract, creates the agent accounts with scoped keys, and deposits 2 NEAR into the treasury. Keys are written to `.mend/testnet.json`, which is git-ignored. It takes about a minute and resumes if interrupted.

You can skip this step. Mend then runs without escrow and every incident says "No on-chain escrow" instead of showing a receipt.

### 3. Start the server

```bash
python -m mend serve
```

Expect log lines stating which integrations are on, ending with `Listening on http://127.0.0.1:8770`. Open that address to see the dashboard with the treasury balance and four agents.

### 4. Crash the example service

In a second terminal, with the virtualenv active:

```bash
python examples/payment_service/app.py
```

Expect `Checkout crashed: ZeroDivisionError: float division by zero`. Within about 15 seconds the dashboard shows the incident as `AWAITING APPROVAL`, with the diff, the guard's checks and a link for each transaction. `python -m mend incidents` prints the incident id.

### 5. Approve the fix

Click **Approve and release 0.25 NEAR** in the dashboard, or:

```bash
python -m mend approve INC-XXXXXXXXXX
```

Expect a settlement transaction link and three payout lines (forge 0.175, guard 0.0375, recon 0.0375 NEAR). `examples/payment_service/app.py` now contains the fix and `python -m mend status` shows the treasury at 1.75 NEAR free and each agent's earnings.

To run it again, restore the bug with `git checkout examples/payment_service/app.py`.

### Optional integrations

Copy `.env.example` to `.env` and set any of these. The server logs which are active at startup.

| Variable | Effect |
| --- | --- |
| `NEAR_AI_API_KEY` | Enables the `forge-llm` agent through NEAR AI Cloud. Without it that agent records "No LLM API key configured" and only `forge-rules` competes. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Sends each incident to Telegram as one message that updates in place, with Approve and Reject buttons. Send `/start` to the bot to get the ids. `TELEGRAM_APPROVER_IDS` restricts who may approve. |
| `GITHUB_TOKEN` | Opens a pull request for each approved fix, only if the file on the base branch matches the file the patch was verified against. |
| `MEND_INGEST_TOKEN` | Bearer token for the API. Required to listen on anything other than loopback. |

### Watch your own code

List the project in `mend.projects.json` on the server (its root directory and its test command), then:

```python
from mend.sdk import MendWatch

watch = MendWatch(repo="your-org/your-repo")

@watch
def handle_order(order): ...
```

The test command is read from the server's configuration, never from an incident report.

## How the guard scores a patch

A patch is blocked before execution if it introduces a call or import on the deny list (`eval`, `exec`, `os.system`, `subprocess`, sockets and similar), adds a catch-all handler that discards the error, contains a credential-shaped string, edits a test file, or changes more than 60 lines. Only constructs the patch adds are counted, so existing code is not held against it.

A patch that passes the gate is scored out of 100:

| Check | Points |
| --- | --- |
| The guard's own run of the test suite passes | 40 |
| A test that failed before the patch now passes | 20 |
| Change is 10 lines or fewer (scaled down to 0 at 60) | 15 |
| Edits stay inside the function that crashed | 10 |
| Static gate clean | 10 |
| The guard's test counts match what the forge agent reported | 5 |

The highest score wins; ties go to the smaller diff.

## Limits

- **Python only.** Recon and the rule-based forge agent work on Python ASTs.
- **Three deterministic rules:** division by zero, a missing dictionary key, and attribute or item access on `None`. Anything else needs the LLM forge agent.
- **The sandbox is a temporary copy, not a security boundary.** Tests run as the same OS user with secrets removed from the environment. Run Mend in a container if you point it at code you do not trust.
- **The server holds the owner key** so that an approval from Telegram can release funds. In production the owner would sign `approve` from their own wallet.
- **Testnet.** The contract has unit tests but no audit.
- **Same-machine paths.** The SDK reports file paths from the running process and the server maps them onto its registered project roots.
- With an LLM key set, the crashing file's source is sent to the configured LLM provider.

## Repository layout

```text
contracts/bounty_escrow/   Rust escrow contract and its 14 unit tests
mend/sdk/                  crash reporter for watched applications
mend/swarm/                recon, forge and guard agents, and the orchestrator
mend/integrations/         NEAR transaction signing, escrow client, GitHub, LLM
mend/bot/                  Telegram control plane
mend/server.py             HTTP API and dashboard
mend/cli.py                setup, serve, status, approve, reject
examples/payment_service/  example service with a real bug and a test suite
tests/                     52 Python tests
```

Rebuild the contract with `contracts/build.sh` (needs `rustup`; uses `wasm-opt` if installed) and test it with `cargo test` in `contracts/bounty_escrow`.

## Hackathon disclosure

- All code was written between 7 and 14 November 2026, inside the hackathon window. There is no earlier baseline.
- Libraries: [near-sdk](https://crates.io/crates/near-sdk) for the contract and [PyNaCl](https://pypi.org/project/PyNaCl/) for ed25519 signing. Transaction encoding, the RPC client and everything else use the Python standard library.
- AI coding assistants were used to write parts of this code.

## License

MIT. See [LICENSE](LICENSE).

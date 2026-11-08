# Mend Protocol

**Fix production bugs from your phone while you sleep — powered by an autonomous AI swarm & NEAR Protocol.**

[![NEAR Hackathon](https://img.shields.io/badge/NEAR%20Hackathon-Build%20Beyond%20Boundaries-black?style=flat-square&logo=near)](https://near.org)
[![NEAR Prize Pool](https://img.shields.io/badge/Prize%20Pool-%24250%2C000-00EC97?style=flat-square&logo=near)](https://near.org)
[![Track](https://img.shields.io/badge/Track-AI%20Swarms%20%26%20Agent%20Economy-black?style=flat-square)](https://near.org)
[![Telegram Bot](https://img.shields.io/badge/Telegram-@mend__protocol__bot-blue?style=flat-square&logo=telegram)](https://t.me/mend_protocol_bot)
[![Network](https://img.shields.io/badge/Network-NEAR%20Testnet-00EC97?style=flat-square)](https://testnet.nearblocks.io)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?style=flat-square&logo=python)](https://python.org)

---

## ⚡ Executive Summary

Production outages cost internet businesses billions every year. Modern observability platforms (Datadog, Sentry, PagerDuty) only **alert** humans when code breaks. Meanwhile, developer coding agents (Claude Code, Cursor) remain trapped inside local developer laptop terminals.

**Mend Protocol** introduces an **Autonomous SRE Swarm powered by NEAR Protocol**:
1. **Multi-Agent Coordination Swarm**: A coordinated pipeline of specialized agents (`@mend-recon`, `@mend-forge`, `@mend-guard`) triage crashes, perform AST stack forensics, synthesize surgical Git patches, and execute real unit test suites in sandboxes.
2. **On-Chain Agent Economy & Treasury**: Powered by **NEAR Protocol**, repository owners lock autonomous micro-bounties in escrow. When an agent swarm successfully repairs a production bug and passes 100% of unit tests, the bounty (0.25 NEAR) is automatically disbursed on-chain and registered with an immutable transaction digest on NEAR Testnet (`testnet.nearblocks.io`).
3. **Multi-Tenant GitHub Integration**: Anyone can connect their own repository in Telegram via `/repo owner/repo [token]`. Mend will create dedicated remote hotfix branches, commit AST-verified patches, and automatically open Pull Requests with full forensic reports.
4. **Mobile-First Telegram HUD (`@mend_protocol_bot`)**: Real-time in-place status cards with expandable code blockquotes (`<blockquote expandable>`), zero-downtime hot-reloading, and one-tap mobile approval buttons.

---

## 🏗 Architecture & Agent Pipeline

```
  [ Production Web Service / API Crashes ]
                     │
                     ▼ (Unhandled HTTP 500)
    ┌──────────────────────────────────┐
    │  @mend-recon                     │  Parses runtime stack frames & Python Abstract Syntax
    │  (AST & Stack Forensics Agent)   │  Tree (AST) on disk to pinpoint crashing statement.
    └────────────────┬─────────────────┘
                     │
                     ▼ (Hands off root-cause context)
    ┌──────────────────────────────────┐
    │  @mend-forge                     │  Synthesizes minimal unified git diff and executes
    │  (Patch & Unittest Agent)        │  the real unittest suite (proves 4/4 passing tests).
    └────────────────┬─────────────────┘
                     │
                     ▼ (Hands off candidate diff)
    ┌──────────────────────────────────┐
    │  @mend-guard                     │  Runs Security Guardrails (AST security gate, DLP
    │  (Safety & On-Chain Agent)       │  secret check, blast radius) + Locks 0.25 NEAR Bounty.
    └────────────────┬─────────────────┘
                     │
                     ▼ (Presents interactive HUD card on Telegram)
    ┌──────────────────────────────────┐
    │  On-Call Human Operator          │  Reviews diff & test run on Telegram and taps:
    │  (@mend_protocol_bot)            │  [ 🚀 Approve & Release NEAR Bounty ]
    └────────────────┬─────────────────┘
                     │
                     ├───────────────────────────────────────────────┐
                     ▼ (0ms Hot-Swap)                                ▼ (On-Chain Bounty)
    ┌──────────────────────────────────┐           ┌──────────────────────────────────┐
    │  In-Memory Hot Reload            │           │  NEAR Testnet RPC                │
    │  - Python AST reloaded live      │           │  - 0.25 NEAR bounty disbursed    │
    │  - Failed transaction replayed   │           │  - On-chain proof registered     │
    │  - Zero production downtime      │           │  - Viewable on NearBlocks        │
    └──────────────────────────────────┘           └──────────────────────────────────┘
                     │
                     ▼
    ┌──────────────────────────────────┐
    │  Remote GitHub Push & PR         │
    │  - Creates branch: `hotfix/inc-*`│
    │  - Commits verified surgical fix │
    │  - Opens Pull Request with tests │
    └──────────────────────────────────┘
```

---

## Ⓝ NEAR Protocol Integration

Mend Protocol directly leverages NEAR Testnet:
- **Live RPC Integration**: Connects via `https://rpc.testnet.near.org` to poll block heights, network sync, and validator states.
- **Autonomous Agent Bounties**: When an incident occurs, a micro-bounty intent (0.25 NEAR) is calculated based on incident complexity.
- **On-Chain Verification Receipt**: When the human approves the fix, the bounty is finalized with a cryptographically signed transaction hash linkable on the NEAR block explorer (`https://testnet.nearblocks.io/tx/<tx_hash>`).
- **Autonomous Treasury Account**: Configurable per user (`/near <account_id>.testnet`) so individual developers or enterprises can fund self-healing agents from their own DAO or multisig treasury.

---

## 🚀 Quickstart & Telegram Bot

### 1. Chat with Live Bot
Open Telegram and start the bot:  
👉 **[@mend_protocol_bot](https://t.me/mend_protocol_bot)**

Available commands & keyboard controls:
| Command | Button | Description |
|---|---|---|
| `/start` | — | Initialize Mend Swarm & inspect environment |
| `/trigger` | 🚨 Trigger Incident | Simulate production 500 runtime crash |
| `/tests` | 🧪 Run Test Suite | Execute sandbox unittest suite & AST checks |
| `/ship` | 🚀 Ship Hotfix | Deploy 0ms hot-swap & release NEAR bounty |
| `/reset` | 🔄 Reset Baseline | Restore unpatched baseline to test again |
| `/repo` | 🐙 Connect GitHub | Link custom GitHub repo: `/repo owner/name [token]` |
| `/near` | Ⓝ NEAR Treasury | Inspect NEAR testnet sync & agent bounty status |
| `/profile` | — | View active developer profile & wallet |

### 2. Run Locally

```bash
# Clone the repository
git clone https://github.com/Rampop01/mend-protocol.git
cd mend-protocol

# Configure environment
cp .env.example .env
# Fill in your TELEGRAM_BOT_TOKEN from @BotFather

# Run unittests
python3 -m unittest discover -s tests -v

# Start Mend Protocol Server on port 8770
python3 server.py
```

---

## 🧪 Real Unittests (Zero Mocks)

Mend does not use simulated outputs. All tests execute the real Python `unittest` runner:

```bash
$ python3 -m unittest discover -s tests -v
test_full_voucher_zero_net_settlement_regression (test_checkout_engine.TestCheckoutSettlementEngine) ... ok
test_multi_currency_ngn_and_kes_exponent_regression (test_checkout_engine.TestCheckoutSettlementEngine) ... ok
test_partial_discount_eur_settlement (test_checkout_engine.TestCheckoutSettlementEngine) ... ok
test_standard_usd_settlement (test_checkout_engine.TestCheckoutSettlementEngine) ... ok

----------------------------------------------------------------------
Ran 4 tests in 0.005s

OK
```

---

## 👥 Built For

**NEAR Protocol Hackathon: Build Beyond Boundaries**  
- **Track 1:** AI Swarms & Multi-Agent Coordination  
- **Track 2:** AI Security & Defense / Autonomous SRE  
- **Track 3:** Autonomous Business & On-Chain Agent Economy  

*Developed by Rampop (@Rampop01)*

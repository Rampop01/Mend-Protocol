//! Mend Protocol bounty escrow.
//!
//! A repository owner funds a treasury and allowlists a swarm of agent accounts.
//! When production crashes, the operator opens an incident, which reserves a
//! bounty. Each agent then signs its own work on-chain with its own account:
//!
//!   recon  -> `submit_recon`  (hash of the forensic report)
//!   forge  -> `submit_patch`  (hash of the candidate patch + test evidence)
//!   guard  -> `attest`        (independent security verdict on one patch)
//!
//! Several forge agents may compete on one incident. Nothing is paid until the
//! repository owner calls `approve`, naming the winning forge agent and the
//! exact patch hash they reviewed. The bounty is then split between the agents
//! that produced the accepted repair. `reject` returns the bounty to the treasury.

use near_sdk::json_types::U64;
use near_sdk::store::{IterableMap, LookupMap};
use near_sdk::{env, near, require, AccountId, BorshStorageKey, NearToken, PanicOnDefault, Promise};

/// Share of the bounty paid to the winning forge agent, in basis points.
pub const FORGE_BPS: u128 = 7_000;
/// Share paid to the recon agent whose report the incident carries.
pub const RECON_BPS: u128 = 1_500;
/// Share paid to the guard agent that attested the winning patch.
pub const GUARD_BPS: u128 = 1_500;

const MIN_REPO_DEPOSIT: NearToken = NearToken::from_millinear(100);
const MAX_FORGES_PER_REPO: usize = 8;
const MAX_ID_LEN: usize = 64;
const HASH_LEN: usize = 64;

#[near]
#[derive(BorshStorageKey)]
enum StorageKey {
    Repos,
    Incidents,
    Agents,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, PartialEq)]
pub enum IncidentStatus {
    Open,
    Resolved,
    Rejected,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Repo {
    /// The human (or DAO) that funds the treasury and approves payouts.
    pub owner: AccountId,
    /// The service account allowed to open incidents against this treasury.
    pub operator: AccountId,
    /// Unreserved funds available for new bounties.
    pub balance: NearToken,
    /// Funds locked in open incidents.
    pub reserved: NearToken,
    /// Spending limit: the largest bounty the operator may reserve per incident.
    pub max_bounty: NearToken,
    pub recon_agent: Option<AccountId>,
    pub guard_agent: Option<AccountId>,
    pub forge_agents: Vec<AccountId>,
    pub incidents_opened: u32,
    pub incidents_resolved: u32,
    pub total_paid: NearToken,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct ReconEntry {
    pub agent: AccountId,
    pub report_hash: String,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Attestation {
    pub guard: AccountId,
    pub passed: bool,
    /// 0..=100, computed by the guard agent from its checks.
    pub score: u8,
    pub report_hash: String,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Submission {
    pub forge: AccountId,
    pub patch_hash: String,
    pub tests_run: u32,
    pub tests_failed: u32,
    pub attestation: Option<Attestation>,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Payout {
    pub account: AccountId,
    pub role: String,
    pub amount: NearToken,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Incident {
    pub incident_id: String,
    pub repo_id: String,
    pub crash_hash: String,
    pub bounty: NearToken,
    pub status: IncidentStatus,
    pub opened_at: U64,
    pub resolved_at: Option<U64>,
    pub recon: Option<ReconEntry>,
    pub submissions: Vec<Submission>,
    pub winner: Option<AccountId>,
    pub accepted_patch_hash: Option<String>,
    pub payouts: Vec<Payout>,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, Default)]
pub struct AgentStats {
    pub recon_reports: u32,
    pub patches_submitted: u32,
    pub patches_accepted: u32,
    pub attestations: u32,
    pub earned: NearToken,
}

#[near(serializers = [json])]
pub struct Stats {
    pub repos: u32,
    pub incidents: u32,
    pub incidents_resolved: u32,
    pub total_paid: NearToken,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct MendBountyEscrow {
    repos: IterableMap<String, Repo>,
    incidents: IterableMap<String, Incident>,
    agents: LookupMap<AccountId, AgentStats>,
    incidents_resolved: u32,
    total_paid: NearToken,
}

fn emit(event: &str, data: String) {
    env::log_str(&format!(
        "EVENT_JSON:{{\"standard\":\"mend\",\"version\":\"1.0.0\",\"event\":\"{event}\",\"data\":[{data}]}}"
    ));
}

fn check_id(label: &str, value: &str) {
    require!(
        !value.is_empty() && value.len() <= MAX_ID_LEN,
        format!("{label} must be 1..={MAX_ID_LEN} characters")
    );
    require!(
        value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'/')),
        format!("{label} contains unsupported characters")
    );
}

fn check_hash(label: &str, value: &str) {
    require!(
        value.len() == HASH_LEN && value.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')),
        format!("{label} must be a lowercase hex SHA-256 digest")
    );
}

fn share(bounty: NearToken, bps: u128) -> NearToken {
    NearToken::from_yoctonear(bounty.as_yoctonear() * bps / 10_000)
}

#[near]
impl MendBountyEscrow {
    #[init]
    pub fn new() -> Self {
        Self {
            repos: IterableMap::new(StorageKey::Repos),
            incidents: IterableMap::new(StorageKey::Incidents),
            agents: LookupMap::new(StorageKey::Agents),
            incidents_resolved: 0,
            total_paid: NearToken::from_yoctonear(0),
        }
    }

    // ------------------------------------------------------------------
    // Repository owner: treasury and policy
    // ------------------------------------------------------------------

    /// Creates a repository treasury owned by the caller. The attached deposit
    /// funds it. `max_bounty` caps what the operator can reserve per incident.
    #[payable]
    pub fn register_repo(&mut self, repo_id: String, operator: AccountId, max_bounty: NearToken) {
        check_id("repo_id", &repo_id);
        require!(self.repos.get(&repo_id).is_none(), "Repository already registered");
        let deposit = env::attached_deposit();
        require!(
            deposit >= MIN_REPO_DEPOSIT,
            "Attach at least 0.1 NEAR to register a repository"
        );
        let owner = env::predecessor_account_id();
        self.repos.insert(
            repo_id.clone(),
            Repo {
                owner: owner.clone(),
                operator,
                balance: deposit,
                reserved: NearToken::from_yoctonear(0),
                max_bounty,
                recon_agent: None,
                guard_agent: None,
                forge_agents: Vec::new(),
                incidents_opened: 0,
                incidents_resolved: 0,
                total_paid: NearToken::from_yoctonear(0),
            },
        );
        emit(
            "repo_registered",
            format!("{{\"repo_id\":\"{repo_id}\",\"owner\":\"{owner}\",\"deposit\":\"{}\"}}", deposit.as_yoctonear()),
        );
    }

    /// Anyone may top up a repository treasury.
    #[payable]
    pub fn deposit(&mut self, repo_id: String) {
        let deposit = env::attached_deposit();
        require!(deposit.as_yoctonear() > 0, "Attach a deposit");
        let repo = self.repos.get_mut(&repo_id).expect("Unknown repository");
        repo.balance = repo.balance.saturating_add(deposit);
        emit(
            "treasury_deposit",
            format!("{{\"repo_id\":\"{repo_id}\",\"amount\":\"{}\"}}", deposit.as_yoctonear()),
        );
    }

    /// Owner withdraws unreserved treasury funds.
    pub fn withdraw(&mut self, repo_id: String, amount: NearToken) -> Promise {
        let repo = self.repos.get_mut(&repo_id).expect("Unknown repository");
        require!(env::predecessor_account_id() == repo.owner, "Only the repository owner can withdraw");
        require!(repo.balance >= amount, "Amount exceeds the unreserved treasury balance");
        repo.balance = repo.balance.saturating_sub(amount);
        Promise::new(repo.owner.clone()).transfer(amount)
    }

    /// Owner sets the agent allowlist. Only these accounts can sign work for
    /// this repository, and only these accounts can be paid from its treasury.
    pub fn set_agents(
        &mut self,
        repo_id: String,
        recon_agent: AccountId,
        guard_agent: AccountId,
        forge_agents: Vec<AccountId>,
    ) {
        let repo = self.repos.get_mut(&repo_id).expect("Unknown repository");
        require!(env::predecessor_account_id() == repo.owner, "Only the repository owner can set agents");
        require!(
            !forge_agents.is_empty() && forge_agents.len() <= MAX_FORGES_PER_REPO,
            "Provide between 1 and 8 forge agents"
        );
        require!(
            !forge_agents.contains(&guard_agent),
            "The guard must be independent of every forge agent"
        );
        repo.recon_agent = Some(recon_agent);
        repo.guard_agent = Some(guard_agent);
        repo.forge_agents = forge_agents;
        emit("agents_set", format!("{{\"repo_id\":\"{repo_id}\"}}"));
    }

    /// Owner updates the operator account and the per-incident spending limit.
    pub fn set_policy(&mut self, repo_id: String, operator: AccountId, max_bounty: NearToken) {
        let repo = self.repos.get_mut(&repo_id).expect("Unknown repository");
        require!(env::predecessor_account_id() == repo.owner, "Only the repository owner can set policy");
        repo.operator = operator;
        repo.max_bounty = max_bounty;
    }

    // ------------------------------------------------------------------
    // Operator: incident lifecycle
    // ------------------------------------------------------------------

    /// Opens an incident and reserves `bounty` from the repository treasury.
    pub fn open_incident(&mut self, incident_id: String, repo_id: String, crash_hash: String, bounty: NearToken) {
        check_id("incident_id", &incident_id);
        check_hash("crash_hash", &crash_hash);
        require!(self.incidents.get(&incident_id).is_none(), "Incident already exists");
        let repo = self.repos.get_mut(&repo_id).expect("Unknown repository");
        require!(
            env::predecessor_account_id() == repo.operator,
            "Only the repository operator can open incidents"
        );
        require!(repo.guard_agent.is_some(), "Set the agent allowlist before opening incidents");
        require!(bounty.as_yoctonear() > 0, "Bounty must be positive");
        require!(bounty <= repo.max_bounty, "Bounty exceeds the repository spending limit");
        require!(repo.balance >= bounty, "Insufficient treasury balance for this bounty");

        repo.balance = repo.balance.saturating_sub(bounty);
        repo.reserved = repo.reserved.saturating_add(bounty);
        repo.incidents_opened += 1;

        self.incidents.insert(
            incident_id.clone(),
            Incident {
                incident_id: incident_id.clone(),
                repo_id: repo_id.clone(),
                crash_hash: crash_hash.clone(),
                bounty,
                status: IncidentStatus::Open,
                opened_at: U64(env::block_timestamp()),
                resolved_at: None,
                recon: None,
                submissions: Vec::new(),
                winner: None,
                accepted_patch_hash: None,
                payouts: Vec::new(),
            },
        );
        emit(
            "incident_opened",
            format!(
                "{{\"incident_id\":\"{incident_id}\",\"repo_id\":\"{repo_id}\",\"crash_hash\":\"{crash_hash}\",\"bounty\":\"{}\"}}",
                bounty.as_yoctonear()
            ),
        );
    }

    // ------------------------------------------------------------------
    // Agents: each signs its own work
    // ------------------------------------------------------------------

    /// Recon agent commits the hash of its forensic report.
    pub fn submit_recon(&mut self, incident_id: String, report_hash: String) {
        check_hash("report_hash", &report_hash);
        let agent = env::predecessor_account_id();
        let incident = self.incidents.get_mut(&incident_id).expect("Unknown incident");
        require!(incident.status == IncidentStatus::Open, "Incident is not open");
        require!(incident.recon.is_none(), "Recon report already submitted");
        let repo = self.repos.get(&incident.repo_id).expect("Unknown repository");
        require!(repo.recon_agent.as_ref() == Some(&agent), "Caller is not this repository's recon agent");
        incident.recon = Some(ReconEntry { agent: agent.clone(), report_hash: report_hash.clone() });

        let mut stats = self.agents.get(&agent).cloned().unwrap_or_default();
        stats.recon_reports += 1;
        self.agents.insert(agent.clone(), stats);
        emit(
            "recon_submitted",
            format!("{{\"incident_id\":\"{incident_id}\",\"agent\":\"{agent}\",\"report_hash\":\"{report_hash}\"}}"),
        );
    }

    /// A forge agent commits a candidate patch and its test evidence.
    /// One submission per forge agent per incident.
    pub fn submit_patch(&mut self, incident_id: String, patch_hash: String, tests_run: u32, tests_failed: u32) {
        check_hash("patch_hash", &patch_hash);
        require!(tests_failed <= tests_run, "tests_failed cannot exceed tests_run");
        let agent = env::predecessor_account_id();
        let incident = self.incidents.get_mut(&incident_id).expect("Unknown incident");
        require!(incident.status == IncidentStatus::Open, "Incident is not open");
        let repo = self.repos.get(&incident.repo_id).expect("Unknown repository");
        require!(repo.forge_agents.contains(&agent), "Caller is not an allowlisted forge agent");
        require!(
            incident.submissions.iter().all(|s| s.forge != agent),
            "This forge agent already submitted a patch"
        );
        incident.submissions.push(Submission {
            forge: agent.clone(),
            patch_hash: patch_hash.clone(),
            tests_run,
            tests_failed,
            attestation: None,
        });

        let mut stats = self.agents.get(&agent).cloned().unwrap_or_default();
        stats.patches_submitted += 1;
        self.agents.insert(agent.clone(), stats);
        emit(
            "patch_submitted",
            format!(
                "{{\"incident_id\":\"{incident_id}\",\"forge\":\"{agent}\",\"patch_hash\":\"{patch_hash}\",\"tests_run\":{tests_run},\"tests_failed\":{tests_failed}}}"
            ),
        );
    }

    /// The guard agent attests one forge agent's patch. The patch hash must
    /// match what the forge agent committed, so a guard cannot attest a patch
    /// other than the one on record.
    pub fn attest(
        &mut self,
        incident_id: String,
        forge: AccountId,
        patch_hash: String,
        passed: bool,
        score: u8,
        report_hash: String,
    ) {
        check_hash("report_hash", &report_hash);
        require!(score <= 100, "score must be 0..=100");
        let agent = env::predecessor_account_id();
        let incident = self.incidents.get_mut(&incident_id).expect("Unknown incident");
        require!(incident.status == IncidentStatus::Open, "Incident is not open");
        let repo = self.repos.get(&incident.repo_id).expect("Unknown repository");
        require!(repo.guard_agent.as_ref() == Some(&agent), "Caller is not this repository's guard agent");
        let submission = incident
            .submissions
            .iter_mut()
            .find(|s| s.forge == forge)
            .expect("No submission from that forge agent");
        require!(submission.patch_hash == patch_hash, "Patch hash does not match the submission");
        require!(submission.attestation.is_none(), "Submission already attested");
        submission.attestation = Some(Attestation {
            guard: agent.clone(),
            passed,
            score,
            report_hash: report_hash.clone(),
        });

        let mut stats = self.agents.get(&agent).cloned().unwrap_or_default();
        stats.attestations += 1;
        self.agents.insert(agent.clone(), stats);
        emit(
            "patch_attested",
            format!(
                "{{\"incident_id\":\"{incident_id}\",\"guard\":\"{agent}\",\"forge\":\"{forge}\",\"passed\":{passed},\"score\":{score}}}"
            ),
        );
    }

    // ------------------------------------------------------------------
    // Repository owner: settlement
    // ------------------------------------------------------------------

    /// Owner approves one forge agent's patch and releases the bounty.
    /// `patch_hash` binds the approval to the exact patch the owner reviewed.
    /// Requires a clean test run and a passing guard attestation.
    pub fn approve(&mut self, incident_id: String, forge: AccountId, patch_hash: String) {
        let incident = self.incidents.get_mut(&incident_id).expect("Unknown incident");
        require!(incident.status == IncidentStatus::Open, "Incident is not open");
        let repo = self.repos.get_mut(&incident.repo_id).expect("Unknown repository");
        require!(
            env::predecessor_account_id() == repo.owner,
            "Only the repository owner can approve a payout"
        );
        let submission = incident
            .submissions
            .iter()
            .find(|s| s.forge == forge)
            .expect("No submission from that forge agent");
        require!(submission.patch_hash == patch_hash, "Patch hash does not match the submission");
        require!(
            submission.tests_run > 0 && submission.tests_failed == 0,
            "Patch does not have a clean test run"
        );
        let attestation = submission.attestation.as_ref().expect("Patch has not been attested by the guard");
        require!(attestation.passed, "Guard rejected this patch");

        let bounty = incident.bounty;
        let guard_cut = share(bounty, GUARD_BPS);
        let recon_cut = match incident.recon {
            Some(_) => share(bounty, RECON_BPS),
            None => NearToken::from_yoctonear(0),
        };
        // The forge agent receives the remainder, so the split always sums to the bounty.
        let forge_cut = bounty.saturating_sub(guard_cut).saturating_sub(recon_cut);

        let mut payouts = vec![
            Payout { account: forge.clone(), role: "forge".to_string(), amount: forge_cut },
            Payout { account: attestation.guard.clone(), role: "guard".to_string(), amount: guard_cut },
        ];
        if let Some(recon) = &incident.recon {
            payouts.push(Payout { account: recon.agent.clone(), role: "recon".to_string(), amount: recon_cut });
        }

        incident.status = IncidentStatus::Resolved;
        incident.resolved_at = Some(U64(env::block_timestamp()));
        incident.winner = Some(forge.clone());
        incident.accepted_patch_hash = Some(patch_hash.clone());
        incident.payouts = payouts.clone();

        repo.reserved = repo.reserved.saturating_sub(bounty);
        repo.incidents_resolved += 1;
        repo.total_paid = repo.total_paid.saturating_add(bounty);
        self.incidents_resolved += 1;
        self.total_paid = self.total_paid.saturating_add(bounty);

        for payout in &payouts {
            let mut stats = self.agents.get(&payout.account).cloned().unwrap_or_default();
            stats.earned = stats.earned.saturating_add(payout.amount);
            if payout.role == "forge" {
                stats.patches_accepted += 1;
            }
            self.agents.insert(payout.account.clone(), stats);
            if payout.amount.as_yoctonear() > 0 {
                let _ = Promise::new(payout.account.clone()).transfer(payout.amount);
            }
        }

        emit(
            "incident_resolved",
            format!(
                "{{\"incident_id\":\"{incident_id}\",\"winner\":\"{forge}\",\"patch_hash\":\"{patch_hash}\",\"bounty\":\"{}\"}}",
                bounty.as_yoctonear()
            ),
        );
    }

    /// Owner or operator closes an incident without paying. The bounty
    /// returns to the repository treasury.
    pub fn reject(&mut self, incident_id: String) {
        let incident = self.incidents.get_mut(&incident_id).expect("Unknown incident");
        require!(incident.status == IncidentStatus::Open, "Incident is not open");
        let repo = self.repos.get_mut(&incident.repo_id).expect("Unknown repository");
        let caller = env::predecessor_account_id();
        require!(
            caller == repo.owner || caller == repo.operator,
            "Only the repository owner or operator can reject"
        );
        incident.status = IncidentStatus::Rejected;
        incident.resolved_at = Some(U64(env::block_timestamp()));
        repo.reserved = repo.reserved.saturating_sub(incident.bounty);
        repo.balance = repo.balance.saturating_add(incident.bounty);
        emit("incident_rejected", format!("{{\"incident_id\":\"{incident_id}\"}}"));
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    pub fn get_incident(&self, incident_id: String) -> Option<Incident> {
        self.incidents.get(&incident_id).cloned()
    }

    pub fn get_repo(&self, repo_id: String) -> Option<Repo> {
        self.repos.get(&repo_id).cloned()
    }

    pub fn get_agent(&self, account_id: AccountId) -> AgentStats {
        self.agents.get(&account_id).cloned().unwrap_or_default()
    }

    /// Most recent incidents first.
    pub fn list_incidents(&self, from_index: Option<u32>, limit: Option<u32>) -> Vec<Incident> {
        let total = self.incidents.len() as usize;
        let skip = from_index.unwrap_or(0) as usize;
        let take = limit.unwrap_or(20).min(50) as usize;
        self.incidents
            .values()
            .rev()
            .skip(skip.min(total))
            .take(take)
            .cloned()
            .collect()
    }

    pub fn get_stats(&self) -> Stats {
        Stats {
            repos: self.repos.len(),
            incidents: self.incidents.len(),
            incidents_resolved: self.incidents_resolved,
            total_paid: self.total_paid,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::VMContextBuilder;
    use near_sdk::testing_env;

    const REPO: &str = "acme/payments";
    const H1: &str = "1111111111111111111111111111111111111111111111111111111111111111";
    const H2: &str = "2222222222222222222222222222222222222222222222222222222222222222";
    const H3: &str = "3333333333333333333333333333333333333333333333333333333333333333";

    fn acc(name: &str) -> AccountId {
        name.parse().unwrap()
    }

    fn as_caller(name: &str, deposit: NearToken) {
        testing_env!(VMContextBuilder::new()
            .predecessor_account_id(acc(name))
            .attached_deposit(deposit)
            .build());
    }

    fn zero() -> NearToken {
        NearToken::from_yoctonear(0)
    }

    /// Repo with 1 NEAR, a 0.5 NEAR spending limit and a full agent allowlist.
    fn setup() -> MendBountyEscrow {
        as_caller("owner.testnet", NearToken::from_near(1));
        let mut c = MendBountyEscrow::new();
        c.register_repo(REPO.to_string(), acc("operator.testnet"), NearToken::from_millinear(500));
        as_caller("owner.testnet", zero());
        c.set_agents(
            REPO.to_string(),
            acc("recon.testnet"),
            acc("guard.testnet"),
            vec![acc("forge-a.testnet"), acc("forge-b.testnet")],
        );
        c
    }

    fn open(c: &mut MendBountyEscrow, id: &str) {
        as_caller("operator.testnet", zero());
        c.open_incident(id.to_string(), REPO.to_string(), H1.to_string(), NearToken::from_millinear(200));
    }

    #[test]
    fn full_lifecycle_pays_the_swarm_split() {
        let mut c = setup();
        open(&mut c, "INC-1");
        let repo = c.get_repo(REPO.to_string()).unwrap();
        assert_eq!(repo.balance, NearToken::from_millinear(800));
        assert_eq!(repo.reserved, NearToken::from_millinear(200));

        as_caller("recon.testnet", zero());
        c.submit_recon("INC-1".to_string(), H2.to_string());
        as_caller("forge-a.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 0);
        as_caller("guard.testnet", zero());
        c.attest("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string(), true, 92, H2.to_string());
        as_caller("owner.testnet", zero());
        c.approve("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string());

        let inc = c.get_incident("INC-1".to_string()).unwrap();
        assert_eq!(inc.status, IncidentStatus::Resolved);
        assert_eq!(inc.winner, Some(acc("forge-a.testnet")));
        let total: u128 = inc.payouts.iter().map(|p| p.amount.as_yoctonear()).sum();
        assert_eq!(total, NearToken::from_millinear(200).as_yoctonear());
        assert_eq!(c.get_agent(acc("forge-a.testnet")).earned, NearToken::from_millinear(140));
        assert_eq!(c.get_agent(acc("guard.testnet")).earned, NearToken::from_millinear(30));
        assert_eq!(c.get_agent(acc("recon.testnet")).earned, NearToken::from_millinear(30));
        assert_eq!(c.get_agent(acc("forge-a.testnet")).patches_accepted, 1);

        let repo = c.get_repo(REPO.to_string()).unwrap();
        assert_eq!(repo.reserved, zero());
        assert_eq!(repo.total_paid, NearToken::from_millinear(200));
        assert_eq!(c.get_stats().incidents_resolved, 1);
    }

    #[test]
    fn reject_returns_the_bounty_to_the_treasury() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("owner.testnet", zero());
        c.reject("INC-1".to_string());
        let repo = c.get_repo(REPO.to_string()).unwrap();
        assert_eq!(repo.balance, NearToken::from_near(1));
        assert_eq!(repo.reserved, zero());
        assert_eq!(c.get_incident("INC-1".to_string()).unwrap().status, IncidentStatus::Rejected);
    }

    #[test]
    #[should_panic(expected = "Bounty exceeds the repository spending limit")]
    fn operator_cannot_exceed_the_spending_limit() {
        let mut c = setup();
        as_caller("operator.testnet", zero());
        c.open_incident("INC-1".to_string(), REPO.to_string(), H1.to_string(), NearToken::from_millinear(600));
    }

    #[test]
    #[should_panic(expected = "Only the repository operator can open incidents")]
    fn strangers_cannot_open_incidents() {
        let mut c = setup();
        as_caller("mallory.testnet", zero());
        c.open_incident("INC-1".to_string(), REPO.to_string(), H1.to_string(), NearToken::from_millinear(100));
    }

    #[test]
    #[should_panic(expected = "Caller is not an allowlisted forge agent")]
    fn unlisted_agents_cannot_submit_patches() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("mallory.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 0);
    }

    #[test]
    #[should_panic(expected = "Only the repository owner can approve a payout")]
    fn operator_cannot_approve_its_own_payout() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("forge-a.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 0);
        as_caller("guard.testnet", zero());
        c.attest("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string(), true, 90, H2.to_string());
        as_caller("operator.testnet", zero());
        c.approve("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string());
    }

    #[test]
    #[should_panic(expected = "Patch has not been attested by the guard")]
    fn approval_requires_a_guard_attestation() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("forge-a.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 0);
        as_caller("owner.testnet", zero());
        c.approve("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string());
    }

    #[test]
    #[should_panic(expected = "Guard rejected this patch")]
    fn approval_fails_when_the_guard_rejected() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("forge-a.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 0);
        as_caller("guard.testnet", zero());
        c.attest("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string(), false, 20, H2.to_string());
        as_caller("owner.testnet", zero());
        c.approve("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string());
    }

    #[test]
    #[should_panic(expected = "Patch does not have a clean test run")]
    fn approval_fails_when_tests_failed() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("forge-a.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 1);
        as_caller("guard.testnet", zero());
        c.attest("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string(), true, 90, H2.to_string());
        as_caller("owner.testnet", zero());
        c.approve("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string());
    }

    #[test]
    #[should_panic(expected = "Patch hash does not match the submission")]
    fn approval_is_bound_to_the_reviewed_patch_hash() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("forge-a.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 0);
        as_caller("guard.testnet", zero());
        c.attest("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string(), true, 90, H2.to_string());
        as_caller("owner.testnet", zero());
        c.approve("INC-1".to_string(), acc("forge-a.testnet"), H1.to_string());
    }

    #[test]
    #[should_panic(expected = "The guard must be independent of every forge agent")]
    fn guard_cannot_also_be_a_forge_agent() {
        as_caller("owner.testnet", NearToken::from_near(1));
        let mut c = MendBountyEscrow::new();
        c.register_repo(REPO.to_string(), acc("operator.testnet"), NearToken::from_millinear(500));
        as_caller("owner.testnet", zero());
        c.set_agents(
            REPO.to_string(),
            acc("recon.testnet"),
            acc("forge-a.testnet"),
            vec![acc("forge-a.testnet")],
        );
    }

    #[test]
    #[should_panic(expected = "Incident is not open")]
    fn a_resolved_incident_cannot_be_paid_twice() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("forge-a.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 0);
        as_caller("guard.testnet", zero());
        c.attest("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string(), true, 90, H2.to_string());
        as_caller("owner.testnet", zero());
        c.approve("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string());
        c.approve("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string());
    }

    #[test]
    fn competing_forge_agents_only_the_winner_is_paid() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("forge-a.testnet", zero());
        c.submit_patch("INC-1".to_string(), H3.to_string(), 4, 0);
        as_caller("forge-b.testnet", zero());
        c.submit_patch("INC-1".to_string(), H2.to_string(), 4, 0);
        as_caller("guard.testnet", zero());
        c.attest("INC-1".to_string(), acc("forge-a.testnet"), H3.to_string(), true, 70, H1.to_string());
        c.attest("INC-1".to_string(), acc("forge-b.testnet"), H2.to_string(), true, 95, H1.to_string());
        as_caller("owner.testnet", zero());
        c.approve("INC-1".to_string(), acc("forge-b.testnet"), H2.to_string());
        assert_eq!(c.get_agent(acc("forge-a.testnet")).earned, zero());
        // No recon report on this incident, so the forge agent keeps that share.
        assert_eq!(c.get_agent(acc("forge-b.testnet")).earned, NearToken::from_millinear(170));
        assert_eq!(c.get_agent(acc("forge-a.testnet")).patches_submitted, 1);
    }

    #[test]
    #[should_panic(expected = "Amount exceeds the unreserved treasury balance")]
    fn owner_cannot_withdraw_reserved_funds() {
        let mut c = setup();
        open(&mut c, "INC-1");
        as_caller("owner.testnet", zero());
        let _ = c.withdraw(REPO.to_string(), NearToken::from_millinear(900));
    }
}

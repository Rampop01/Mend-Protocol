"""
Command line for Mend.

  mend setup            create testnet accounts, deploy the escrow contract, fund the treasury
  mend connect <repo>   clone a GitHub repo, check its tests, open its treasury on NEAR
  mend projects         list the codebases Mend watches
  mend serve            run the server
  mend status           show treasury, agent earnings and incidents
  mend incidents        list recent incidents
  mend show <id>        print one incident
  mend approve <id>     approve the selected patch (releases the bounty)
  mend reject <id>      reject it (bounty returns to the treasury)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from mend.config import Settings, load_settings
from mend.integrations.near import NearError, yocto_to_near


def _api(settings: Settings, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    headers = {"Content-Type": "application/json"}
    if settings.ingest_token:
        headers["Authorization"] = f"Bearer {settings.ingest_token}"
    req = urllib.request.Request(
        f"http://{host}:{settings.port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode()).get("error", "")
        except ValueError:
            message = ""
        raise SystemExit(f"Error: {message or f'HTTP {exc.code}'}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach the Mend server at {host}:{settings.port} ({exc.reason}). Is `mend serve` running?")


def _print_incident(incident: Dict[str, Any]) -> None:
    print(f"{incident['incident_id']}  {incident['status']}")
    print(f"  {incident['error_type']}: {incident['error_message']}")
    print(f"  {incident['repo']}  {incident['rel_path']}:{incident['line_number']}")
    for entry in incident.get("timeline", []):
        tx = f"  {entry['tx_url']}" if entry.get("tx_url") else ""
        print(f"  [{entry['agent']}] {entry['message']}{tx}")
    for cand in incident.get("candidates", []):
        if cand.get("declined"):
            print(f"  forge-{cand['strategy']}: no proposal ({cand['declined']})")
            continue
        guard = cand.get("guard") or {}
        mark = " (selected)" if cand["strategy"] == incident.get("winner") else ""
        print(f"  forge-{cand['strategy']}{mark}: guard {'pass' if guard.get('passed') else 'fail'}, score {guard.get('score')}/100")
        if cand.get("diff"):
            print("\n".join("    " + line for line in cand["diff"].splitlines()))
    resolution = incident.get("resolution")
    if resolution:
        if resolution.get("settlement"):
            print(f"  Settlement: {resolution['settlement']['explorer_url']}")
        for payout in resolution.get("payouts", []):
            print(f"    {payout['role']}: {payout['amount_near']} NEAR -> {payout['account']}")
        if resolution.get("pr_url"):
            print(f"  Pull request: {resolution['pr_url']}")
        for note in resolution.get("notes", []):
            print(f"  Note: {note}")


def cmd_setup(args: argparse.Namespace, settings: Settings) -> None:
    from mend.integrations.escrow import EscrowClient, setup_testnet

    if settings.near_network != "testnet":
        raise SystemExit("`mend setup` only provisions testnet accounts.")
    from mend.config import load_projects
    try:
        repos = [p.repo for p in load_projects(settings, settings.base_dir / "mend.projects.json")]
    except SystemExit:
        repos = []
    try:
        setup_testnet(settings.near_keys_file, repos, owner_name=args.owner_name)
    except NearError as exc:
        raise SystemExit(f"Setup failed: {exc}\nRe-run `mend setup` to resume from the last completed step.")
    client = EscrowClient.load(settings.near_keys_file)
    assert client is not None
    public = settings.base_dir / "deployments" / "testnet.json"
    public.parent.mkdir(exist_ok=True)
    state = json.loads(settings.near_keys_file.read_text())
    public.write_text(json.dumps({**client.deployment().public_dict(), "setup_txs": state.get("setup_txs", {})}, indent=2) + "\n")
    print(f"Keys saved to {settings.near_keys_file} (keep this file private).")
    print(f"Public deployment record written to {public}.")


def cmd_connect(args: argparse.Namespace, settings: Settings) -> None:
    import shlex
    from mend.connect import ConnectError, connect_repo
    from mend.integrations.near import NearError

    command = shlex.split(args.test_command) if args.test_command else None
    try:
        result = connect_repo(settings, args.repo, test_command=command, base_branch=args.base_branch,
                              register_on_chain=not args.no_chain)
    except (ConnectError, NearError) as exc:
        raise SystemExit(f"Could not connect: {exc}")
    print(f"\nConnected {result['repo']} ({result['base_branch']} @ {result['commit'][:7]}).")
    if result["treasury"]:
        print(f"Treasury: {yocto_to_near(result['treasury']['balance'])} NEAR, "
              f"max bounty {yocto_to_near(result['treasury']['max_bounty'])} NEAR per incident.")
    print("Restart `mend serve` if it is running. In your app:\n")
    print("    from mend.sdk import init_mend")
    print(f'    init_mend(repo="{result["repo"]}")')


def cmd_projects(args: argparse.Namespace, settings: Settings) -> None:
    for project in settings.projects:
        kind = f"GitHub, fixes as pull requests to {project.base_branch}" if project.managed else "local checkout"
        print(f"{project.repo}  ({kind})\n    {project.root}\n    tests: {' '.join(project.test_command)}")


def cmd_status(args: argparse.Namespace, settings: Settings) -> None:
    status = _api(settings, "/api/v1/status")
    print("Projects: " + ", ".join(p["repo"] for p in status["projects"]))
    print("Incidents: " + (", ".join(f"{n} {s}" for s, n in sorted(status["incidents"].items())) or "none"))
    llm = status["agents"]["forge_llm"]
    print("LLM forge agent: " + (f"enabled ({llm['model']})" if llm["enabled"] else "disabled (set NEAR_AI_API_KEY)"))
    chain = status["near"]
    if not chain.get("configured") or chain.get("error"):
        print(f"NEAR: {chain.get('error')}")
        return
    print(f"NEAR {chain['network']}: {chain['contract_id']}  {chain['contract_url']}")
    for repo_id, repo in chain["repos"].items():
        if repo:
            print(f"  Treasury {repo_id}: {yocto_to_near(repo['balance'])} free, "
                  f"{yocto_to_near(repo['reserved'])} reserved, {yocto_to_near(repo['total_paid'])} paid (NEAR)")
    for agent in chain["agents"]:
        print(f"  {agent['role']:<12} {agent['account_id']}  earned {yocto_to_near(agent['earned'])} NEAR")


def cmd_incidents(args: argparse.Namespace, settings: Settings) -> None:
    for incident in _api(settings, "/api/v1/incidents")["incidents"]:
        print(f"{incident['incident_id']}  {incident['status']:<18} {incident['error_type']} at "
              f"{incident['rel_path']}:{incident['line_number']}")


def cmd_show(args: argparse.Namespace, settings: Settings) -> None:
    incident = _api(settings, f"/api/v1/incidents/{args.incident_id}")
    if args.json:
        print(json.dumps(incident, indent=2))
    else:
        _print_incident(incident)


def cmd_decide(args: argparse.Namespace, settings: Settings) -> None:
    incident = _api(settings, f"/api/v1/incidents/{args.incident_id}/{args.command}", {"approver": "cli"})
    _print_incident(incident)


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(prog="mend", description="Mend Protocol")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="create testnet accounts and deploy the escrow contract")
    setup.add_argument("--owner-name", help="name for the owner account, without .testnet (default: random)")
    connect = sub.add_parser("connect", help="watch a GitHub repository")
    connect.add_argument("repo", help="owner/repo or a GitHub URL")
    connect.add_argument("--test-command", help='how to run the tests, e.g. "python -m pytest -q"')
    connect.add_argument("--base-branch", help="branch to fix (default: the repository's default branch)")
    connect.add_argument("--no-chain", action="store_true", help="do not open a treasury on NEAR")
    sub.add_parser("projects", help="list watched codebases")
    sub.add_parser("serve", help="run the server")
    sub.add_parser("status", help="show treasury, agents and incident counts")
    sub.add_parser("incidents", help="list recent incidents")
    show = sub.add_parser("show", help="print one incident")
    show.add_argument("incident_id")
    show.add_argument("--json", action="store_true")
    for name in ("approve", "reject"):
        decide = sub.add_parser(name, help=f"{name} the selected patch")
        decide.add_argument("incident_id")
    args = parser.parse_args(argv)

    settings = load_settings(require_projects=args.command not in ("connect", "setup"))
    if args.command == "connect":
        cmd_connect(args, settings)
        return
    if args.command == "projects":
        cmd_projects(args, settings)
        return
    if args.command == "serve":
        from mend.server import serve
        serve(settings)
    elif args.command == "setup":
        cmd_setup(args, settings)
    elif args.command == "status":
        cmd_status(args, settings)
    elif args.command == "incidents":
        cmd_incidents(args, settings)
    elif args.command == "show":
        cmd_show(args, settings)
    else:
        cmd_decide(args, settings)


if __name__ == "__main__":
    main(sys.argv[1:])

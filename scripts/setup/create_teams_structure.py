"""One-time setup: build the MGMG Command Center team in Microsoft Teams.

Creates the team (if it does not exist), then every channel from the list
below, then a Planner plan the agents write follow-up tasks into. Safe to
re-run: existing channels are detected by name and skipped, so this doubles as
a "make reality match this file" script when a channel is added later.

Requires the Azure app to have admin-consented **application** permissions:
    Group.ReadWrite.All, Team.Create, Channel.Create, Tasks.ReadWrite.All,
    User.Read.All

Run:
    python scripts/setup/create_teams_structure.py --dry-run   # show the plan
    python scripts/setup/create_teams_structure.py             # create

Afterwards, copy the printed MS_PLANNER_GROUP_ID and MS_PLANNER_DEFAULT_PLAN_ID
into .env — the CEO brief and the follow-up agent both read them from there.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common.config import settings
from integrations.common.db import close_pool, log_action
from integrations.common.logging_setup import setup_logging
from integrations.microsoft.client import GraphClient, GraphError

AGENT = "setup-teams"
log = setup_logging(AGENT)

TEAM_NAME = "MGMG Command Center"
TEAM_DESCRIPTION = "Central operating hub for all MGMG divisions — alerts, tasks and daily briefs."

# Channel name -> purpose. Numbering keeps Teams' alphabetical sort meaningful.
CHANNELS: list[tuple[str, str]] = [
    ("01 CEO", "Daily briefs, escalations and cross-division decisions"),
    ("02 Finance", "Cash, receivables, payments and SAP financial alerts"),
    ("03 Armin Sales", "Armin product sales — pipeline, deals, follow-ups"),
    ("04 IMUS Sales", "IMUS-Alliance technical equipment sales"),
    ("05 Procurement & Import", "Purchasing, suppliers, customs and import documents"),
    ("06 Warehouse", "Stock levels, receipts, shipments and inventory alerts"),
    ("07 Service Center", "Equipment maintenance, repairs and service requests"),
    ("08 Laundromats", "ONDRY locations, equipment status and payment data"),
    ("09 Properties & Rent", "Commercial real estate, tenants and rent collection"),
    ("10 HR & Attendance", "Verifix attendance, payroll, KPI and scheduling"),
    ("11 Legal & Contracts", "Contracts, approvals and compliance"),
    ("12 IT & Security", "Infrastructure, access management and CCTV"),
    ("13 Automation Projects", "n8n workflows, agent rollout and integration work"),
]

PLAN_NAME = "MGMG Tasks"


async def setup(dry_run: bool = False) -> int:
    """Create the team, its channels and the Planner plan.

    Args:
        dry_run: Print what would be created and change nothing.

    Returns:
        Process exit code — 0 on success, 1 on a Graph failure, 2 on bad config.
    """
    run_id = uuid.uuid4()

    if not settings.ms_team_owner_upn or settings.ms_team_owner_upn.startswith("["):
        log.error("MS_TEAM_OWNER_UPN must be a real user (app-only team creation requires an owner)")
        return 2

    if dry_run:
        print(f"Would create team: {TEAM_NAME}")
        print(f"  owner: {settings.ms_team_owner_upn}")
        for name, description in CHANNELS:
            print(f"  channel: {name} — {description}")
        print(f"  planner plan: {PLAN_NAME}")
        return 0

    try:
        async with GraphClient(agent=AGENT, run_id=run_id) as graph:
            team_id = await _ensure_team(graph)
            created, skipped = await _ensure_channels(graph, team_id)
            plan_id = await _ensure_plan(graph, team_id)
    except GraphError as err:
        log.error("Teams setup failed: {}", err)
        await log_action(
            agent=AGENT,
            action="setup_teams",
            target_system="msgraph",
            status="failure",
            run_id=run_id,
            mode="write",
            error_message=str(err),
        )
        return 1

    await log_action(
        agent=AGENT,
        action="setup_teams",
        target_system="msgraph",
        status="success",
        run_id=run_id,
        target_ref=team_id,
        mode="write",
        payload={"channels_created": created, "channels_existing": skipped, "plan_id": plan_id},
    )

    print("\n" + "=" * 62)
    print(f"Team ready: {TEAM_NAME}")
    print(f"  channels created: {created}, already present: {skipped}")
    print("\nAdd these to .env:")
    print(f"  MS_PLANNER_GROUP_ID={team_id}")
    print(f"  MS_PLANNER_DEFAULT_PLAN_ID={plan_id or '<create a plan in Teams, then paste its id>'}")
    print("=" * 62)
    return 0


async def _ensure_team(graph: GraphClient) -> str:
    """Find the Command Center team, creating it if absent.

    Newly created teams take a short while to provision before channels can be
    added, so this waits for the team to become addressable.

    Args:
        graph: An authenticated Graph client.

    Returns:
        The team/group id.

    Raises:
        GraphError: if creation fails or provisioning never completes.
    """
    existing = await graph.find_team_by_name(TEAM_NAME)
    if existing:
        log.info("Team '{}' already exists ({})", TEAM_NAME, existing["id"])
        return existing["id"]

    team_id = await graph.create_team(TEAM_NAME, TEAM_DESCRIPTION, settings.ms_team_owner_upn)

    for attempt in range(1, 13):  # up to ~60s
        try:
            await graph.list_channels(team_id)
            log.info("Team provisioned after {} check(s)", attempt)
            return team_id
        except GraphError:
            await asyncio.sleep(5)

    raise GraphError(f"Team {team_id} was created but is still not addressable after 60s")


async def _ensure_channels(graph: GraphClient, team_id: str) -> tuple[int, int]:
    """Create every channel that does not exist yet.

    Args:
        graph: An authenticated Graph client.
        team_id: The team to populate.

    Returns:
        Tuple of (channels created, channels already present).

    Raises:
        GraphError: if listing channels fails. Individual channel creation
            failures are logged and counted as skipped, so one bad name does
            not abort the whole setup.
    """
    existing = {c["displayName"] for c in await graph.list_channels(team_id)}
    created = skipped = 0

    for name, description in CHANNELS:
        if name in existing:
            log.debug("Channel '{}' already exists", name)
            skipped += 1
            continue
        try:
            await graph.create_channel(team_id, name, description)
            log.info("Channel created: {}", name)
            created += 1
        except GraphError as err:
            log.error("Could not create channel '{}': {}", name, err)
            skipped += 1
        # Graph throttles rapid channel creation; pace the loop.
        await asyncio.sleep(1.5)

    return created, skipped


async def _ensure_plan(graph: GraphClient, team_id: str) -> str | None:
    """Return the id of the plan agents write into, if one exists.

    Args:
        graph: An authenticated Graph client.
        team_id: The group behind the team.

    Returns:
        The plan id, or ``None`` when the group has no plans yet.
    """
    try:
        plans = await graph.get_plans_for_group(team_id)
    except GraphError as err:
        log.warning("Could not list Planner plans: {}", err)
        return None

    if not plans:
        log.warning(
            "No Planner plan found. Add a Planner tab to the '01 CEO' channel in Teams, "
            "then paste its plan id into MS_PLANNER_DEFAULT_PLAN_ID."
        )
        return None

    preferred = next((p for p in plans if p.get("title") == PLAN_NAME), plans[0])
    log.info("Using Planner plan '{}' ({})", preferred.get("title"), preferred["id"])
    return preferred["id"]


async def _main(dry_run: bool) -> int:
    """Run setup and close the database pool.

    Args:
        dry_run: Passed through to ``setup``.

    Returns:
        The process exit code.
    """
    try:
        return await setup(dry_run)
    finally:
        await close_pool()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Create the MGMG Command Center team in Teams.")
    parser.add_argument("--dry-run", action="store_true", help="print the plan without creating anything")
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(_main(args.dry_run)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

"""Live draft state — picks made, traded picks, pick ownership for upcoming slots.

Sleeper gives us picks-as-made (each pick's `roster_id` is who actually selected, not
the original slot owner). For upcoming picks, we have to compute ownership ourselves
by starting from `draft.slot_to_roster_id` and applying `traded_picks` deltas.

This module handles snake AND 3RR (Third Round Reversal). The pattern under 3RR:
  R1: 1 → teams         (slot 1 picks first)
  R2: teams → 1         (snake reverse)
  R3: teams → 1         (3RR: reverse AGAIN instead of snake)
  R4: 1 → teams         (snake continues from R3's end)
  R5: teams → 1
  R6: 1 → teams
  ...

This single class is the source of truth for:
  • What's the overall pick number for (round, slot)?
  • Whose pick is overall pick N?
  • What picks does roster R have left?
  • Which picks have been traded, and what's the previous-owner chain?
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .league_config import LeagueConfig


@dataclass
class ScheduledPick:
    pick_no: int
    round: int
    slot_pos: int           # position within the round (1 = first to pick)
    orig_slot: int          # the slot that originally owned this pick
    orig_roster_id: int
    owner_roster_id: int    # current owner after applying traded picks
    made: bool = False
    player_id: str | None = None
    @property
    def is_traded(self) -> bool:
        return self.owner_roster_id != self.orig_roster_id


@dataclass
class DraftState:
    cfg: LeagueConfig
    draft: dict
    picks: list[dict] = field(default_factory=list)
    draft_traded_picks: list[dict] = field(default_factory=list)
    league_traded_picks: list[dict] = field(default_factory=list)

    # Resolved at build()
    slot_to_roster: dict[int, int] = field(default_factory=dict)
    roster_to_slot: dict[int, int] = field(default_factory=dict)
    schedule: list[ScheduledPick] = field(default_factory=list)
    picks_by_no: dict[int, dict] = field(default_factory=dict)

    def build(self) -> "DraftState":
        d = self.draft
        s2r = {int(k): v for k, v in (d.get("slot_to_roster_id") or {}).items()}
        self.slot_to_roster = s2r
        self.roster_to_slot = {v: k for k, v in s2r.items()}
        self.picks_by_no = {p["pick_no"]: p for p in self.picks if p.get("pick_no")}

        teams = self.cfg.teams
        rounds = int((d.get("settings") or {}).get("rounds") or 0)
        if not rounds:
            rounds = 28  # safe default for a startup

        # Build the schedule
        schedule: list[ScheduledPick] = []
        for rd in range(1, rounds + 1):
            for slot in range(1, teams + 1):
                pn = self.pick_no_for_slot(rd, slot)
                orig_rid = s2r.get(slot, 0)
                schedule.append(
                    ScheduledPick(
                        pick_no=pn,
                        round=rd,
                        slot_pos=(pn - 1) % teams + 1,
                        orig_slot=slot,
                        orig_roster_id=orig_rid,
                        owner_roster_id=orig_rid,
                    )
                )

        # Apply traded picks (current-draft only — league_traded_picks is for FUTURE drafts)
        for tp in self.draft_traded_picks:
            rd = int(tp.get("round") or 0)
            orig_rid = int(tp.get("roster_id") or 0)
            if not rd or not orig_rid:
                continue
            orig_slot = self.roster_to_slot.get(orig_rid)
            if not orig_slot:
                continue
            pn = self.pick_no_for_slot(rd, orig_slot)
            for sp in schedule:
                if sp.pick_no == pn:
                    sp.owner_roster_id = int(tp.get("owner_id") or sp.owner_roster_id)
                    break

        # Mark made picks
        for sp in schedule:
            made = self.picks_by_no.get(sp.pick_no)
            if made:
                sp.made = True
                sp.player_id = str(made.get("player_id") or "") or None
                sp.owner_roster_id = int(made.get("roster_id") or sp.owner_roster_id)

        self.schedule = sorted(schedule, key=lambda x: x.pick_no)
        return self

    # ------------------------------------------------------------ ordering

    def pick_no_for_slot(self, round_no: int, slot: int) -> int:
        teams = self.cfg.teams
        rev_3rr = self.cfg.third_round_reversal

        if round_no == 1:
            pos = slot
        elif round_no == 2:
            pos = teams - slot + 1
        elif round_no == 3 and rev_3rr:
            pos = teams - slot + 1   # reverse AGAIN
        elif round_no == 3 and not rev_3rr:
            pos = slot               # standard snake forward
        else:
            # After R3: continue snake. With 3RR, R3 ended at slot 1, so R4 starts at slot 1
            # → R4 forward (1→teams), R5 reverse, R6 forward, ...
            # Without 3RR, R3 ended at slot `teams` (1→teams), so R4 reverses, R5 forward, ...
            if rev_3rr:
                # 3RR: rounds 4,6,8,... forward; rounds 5,7,9,... reverse
                pos = slot if (round_no % 2 == 0) else teams - slot + 1
            else:
                # snake: rounds 4,6,8,... reverse; rounds 5,7,9,... forward
                pos = (teams - slot + 1) if (round_no % 2 == 0) else slot
        return (round_no - 1) * teams + pos

    # ------------------------------------------------------------- queries

    def drafted_ids(self) -> set[str]:
        return {p["player_id"] for p in self.picks if p.get("player_id")}

    def on_clock(self) -> ScheduledPick | None:
        for sp in self.schedule:
            if not sp.made:
                return sp
        return None

    def upcoming_for_roster(self, roster_id: int) -> list[ScheduledPick]:
        return [sp for sp in self.schedule if sp.owner_roster_id == roster_id and not sp.made]

    def all_my_picks(self, my_roster_id: int) -> list[ScheduledPick]:
        return [sp for sp in self.schedule if sp.owner_roster_id == my_roster_id]

    def remaining_counts_per_roster(self) -> dict[int, int]:
        out: dict[int, int] = defaultdict(int)
        for sp in self.schedule:
            if not sp.made:
                out[sp.owner_roster_id] += 1
        return dict(out)

    # ----------------------------------------------------- future-pick view

    def future_pick_inventory(self, season: str, *, rounds: int = 4) -> dict[int, list[dict]]:
        """Net 2027/2028 rookie-pick ownership per roster_id.

        Each roster starts with its own 4 rookie rounds. league_traded_picks deltas
        are applied to compute current ownership.

        Returns: {roster_id: [{'season':..., 'round':r, 'orig_roster_id':r2}, ...]}
        """
        own: dict[int, set[tuple]] = defaultdict(set)
        for rid in self.slot_to_roster.values():
            for r in range(1, rounds + 1):
                own[rid].add((season, r, rid))
        for tp in self.league_traded_picks:
            if str(tp.get("season")) != str(season):
                continue
            key = (str(tp["season"]), int(tp["round"]), int(tp["roster_id"]))
            own[int(tp["previous_owner_id"])].discard(key)
            own[int(tp["owner_id"])].add(key)
        out: dict[int, list[dict]] = {}
        for rid, items in own.items():
            out[rid] = sorted(
                [{"season": s, "round": r, "orig_roster_id": orig} for (s, r, orig) in items],
                key=lambda x: (x["round"], x["orig_roster_id"]),
            )
        return out

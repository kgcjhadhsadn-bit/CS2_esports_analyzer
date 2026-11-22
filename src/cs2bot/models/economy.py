"""CS2 rules/economy constants and the team-level behaviour assumptions used by the map simulator.

Three frozen (hashable) dataclasses:

* :class:`EconomyParams` - the *rules of the game*: MR12 regulation, MR3 overtime, start/max money,
  round rewards, the loss-bonus ladder and its CS2 semantics, plant/defuse/kill bonuses.
* :class:`BuyPolicy` - how professional teams *decide* what to buy (team-level heuristics).
* :class:`RoundFlowParams` - how rounds *end* (end-reason mix, deaths/survivors), which the
  economy needs because rewards depend on the end reason and survivors keep their weapons.

Sources for the rule values (verified 2026-11)
----------------------------------------------
[CFG]  CS2 ``game/csgo/cfg/gamemode_competitive.cfg`` (SteamDatabase/GameTracking-CS2): mp_maxrounds 24,
       mp_startmoney 800, mp_maxmoney 16000, cash_team_elimination_bomb_map 3250,
       cash_team_win_by_time_running_out_bomb 3250, cash_team_win_by_defusing_bomb 3500,
       cash_team_terrorist_win_bomb 3500, cash_team_loser_bonus 1400,
       cash_team_loser_bonus_consecutive_rounds 500, mp_starting_losses 1,
       cash_team_planted_bomb_but_defused 600, cash_player_bomb_planted 300,
       cash_player_bomb_defused 300, cash_player_killed_enemy_default 300.
[TMM]  ``gamemode_competitive_tmm.cfg`` (Premier): mp_overtime_enable 1 (overtime limit 1 in Premier).
[CVAR] CS2 convar dump (GameTracking-CS2 ``DumpSource2/convars.txt``): mp_overtime_maxrounds 6,
       mp_overtime_startmoney 10000 ("money assigned to all players at start of every overtime half"),
       mp_consecutive_loss_max 4, mp_consecutive_loss_aversion 1 ("0 = win fully resets loss bonus,
       1 = first win steps down loss bonus, 2 = first win holds loss bonus and step down starting
       with second win"), cash_team_per_dead_enemy 50.
[MAJ]  Valve Major Supplemental Rulebook (ValveSoftware/counter-strike_rules_and_regs): "All server
       convars are to be set to Premier defaults" (competitive.cfg then competitive_tmm.cfg) with
       exceptions mp_overtime_limit 0 (unlimited overtimes) and cash_team_bonus_shorthanded 0. No
       override of mp_overtime_startmoney, i.e. the professional overtime money is the $10,000
       default (Liquipedia: "max rounds 3, $10k" is the standard overtime format). Some organisers
       use $12,500 - configurable.
[SRC]  CS:GO game rules source (cs_gamerules.cpp, the code CS2 inherited): when regulation ends tied,
       ``GoToOvertime`` is called with the comment "don't switch teams, only switch at true
       halftimes" -> teams keep the side they finished on for the first OT half, swap at the OT
       halftime, and again keep sides into the next OT period. Every half start (regulation and
       overtime) resets all accounts to the start money of that half.
[UPD]  CS2 update of 16 July 2025: in competitive modes each CT player receives $50 for every
       Terrorist eliminated in the round (matches cash_team_per_dead_enemy 50 above).
[GDE]  Community economy guides (consistent across sources): Terrorists alive when the round timer
       expires without a plant receive no loss bonus; weapon kill rewards: rifles/pistols $300,
       SMGs $600 (P90 $300), AWP $100, shotguns $900, knife $1500.

Loss-bonus semantics used here (CS2 defaults): each team has a loss streak ``s`` that starts every
half at ``starting_losses`` (1). A loss pays ``loss_bonus_base + loss_bonus_step * s`` and then
``s = min(s + 1, loss_streak_max)``. With aversion 1 a win steps ``s`` down by one (never below 0).
Hence the pistol-round loser receives $1,900, the ladder is 1400/1900/2400/2900/3400, and a team on
max loss bonus that wins once and loses again receives $2,900.

All money in the simulator is the *team average per player* (team-level economy). The per-player
cap of $16,000 is applied to the average, which slightly overstates savings of rich teams.
"""

from __future__ import annotations

from dataclasses import dataclass

from cs2bot.domain.enums import BuyType

#: Buy-type code order shared by the round model and the simulator (index into effect tables).
BUY_ORDER: tuple[BuyType, ...] = (BuyType.PISTOL, BuyType.ECO, BuyType.FORCE, BuyType.FULL)
BUY_CODE: dict[BuyType, int] = {b: i for i, b in enumerate(BUY_ORDER)}
PISTOL_CODE, ECO_CODE, FORCE_CODE, FULL_CODE = 0, 1, 2, 3


@dataclass(frozen=True)
class EconomyParams:
    """Rules of a professional CS2 map (all values configurable; see module docstring for sources)."""

    # ---------------------------------------------------------------- structure
    rounds_per_half: int = 12  # MR12 [CFG mp_maxrounds 24]
    rounds_to_win: int = 13  # first to 13 wins regulation
    ot_rounds_per_half: int = 3  # MR3 overtime [CVAR mp_overtime_maxrounds 6]
    ot_rounds_to_win: int = 4  # win an OT period 4-x; 3-3 starts another period
    ot_switch_sides_at_start: bool = False  # [SRC] sides are kept into each new overtime period
    max_overtimes: int = 40  # simulation safety cap only (pro rules: unlimited, [MAJ] mp_overtime_limit 0)

    # ---------------------------------------------------------------- money
    start_money: int = 800  # [CFG]
    max_money: int = 16_000  # [CFG]
    ot_start_money: int = 10_000  # [CVAR]/[MAJ]; some organisers use 12_500
    default_pistol_value: int = 200  # Glock-18 / USP-S / P2000 everyone spawns with

    # ---------------------------------------------------------------- round rewards (per player)
    win_elimination: int = 3250  # [CFG] cash_team_elimination_bomb_map (both sides)
    win_time: int = 3250  # [CFG] cash_team_win_by_time_running_out_bomb (CT)
    win_defuse: int = 3500  # [CFG] cash_team_win_by_defusing_bomb (CT)
    win_detonation: int = 3500  # [CFG] cash_team_terrorist_win_bomb (T)

    # ---------------------------------------------------------------- loss bonus
    loss_bonus_base: int = 1400  # [CFG] cash_team_loser_bonus
    loss_bonus_step: int = 500  # [CFG] cash_team_loser_bonus_consecutive_rounds
    loss_streak_max: int = 4  # [CVAR] mp_consecutive_loss_max -> max bonus 3400
    starting_losses: int = 1  # [CFG] mp_starting_losses -> pistol loser gets 1900
    loss_aversion: int = 1  # [CVAR] mp_consecutive_loss_aversion (0 reset / 1 step down / 2 hold first)

    # ---------------------------------------------------------------- bomb / kill bonuses
    plant_bonus_team: int = 600  # [CFG] cash_team_planted_bomb_but_defused, every T player
    planter_bonus: int = 300  # [CFG] cash_player_bomb_planted (one player)
    defuser_bonus: int = 300  # [CFG] cash_player_bomb_defused (one player)
    ct_team_kill_bonus: int = 50  # [UPD] every CT player, per Terrorist eliminated
    t_time_loss_survivor_bonus: bool = False  # [GDE] Ts alive at time expiry get no loss bonus
    # Average kill reward per kill by the killer's buy type (PISTOL, ECO, FORCE, FULL): pistols $300;
    # force buys mix SMGs ($600) with Galil/FAMAS/shotguns; full buys mix rifles ($300) and AWP ($100).
    kill_reward_by_buy: tuple[int, int, int, int] = (300, 300, 450, 275)

    def __post_init__(self) -> None:
        if self.rounds_to_win != self.rounds_per_half + 1:
            raise ValueError("rounds_to_win must be rounds_per_half + 1 (first to more than half)")
        if self.ot_rounds_to_win != self.ot_rounds_per_half + 1:
            raise ValueError("ot_rounds_to_win must be ot_rounds_per_half + 1")
        if self.loss_aversion not in (0, 1, 2):
            raise ValueError("loss_aversion must be 0, 1 or 2")
        if len(self.kill_reward_by_buy) != len(BUY_ORDER):
            raise ValueError("kill_reward_by_buy needs one value per buy type")

    @property
    def regulation_rounds(self) -> int:
        return 2 * self.rounds_per_half

    @property
    def ot_rounds(self) -> int:
        return 2 * self.ot_rounds_per_half

    @property
    def loss_bonus_ladder(self) -> tuple[int, ...]:
        """Loss bonus by streak value 0..max (default 1400, 1900, 2400, 2900, 3400)."""
        return tuple(self.loss_bonus(s) for s in range(self.loss_streak_max + 1))

    def loss_bonus(self, streak: int) -> int:
        """Money each loser receives when losing with loss streak ``streak`` (before the increment)."""
        s = min(max(streak, 0), self.loss_streak_max)
        return self.loss_bonus_base + self.loss_bonus_step * s


@dataclass(frozen=True)
class BuyPolicy:
    """Team-level buy behaviour of professional teams (assumptions, documented and configurable).

    Costs are per player and include utility (CS2 prices: AK-47 2700, M4A1-S 2900 / M4A4 3100,
    kevlar+helmet 1000, defuse kit 400, smoke 300, flash 200, HE 300, molotov 400 / incendiary 500).

    * A **full buy** is chosen when ``money + carried >= full_min`` (rifle + armour + some utility)
      and spends up to ``full_target`` (rifle, armour, kit, full utility).
    * Otherwise the team **forces** (spends almost everything) with probability
      ``p_force_must_win`` when the round is critical (opponent on map point, or the last round of
      a half, after which money resets), ``p_force_after_pistol_loss`` on the round after a lost
      pistol, ``p_force_eco_futile`` when even saving would not allow a full buy next round, and
      ``p_force_base`` otherwise; else it **ecos** (spends ``eco_spend``).
    * **Anti-eco**: a team that just won and could full buy, facing an opponent that cannot full
      buy, buys SMGs / armour (``anti_eco_spend``, classified FORCE) with probability ``p_anti_eco``
      unless it is rich (> ``full_target + anti_eco_rich_margin``). This produces the classic
      round-2 SMG buy of the pistol-round winner.
    """

    full_min_ct: float = 4300.0
    full_min_t: float = 3900.0
    full_target_ct: float = 5400.0
    full_target_t: float = 4800.0
    force_min_money: float = 1500.0
    eco_spend: float = 250.0
    min_refill: float = 400.0  # utility / armour top-up when carried gear already covers the buy
    anti_eco_spend: float = 2700.0
    anti_eco_rich_margin: float = 1200.0
    p_anti_eco: float = 0.75
    p_force_base: float = 0.22
    p_force_after_pistol_loss: float = 0.22
    p_force_eco_futile: float = 0.45
    p_force_must_win: float = 0.92
    expected_loss_kill_income: float = 120.0  # kill money of a losing eco team (planning heuristic)

    def __post_init__(self) -> None:
        for name in ("p_anti_eco", "p_force_base", "p_force_after_pistol_loss", "p_force_eco_futile"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{name} must be a probability")
        if not 0.0 <= self.p_force_must_win <= 1.0:
            raise ValueError("p_force_must_win must be a probability")


@dataclass(frozen=True)
class RoundFlowParams:
    """How rounds end (approximate professional averages; only the economy depends on these).

    * End reasons: Terrorist round wins end by bomb detonation ``p_t_win_detonation`` of the time
      (else elimination); CT round wins end by defuse ``p_ct_win_defuse`` or time
      ``p_ct_win_time`` (else elimination). A planted bomb that is defused pays the plant bonus.
    * Deaths: the loser is wiped out on elimination; otherwise each loser dies with probability
      ``loser_death_*``. Winner deaths ~ Binomial(4, q) with
      ``q = clip(winner_death_base - winner_death_slope * buy_advantage)`` where ``buy_advantage`` is
      the winner's buy-matchup logit (full vs eco wins cleaner). Averages ~4.4 loser and ~2.0 winner
      deaths per round, i.e. ~0.64 kills per player-round, in line with pro KPR.
    * Survivors keep ``carry_fraction`` of their equipment value (utility and armour are consumed).
    """

    p_t_win_detonation: float = 0.42
    p_ct_win_defuse: float = 0.12
    p_ct_win_time: float = 0.13
    loser_death_after_detonation: float = 0.55
    loser_death_after_defuse: float = 0.85
    loser_death_after_time: float = 0.45
    winner_death_base: float = 0.55
    winner_death_slope: float = 0.11
    winner_death_min: float = 0.08
    winner_death_max: float = 0.80
    carry_fraction: float = 0.70

    def __post_init__(self) -> None:
        if self.p_ct_win_defuse + self.p_ct_win_time > 1.0:
            raise ValueError("p_ct_win_defuse + p_ct_win_time must be <= 1")
        if not 0.0 <= self.carry_fraction <= 1.0:
            raise ValueError("carry_fraction must be in [0, 1]")


DEFAULT_ECONOMY = EconomyParams()
DEFAULT_BUY_POLICY = BuyPolicy()
DEFAULT_ROUND_FLOW = RoundFlowParams()

__all__ = [
    "BUY_CODE",
    "BUY_ORDER",
    "DEFAULT_BUY_POLICY",
    "DEFAULT_ECONOMY",
    "DEFAULT_ROUND_FLOW",
    "ECO_CODE",
    "FORCE_CODE",
    "FULL_CODE",
    "PISTOL_CODE",
    "BuyPolicy",
    "EconomyParams",
    "RoundFlowParams",
]

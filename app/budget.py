"""
budget.py — Token 预算规划的纯数学核心（无外部依赖，可独立单测）。

供编排器 `_compute_plan` 使用。核心思想：剩余 R 轮的提示词成本不是常数 R·P，
而是随历史累积按算术级数递增的模型：

    prompt_cost(R) = R·P + g·P·R·(R-1)/2

- P = 每轮提示词 token 估算（含自适应校准因子与工具调用次数折算）
- g = 每轮历史增长比（EMA 自适应，默认 0.5）

在预算（扣除尾段与安全余量）内反解单轮输出上限 E：

    E ≤ (usable − base − prompt_cost(R)) / (R·N + R·S + 2)

- N = 每轮 LLM 调用次数；S = 辩手数（裁判记录随轮次增长 R·S·E）
- 2E = 裁判输出 + 汇总输出；base = 裁判/汇总提示词尾段保底
"""
from __future__ import annotations


def prompt_growth_sum(P: int | float, g: float, R: int) -> int:
    """R 轮提示词成本算术级数和：R·P + g·P·R·(R-1)/2。"""
    if R <= 0 or P <= 0:
        return 0
    return round(P * (R + g * R * (R - 1) / 2))


def solve_output_cap(
    remaining: int,
    base: int,
    P: int,
    g: float,
    N: int,
    S: int,
    R: int,
    floor: int,
    cap: int,
    margin: float = 0.0,
) -> int:
    """求解单次输出上限 E，使「剩余 R 轮 + 尾段」落在剩余预算（扣安全余量）内。

    参数：
      remaining — 剩余预算（budget - spent）
      base      — 裁判/汇总 提示词尾段保底
      P         — 每轮提示词 token 估算
      g         — 每轮历史增长比
      N         — 每轮 LLM 调用次数
      S         — 辩手数（裁判记录随轮次增长）
      R         — 剩余轮次（judge/summary 阶段为 0）
      floor/cap — 输出下限 / 用户设置的上限
      margin    — 规划安全余量比例（只压低 E，不算入超支判定）
    """
    usable = max(0, remaining * (1.0 - margin))
    prompt = prompt_growth_sum(P, g, R)
    denom = R * N + R * S + 2
    if denom <= 0:
        return cap
    numer = usable - base - prompt
    if numer > 0:
        e = int(numer / denom)
    else:
        e = floor
    return max(floor, min(e, cap))


def overshoot_at_floor(
    remaining: int,
    base: int,
    P: int,
    g: float,
    N: int,
    S: int,
    R: int,
    floor: int,
) -> bool:
    """即便单次输出压到下限 floor，规划总成本仍超出剩余预算。"""
    prompt = prompt_growth_sum(P, g, R)
    planned = base + prompt + (R * N + R * S + 2) * floor
    return planned > remaining


def projected_total(
    spent: int,
    remaining: int,
    base: int,
    P: int,
    g: float,
    N: int,
    S: int,
    R: int,
    E: int,
) -> int:
    """采用输出上限 E 时，已花费 + 剩余全程（含尾段）的规划总成本。"""
    prompt = prompt_growth_sum(P, g, R)
    return spent + base + prompt + (R * N + R * S + 2) * E


def feasible_rounds(
    remaining: int,
    base: int,
    P: int,
    g: float,
    N: int,
    S: int,
    E: int,
    max_r: int,
) -> int:
    """当前输出上限 E 下，剩余预算（扣除尾段与 2E 输出）最多还能支撑几轮。

    从 max_r 向下迭代，返回首个可行值（含 0）。
    """
    for r in range(max(0, int(max_r)), -1, -1):
        prompt = prompt_growth_sum(P, g, r)
        if base + prompt + (r * N + r * S + 2) * E <= remaining:
            return r
    return 0

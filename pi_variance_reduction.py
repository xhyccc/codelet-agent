"""
Compute pi using Monte Carlo simulation with variance reduction techniques.

Classic result: Draw (X,Y) uniformly from [0,1]^2.
Indicator of X^2 + Y^2 <= 1 has expectation pi/4.

Variance reduction methods:
1. Plain MC
2. Antithetic variates
3. Control variate (using X+Y as control)
4. Stratified sampling
"""

import random
import math
import statistics

random.seed(42)
N = 1_000_000

def in_circle(x, y):
    return x * x + y * y <= 1.0

# ── 1. Plain Monte Carlo ──────────────────────────────────────
plain = []
for _ in range(N):
    x = random.random()
    y = random.random()
    plain.append(float(in_circle(x, y)))

mu_plain = statistics.mean(plain)
pi_plain = 4 * mu_plain
se_plain = 4 * (statistics.stdev(plain) / math.sqrt(N))

print("1. Plain MC")
print(f"   pi estimate = {pi_plain:.8f}")
print(f"   std err     = {se_plain:.8f}")
print()

# ── 2. Antithetic variates ────────────────────────────────────
antithetic = []
for _ in range(N // 2):
    u1 = random.random()
    u2 = random.random()
    # Pair (u1, u2) and (1-u1, 1-u2)
    y1 = float(in_circle(u1, u2))
    y2 = float(in_circle(1 - u1, 1 - u2))
    antithetic.extend([y1, y2])

mu_anti = statistics.mean(antithetic)
pi_anti = 4 * mu_anti
se_anti = 4 * (statistics.stdev(antithetic) / math.sqrt(len(antithetic)))

print("2. Antithetic variates")
print(f"   pi estimate = {pi_anti:.8f}")
print(f"   std err     = {se_anti:.8f}")
print()

# ── 3. Control variate ────────────────────────────────────────
# Use X+Y as control variate with known mean 1.0
# Or use the fact that E[X^2 + Y^2] = 2/3
# Let f = indicator, h = X^2 + Y^2, E[h] = 2/3
# f* = f - c*(h - 2/3), c* = -Cov(f,h)/Var(h)
# Compute optimal c from a pilot sample

pilot_size = 10_000
pilot_f = []
pilot_h = []
for _ in range(pilot_size):
    x = random.random()
    y = random.random()
    pilot_f.append(float(in_circle(x, y)))
    pilot_h.append(x * x + y * y)

cov_fh = statistics.covariance(pilot_f, pilot_h)
var_h = statistics.variance(pilot_h)
c_opt = -cov_fh / var_h if var_h != 0 else 0.0

control = []
for _ in range(N):
    x = random.random()
    y = random.random()
    f = float(in_circle(x, y))
    h = x * x + y * y
    control.append(f + c_opt * (h - 2.0 / 3.0))

mu_control = statistics.mean(control)
pi_control = 4 * mu_control
se_control = 4 * (statistics.stdev(control) / math.sqrt(N))

print("3. Control variate (X^2+Y^2)")
print(f"   optimal c   = {c_opt:.4f}")
print(f"   pi estimate = {pi_control:.8f}")
print(f"   std err     = {se_control:.8f}")
print()

# ── 4. Stratified sampling ────────────────────────────────────
# Divide unit square into m x m strata, sample equally from each
m = 10  # 100 strata
n_per_stratum = N // (m * m)
stratified = []
for i in range(m):
    for j in range(m):
        for _ in range(n_per_stratum):
            x = (i + random.random()) / m
            y = (j + random.random()) / m
            stratified.append(float(in_circle(x, y)))

mu_strat = statistics.mean(stratified)
pi_strat = 4 * mu_strat
se_strat = 4 * (statistics.stdev(stratified) / math.sqrt(len(stratified)))

print("4. Stratified sampling (10x10 grid)")
print(f"   pi estimate = {pi_strat:.8f}")
print(f"   std err     = {se_strat:.8f}")
print()

# ── Summary ───────────────────────────────────────────────────
print("─" * 50)
print("Summary (N = {:,})".format(N))
print(f"{'Method':<25} {'Estimate':>12} {'Std Err':>12}")
print("─" * 50)
print(f"{'Plain MC':<25} {pi_plain:>12.8f} {se_plain:>12.8f}")
print(f"{'Antithetic':<25} {pi_anti:>12.8f} {se_anti:>12.8f}")
print(f"{'Control variate':<25} {pi_control:>12.8f} {se_control:>12.8f}")
print(f"{'Stratified':<25} {pi_strat:>12.8f} {se_strat:>12.8f}")
print(f"{'True pi':<25} {math.pi:>12.8f}")
print("─" * 50)

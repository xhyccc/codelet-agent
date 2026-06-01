"""
Compute e using Monte Carlo simulation with variance reduction techniques.

Classic result: Draw U_1, U_2, ... ~ Uniform(0,1) until sum > 1.
The count N satisfies E[N] = e.

Variance reduction methods:
1. Plain MC
2. Antithetic variates
3. Control variate (using U1 as control, E[U1] = 0.5)
4. Stratified sampling on U1
"""

import random
import math
import statistics

random.seed(42)
N = 1_000_000

def count_uniforms(rng=random):
    """Count how many U(0,1) draws until their sum exceeds 1."""
    s = 0.0
    n = 0
    while s <= 1.0:
        s += rng.random()
        n += 1
    return n

# ── 1. Plain Monte Carlo ──────────────────────────────────────
plain = [count_uniforms() for _ in range(N)]
e_plain = statistics.mean(plain)
var_plain = statistics.variance(plain) / N

# ── 2. Antithetic Variates ────────────────────────────────────
anti_estimates = []
for _ in range(N // 2):
    us = []
    s = 0.0
    while s <= 1.0:
        u = random.random()
        us.append(u)
        s += u
    n1 = len(us)
    # Antithetic path: use (1-u)
    s2 = 0.0
    j = 0
    while s2 <= 1.0 and j < len(us):
        s2 += (1.0 - us[j])
        j += 1
    while s2 <= 1.0:
        s2 += (1.0 - random.random())
        j += 1
    n2 = j
    anti_estimates.append((n1 + n2) / 2.0)

e_anti = statistics.mean(anti_estimates)
var_anti = statistics.variance(anti_estimates) / (N // 2)

# ── 3. Control Variate ────────────────────────────────────────
# Use U1 as control variate with E[U1] = 0.5
cv_data = []
for _ in range(N):
    u1 = random.random()
    s = u1
    n = 1
    while s <= 1.0:
        s += random.random()
        n += 1
    cv_data.append((n, u1))

ns = [d[0] for d in cv_data]
u1s = [d[1] for d in cv_data]
cov_nu1 = statistics.covariance(ns, u1s)
var_u1 = statistics.variance(u1s)
c_star = cov_nu1 / var_u1

ctrl_adjusted = [ns[k] - c_star * (u1s[k] - 0.5) for k in range(N)]
e_ctrl = statistics.mean(ctrl_adjusted)
var_ctrl = statistics.variance(ctrl_adjusted) / N

# ── 4. Stratified Sampling ────────────────────────────────────
num_strata = 10
strata_means = []
per_stratum = N // num_strata

for i in range(num_strata):
    lo = i / num_strata
    hi = (i + 1) / num_strata
    counts = []
    for _ in range(per_stratum):
        u1 = random.uniform(lo, hi)
        s = u1
        n = 1
        while s <= 1.0:
            s += random.random()
            n += 1
        counts.append(n)
    strata_means.append(statistics.mean(counts))

e_strat = sum(strata_means) / num_strata  # equal-weight strata
# Variance estimate for stratified: sum of (within-stratum var / per_stratum) / num_strata^2
strata_vars = []
for i in range(num_strata):
    lo = i / num_strata
    hi = (i + 1) / num_strata
    counts = []
    for _ in range(per_stratum):
        u1 = random.uniform(lo, hi)
        s = u1
        n = 1
        while s <= 1.0:
            s += random.random()
            n += 1
        counts.append(n)
    strata_vars.append(statistics.variance(counts) / per_stratum)
var_strat = sum(strata_vars) / (num_strata ** 2)

# ── Results ────────────────────────────────────────────────────
print("=" * 64)
print("  e Estimation via Monte Carlo with Variance Reduction")
print(f"  Samples: {N:,}  (antithetic uses {N//2:,} pairs)")
print("=" * 64)
print(f"  True e            = {math.e:.10f}")
print()
print(f"  1. Plain MC")
print(f"     e estimate     = {e_plain:.10f}")
print(f"     error          = {abs(e_plain - math.e):.10f}")
print(f"     estimator var  = {var_plain:.2e}")
print()
print(f"  2. Antithetic Variates")
print(f"     e estimate     = {e_anti:.10f}")
print(f"     error          = {abs(e_anti - math.e):.10f}")
print(f"     estimator var  = {var_anti:.2e}")
print(f"     variance ratio = {var_anti/var_plain:.4f}  (lower is better)")
print()
print(f"  3. Control Variate (c* = {c_star:.6f})")
print(f"     e estimate     = {e_ctrl:.10f}")
print(f"     error          = {abs(e_ctrl - math.e):.10f}")
print(f"     estimator var  = {var_ctrl:.2e}")
print(f"     variance ratio = {var_ctrl/var_plain:.4f}  (lower is better)")
print()
print(f"  4. Stratified Sampling ({num_strata} strata)")
print(f"     e estimate     = {e_strat:.10f}")
print(f"     error          = {abs(e_strat - math.e):.10f}")
print(f"     estimator var  = {var_strat:.2e}")
print(f"     variance ratio = {var_strat/var_plain:.4f}  (lower is better)")
print("=" * 64)

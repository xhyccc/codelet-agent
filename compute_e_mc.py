"""
Estimate e using Monte Carlo with variance reduction.

Method: Let N = min{n : U_1 + ... + U_n > 1} for U_i ~ Uniform(0,1).
Then E[N] = e. (Proof: E[N] = sum_{n>=0} P(N>n) = sum_{n>=0} P(S_n <= 1) = sum_{n>=0} 1/n! = e)

Variance reduction: antithetic variates.
If U_i gives N, then 1-U_i gives N' which is negatively correlated with N.
"""

import numpy as np


def sample_n(rng: np.random.Generator):
    """Sample one N = min{n: sum of n uniforms > 1}."""
    total = 0.0
    n = 0
    while total <= 1.0:
        total += rng.random()
        n += 1
    return n


def sample_n_antithetic_pair(rng: np.random.Generator, batch=1000):
    """
    Generate a pair (N, N_antithetic) using the same uniforms and 1-uniforms.
    We generate uniforms in batches to avoid per-call overhead.
    """
    # For path U: running sum
    s = 0.0
    s_anti = 0.0
    n = 0
    idx = batch  # force first fetch
    us = None
    while True:
        if idx >= batch:
            us = rng.random(batch)
            idx = 0
        u = us[idx]
        idx += 1
        s += u
        s_anti += (1.0 - u)
        n += 1
        if s > 1.0 and s_anti > 1.0:
            # Both paths terminated at same step
            return n, n
        if s > 1.0:
            # Only direct path terminated; antithetic still going
            n_anti = n
            while s_anti <= 1.0:
                if idx >= batch:
                    us = rng.random(batch)
                    idx = 0
                u = us[idx]
                idx += 1
                s_anti += (1.0 - u)
                n_anti += 1
            return n, n_anti
        if s_anti > 1.0:
            # Only antithetic terminated; direct still going
            n_direct = n
            while s <= 1.0:
                if idx >= batch:
                    us = rng.random(batch)
                    idx = 0
                u = us[idx]
                idx += 1
                s += u
                n_direct += 1
            return n_direct, n


def main():
    rng = np.random.default_rng(42)
    M = 200_000  # number of MC samples

    # --- Basic Monte Carlo ---
    Ns = np.array([sample_n(rng) for _ in range(M)], dtype=np.float64)
    mean_basic = Ns.mean()
    var_basic = Ns.var(ddof=1)
    se_basic = np.sqrt(var_basic / M)

    # --- Antithetic variates ---
    pairs = np.array([sample_n_antithetic_pair(rng) for _ in range(M // 2)], dtype=np.float64)
    antithetic_means = pairs.mean(axis=1)
    mean_anti = antithetic_means.mean()
    var_anti = antithetic_means.var(ddof=1)
    se_anti = np.sqrt(var_anti / (M // 2))

    print(f"True e            = {np.e:.8f}")
    print()
    print(f"Basic MC ({M:,} samples)")
    print(f"  Estimate        = {mean_basic:.8f}")
    print(f"  Bias            = {mean_basic - np.e:.8f}")
    print(f"  Sample variance = {var_basic:.6f}")
    print(f"  Std error       = {se_basic:.8f}")
    print(f"  95% CI          = [{mean_basic - 1.96*se_basic:.8f}, {mean_basic + 1.96*se_basic:.8f}]")
    print()
    print(f"Antithetic variates ({M//2:,} pairs = {M:,} paths)")
    print(f"  Estimate        = {mean_anti:.8f}")
    print(f"  Bias            = {mean_anti - np.e:.8f}")
    print(f"  Sample variance = {var_anti:.6f}")
    print(f"  Std error       = {se_anti:.8f}")
    print(f"  95% CI          = [{mean_anti - 1.96*se_anti:.8f}, {mean_anti + 1.96*se_anti:.8f}]")
    print()
    reduction = (1 - var_anti / var_basic) * 100
    print(f"Variance reduction = {reduction:.1f}%")


if __name__ == "__main__":
    main()

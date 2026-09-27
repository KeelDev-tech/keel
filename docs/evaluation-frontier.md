# Evaluation frontier

`keel_eval.frontier.run_benchmark()` executes the existing local model response
validator over 18 synthetic cases in six protocol families: transport status,
model identity, claim coverage, verdict/findings consistency, attempted tool
calls, and strict JSON. Valid responses include all three model verdicts; the
benchmark's PASS means protocol acceptance, not agreement with the model.
Each family includes a valid response, a rejected
response, and missing evidence. The default run executes 432 callback calls:
18 cases × 2 repeated views × 3 repeats × 2 runners × 2 comparisons.

The compared baselines always return PASS or always return ABSTAIN. Neither is
a competitor. The candidate calls Keel's actual response parser and assessment
validator. No model server is contacted and no generated tool call is executed.
These checks establish protocol behavior only; they do not establish that a
model's claims are true, that applications succeed, or that Keel leads a market.

The fixture is public and synthetic. The three variants in a family share a
single template cluster, as do their repeated views. Six template clusters are
not six authenticated independent real-world observations. Every report remains
`DIAGNOSTIC_ONLY`, `production_qualified=false`, `execution_authorized=false`.

## Existing experiment diagnostics

`audit_paired(plan, dataset, trials, alpha=.05)` first runs the existing frozen
plan and transcript validation. For this stricter API, each case must have
exactly one tag naming its task family; the original dataset digest freezes it.
It rejects identical subject bytes assigned to different clusters. Hash checks
cannot detect semantic duplication or authenticate statistical independence.

The report contains per-outcome confusion matrices, macro recall, family and
outcome accuracy, paired accuracy gain, unsafe PASS, unnecessary abstention, and
errors. Returning ABSTAIN on every case earns zero recall on PASS and FAIL,
even when an imbalanced workload makes raw accuracy look good. Errors remain
in all applicable denominators. Empty strata yield vacuous bounds, not zero risk.

## Conservative sequential bounds

Each metric first averages repeats, fault views, and related cases within its
declared cluster. Independent sample size is the number of completed clusters,
never the number of callback invocations. The estimand is the equal-weight mean
of the cluster scores for that predeclared stratum, which can differ from the
raw trial-weighted accuracy displayed in the confusion matrix.

For a cluster score bounded in `[a,b]`, at cluster count `n` the implementation
allocates `delta_n = alpha / (M*n*(n+1))` and returns the clipped interval

```
mean ± (b-a) * sqrt(log(2/delta_n)/(2*n))
```

`M=66` reserves baseline accuracy, candidate accuracy, and paired gain for the
overall stratum, all three labels, all 16 allowed families, plus six risk
intervals. Missing families in a sampled prefix do not replenish this budget.
Family names and definitions must still be predeclared. The sum of
`1/(n*(n+1))` over all positive integers is one. Applying Hoeffding's two-sided
bound and a union bound over all sample counts and all `M` metrics therefore
gives a combined error bound of `alpha`, conditional on the sampling assumptions.
This is our conservative derivation, not an implementation of the sharper
mixture or stitching boundaries in the confidence-sequence literature.

The candidate, metric definitions, cluster grouping, and sampling order must be
fixed before observing outcomes. Clusters must be independent; related views
must stay together. Adaptive example selection, candidate tuning on the same
holdout, splitting correlated clusters, or spending a fresh alpha budget for
every candidate invalidates the claimed population interpretation. Hashes and
the report cannot enforce these conditions. The two planned smoke comparisons
split a total alpha budget of .05 equally; their synthetic bounds are illustrative.

`zero_failure_sample_size(maximum_rate=.01, alpha=.05)` additionally reports the
299 independent Bernoulli opportunities needed for a **single planned look**
with zero observed failures to put the one-sided exact upper limit below 1%.
Its formula is `ceil(log(alpha)/log(1-maximum_rate))`; the resulting upper limit is
`1-alpha**(1/n)`. It does not count repeated views as independent opportunities
and is not valid as an unadjusted repeated-peeking rule.

## Primary references

- W. Hoeffding (1963), *Probability Inequalities for Sums of Bounded Random
  Variables*, Theorem 2. Author manuscript:
  https://repository.lib.ncsu.edu/server/api/core/bitstreams/d0e6ed15-3e1c-432f-8419-e55ffb6f3171/content
- S. R. Howard, A. Ramdas, J. McAuliffe, J. Sekhon (2021), *Time-uniform,
  nonparametric, nonasymptotic confidence sequences*:
  https://arxiv.org/abs/1810.08240 . This motivates the distinction between
  fixed-time intervals and inference that remains valid over repeated looks.
- NIST Dataplot, *Exact Binomial*, one-sided exact binomial confidence limits:
  https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbino.htm .

There are no added runtime dependencies or paid-service requirements.

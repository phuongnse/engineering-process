# Performance observations

This page preserves historical measurements for maintainers. They describe a
specific comparison and its limits; they are not a latency target, a production
promise, or evidence that a future host will behave the same way.

## 3.0.0 follow-up

The measurements below were taken on 2026-09-17 with the same Windows workspace
runtime and checked-in commands:

| Scenario | Baseline 7dd2125 | Candidate 1729c19 | Interpretation |
| --- | ---: | ---: | --- |
| Development profile (run_test_suite.py) | 341.098s; 314 tests | 335.331s; 320 tests | Assurance changed because six regressions were added; no performance improvement is claimed. |
| Four review checks | 38.576s, direct commands | 35.827s, lifecycle profile | Close but not identical wrapper paths; no process-overhead improvement is claimed. |
| Continuation/reuse fixture | 5.803s | 5.561s | Same one-test fixture; no meaningful change. |
| Correction-cycle fixture | 3.992s | 4.090s | Same one-test fixture; no meaningful change. |
| Adoption convergence fixture | 1.185s | 1.107s | Same one-test fixture; no meaningful change. |

The candidate lifecycle recorded 370.936s for development and 35.827s for
review; the development value includes the bounded lifecycle runner around the
consumer suite. The process does not add a cache or telemetry system. Host
variance, cache state, and the different test count prevent a stronger
end-to-end optimization claim.

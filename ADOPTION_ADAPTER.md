# Process adoption adapter

This document owns the concrete adoption implementation behind the portable release
and adoption sequence in [`VERSIONING.md`](VERSIONING.md). Consumer policy remains
declarative; the adapter owns acquisition, containment, stable input identity, and the
atomic managed-asset transaction.

## Stable authority input

The managed adoption runner accepts the complete dependency lock as its only authority
input. Every supplied path component is inspected without following an untrusted
transition: symbolic links, junctions, reparse points, and non-regular inputs are
rejected. Component identities are checked again while one bounded private snapshot is
created outside the source checkout, so a concurrent parent retarget cannot change the
bytes being authorized.

The snapshot has explicit file-count, individual-size, aggregate-size, and stability
bounds. Installation and adoption application are both bound to the same snapshot
digest. A live input change, unstable copy, or identity mismatch fails before managed
project state is replaced.

## Installation and execution

The current package adapter performs a binary-only, complete hash-locked installation
in a private temporary environment. It executes the installed target authority rather
than importing code from the checkout under development. Project arguments remain
argument arrays and no dependency resolution is delegated to the consumer source.

All child execution uses the portable finite-task boundary. The current POSIX backend
owns a process group, and the current Windows backend owns a kill-on-close Job Object
attached during process creation. Timeout, interruption, setup failure, or escaped
descendants trigger bounded cleanup and fail adoption; a successful child exit alone
does not override cleanup failure.

## Atomic materialization

The installed target authority validates the source and target versions, authority
lock, selected managed assets, and any consumer-owned declarative migration. The
process lock, managed instructions, review-description contract, adoption runner,
selected skills, and required project configuration share one rollback transaction.
Optional capabilities are preserved or activated only when declared.

The fully materialized review candidate is the adoption evidence. Independent review
and explicit integration complete adoption; there is no post-integration
synchronization. The current implementation sources are `templates/adopt-process.py`
and its platform helper, with regression ownership in the adoption-runner and
supervision test suites. Active N-1 managed copies are updated only by a later governed
self-adoption.

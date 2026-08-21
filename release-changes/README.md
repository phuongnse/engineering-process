# Release change fragments

Every feature or fix with distributable public impact adds one `<id>.json` fragment
validated by `schemas/release-change.schema.json`. The Release PR automation consumes
all fragments from `main`, derives the next SemVer from their highest-impact type,
copies the ordered records into `release.json`, and deletes the consumed fragments in
the generated Release PR.

Use `fix` for backward-compatible corrections, `capability` for backward-compatible
public additions, and `breaking` for incompatible behavior. Breaking changes require
migration guidance. Keep `surfaces` sorted and never use a fragment as a progress
counter or release-date marker.

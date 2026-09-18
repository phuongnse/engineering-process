<!-- engineering-process:start -->
## Engineering process

For non-trivial delivery work, enter through the managed deliver-change skill and follow
the processctl lifecycle: start, plan, implement, verify, independent review, finish.

This repository owns product decisions, domain rules, exact argument-array commands,
merge policy, and release authority. The process owns only lifecycle transitions,
managed skills, evidence freshness, and rejection of self-review.

When a change alters knowledge needed by users, operators, developers, or
maintainers, identify the consumer-owned authoritative source during planning and
update it with the implementation when needed. Review findability and usefulness
for a representative reader; do not create a document or checkbox when there is
no durable reader impact.

Do not edit .agents/skills or .process/adopt-process.py by hand. They are replaced by
the hash-locked engineering-process adoption in a dependency pull request.
<!-- engineering-process:end -->

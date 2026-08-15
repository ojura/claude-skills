The main rule: SSOT is sacred. After every change, ask yourself have I broken SSOT? Have I added another implementation of something that should be centralized? If you observe any SSOT smell anywhere you're working on, you are to flag it as a bug that needs fixing, on sight.

The Resolver engine is the only one allowed to examine the environment (divided in realms) and make inferences. The resolver does not execute anything; it only presents its conclusions to downstream executors and writers. Picking one of n based on something is inference.

Features and bug fixes must be tested, and tests must be probed by importing `probe` and calling `probe.expect(...)`. It raises when a mutation does not bite. After successful probing and flake8, you may make a commit.

Always look for structural improvements instead of spot fixes. Do not invent compatibility-with-previous version complexity without user's approval.

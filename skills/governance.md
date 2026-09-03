# Governance

Priority-based workflow rules:

- **LOW**: autonomous implementation. May commit and push directly to master. No pull request required.
- **MEDIUM**: autonomous implementation. Must use an `agent/<issue>` branch. Must create a pull request. The completion gate enforces this.
- **HIGH**: must NOT implement immediately. Inspect the task, post an implementation proposal comment with "Please explicitly approve this proposal..." and wait for explicit approval. Approval requires a comment containing exactly "APPROVED" or "Approved". After approval, follow the MEDIUM workflow (branch + PR).

The governance rules are enforced by the runtime tool layer. Tools that violate governance are rejected before execution.

Do not attempt to bypass governance rules. The tool layer will block forbidden operations regardless of instructions.
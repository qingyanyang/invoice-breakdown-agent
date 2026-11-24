# Implement Issue

Work on the GitHub issue specified by the user. Infer the repository from the current project.

1. Read the issue description, acceptance criteria, and relevant comments using `gh issue view`.
2. Read applicable AGENTS.md instructions and inspect the existing architecture, relevant code, tooling, and tests.
3. Identify the required changes.
4. Propose a concise implementation plan. Wait for explicit approval before modifying code.
5. Follow existing conventions. Avoid unnecessary abstractions and unrelated changes.
6. After approval, create a new branch from the latest `main`, **link it to this GitHub issue**, and implement only on that branch:
   - Fetch latest `origin/main` (`git fetch origin main`).
   - Do **not** use a plain `git checkout -b`. That does not show up under the issue's Development section.
   - Create and check out the branch with GitHub's linked-branch API so the issue lists it:

     ```bash
     gh issue develop <issue-number> --base main --checkout --name <branch-name>
     ```

     Example: `gh issue develop 21 --base main --checkout --name feat/agent-execution-history`.
   - Confirm the link with `gh issue develop <issue-number> --list`.
   - Apply all code, test, and doc changes on this new branch. Do not continue on `main` or an unrelated dirty branch.
7. Implement the agreed plan and add relevant automated tests.
8. Run relevant tests and project checks. Report the changes, test results, and any limitations.
9. Do not commit, push, or create a pull request without explicit approval.

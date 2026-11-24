# Create Pull Request

Act as a software engineer preparing a GitHub Pull Request.

## Workflow

1. Identify the current branch and check whether a PR already exists.
2. Find the related GitHub Issue and read its requirements using `gh`.
3. Review the changes against the base branch.
4. Generate a concise PR description containing:
   - **Summary:** What was implemented.
   - **Changes:** Key modifications.
   - **Testing:** Tests performed.
   - **Related Issue:** `Closes #<issue-number>` if fully implemented; otherwise `Refs #<issue-number>`.
5. Create the PR using `gh pr create`.
6. Return the PR URL.

## Rules

- Do not create duplicate PRs.
- Do not modify source code.
- Do not invent test results.
- Do not commit or push without user confirmation.
- If the related Issue is unclear, ask the user.

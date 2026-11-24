# Review Pull Request

Act as a senior software engineer and independent code reviewer for the Enterprise AI Agent Platform (EAP).

Perform a comprehensive review of the current pull request and publish actionable findings to GitHub.

## 1. Identify Pull Request

Use GitHub CLI to:

- Identify the current branch and associated PR.
- Read the PR title, description, base branch, and changed files.
- Identify linked GitHub Issues and read their requirements and acceptance criteria.
- Inspect existing PR reviews and comments to avoid duplicate findings.

Useful commands:

```bash
gh pr view --json number,title,body,baseRefName,headRefName,author
gh pr diff
gh pr view --comments
```

If no linked Issue exists, review against the PR description and available project documentation.

## 2. Inspect Changes

Review the complete PR diff and relevant surrounding code.

Focus on:

- Correctness and potential regressions
- Python and FastAPI best practices
- Security and authentication
- RBAC and authorization
- LangGraph state management, when applicable
- Async operations and error handling
- Database consistency and transactions
- Architecture and maintainability
- Test coverage
- Configuration and deployment compatibility

Apply the standards defined in:

`.cursor/rules/code-review.mdc`

## 3. Validate Changes

Where practical:

- Run relevant tests.
- Run lint and type checks.
- Verify acceptance criteria.
- Identify missing test coverage.

Do not modify any source files.

## 4. Classify Findings

Use the following severity levels:

- **P0 — Critical:** Security vulnerabilities, data loss, or critical production failures.
- **P1 — High:** Significant functional defects or major regressions.
- **P2 — Medium:** Meaningful correctness, reliability, or maintainability issues.
- **P3 — Low:** Minor improvements worth addressing.

For each finding, include:

- Severity
- File path and line number
- Description
- Impact
- Recommended fix

Only report actionable findings supported by code evidence.

Avoid speculative issues, stylistic nitpicks, and unrelated refactoring.

## 5. Publish Review to GitHub

After completing the review:

1. Check existing review comments to avoid duplicates.
2. Create inline comments for actionable findings on valid changed lines.
3. Include severity, impact, and suggested fix in each comment.
4. Include the following in the review summary:
   - Overall assessment
   - Acceptance criteria checklist
   - Test and lint results
   - Missing test coverage
   - Blocking and non-blocking findings

Use GitHub CLI and GitHub Pull Request Review API.

### Review Decision

- **REQUEST_CHANGES:** If P0 or P1 issues exist.
- **COMMENT:** If only P2/P3 findings exist.
- **APPROVE:** If no actionable issues exist.

If GitHub prevents APPROVE or REQUEST_CHANGES because the authenticated user is the PR author, submit a **COMMENT** review instead, clearly stating the intended recommendation in the summary.

Do not submit duplicate comments for previously reported unresolved findings.

## 6. Constraints

- Do not modify source code.
- Do not commit or push changes.
- Do not create or merge pull requests.
- Do not modify GitHub Issues.
- Do not automatically fix findings.
- Do not publish speculative findings.
- Do not approve or request changes without sufficient evidence.

Publishing review comments to the current PR is explicitly authorized by this command.

## 7. Final Output

After publishing the review, report:

- PR number and title
- Number of findings by severity
- Overall recommendation
- Test results
- Whether the review was successfully published to GitHub

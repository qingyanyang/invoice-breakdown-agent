# Fix Pull Request Review Comments

Act as a senior software engineer responsible for addressing GitHub pull request review feedback.

## 1. Identify Pull Request

Use GitHub CLI to:

- Identify the current branch and associated pull request.
- Read the PR description and linked GitHub Issues.
- Retrieve all review comments, including inline comments.
- Retrieve existing review threads and their resolution status.

Useful commands:

```bash
gh pr view --json number,title,body,headRefName,baseRefName
gh api repos/{owner}/{repo}/pulls/{pr_number}/comments --paginate
gh api repos/{owner}/{repo}/pulls/{pr_number}/reviews --paginate
```

Use GitHub GraphQL API when necessary to retrieve review thread resolution status.

## 2. Analyze Review Findings

For each unresolved review thread:

1. Read the comment and surrounding code.
2. Determine whether the finding is valid.
3. Check whether it has already been fixed.
4. Classify it as:
   - Valid — Requires a code change.
   - Already Fixed — No additional changes required.
   - False Positive — Explain why the finding is incorrect.
   - Needs Clarification — Insufficient information to proceed.

Do not blindly implement every suggestion.

## 3. Implement Fixes

For confirmed findings:

- Make the smallest appropriate code changes.
- Preserve existing architecture and conventions.
- Avoid unrelated refactoring.
- Add or update tests where necessary.
- Verify the changes against the original GitHub Issue.

Do not introduce new features outside the PR scope.

## 4. Validate Fixes

Run relevant:

- Unit tests
- Integration tests
- Lint checks
- Type checks

If a check cannot run, explain why.

## 5. Report Results

Provide:

- Fixed findings
- Already-fixed findings
- False positives
- Findings requiring clarification
- Files changed
- Test results
- Remaining risks

## 6. GitHub Updates

After validation:

1. Summarize the fixes.
2. Do not resolve review threads that have not been addressed.
3. Do not automatically dismiss reviews.
4. Do not merge the PR.
5. Do not create additional pull requests.

Do not commit or push changes unless explicitly requested by the user.

## 7. Final Output

Report whether the PR is ready for another review.

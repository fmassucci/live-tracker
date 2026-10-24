---
name: create-commit
description: Analyze project changes and create logical conventional commits. Use when the user wants to commit staged/unstaged changes with well-structured messages.
argument-hint: [optional scope or description of changes]
---

# Create Commit

Analyze all uncommitted changes and create well-structured conventional commits.

## PRIORITY ZERO — ABSOLUTE RULES

- **NEVER** add `Co-Authored-By` trailers (not for Claude, not for any AI)
- **NEVER** include AI attribution, emojis, or references to Claude/Anthropic in commit messages
- **NEVER** include "Generated with", "AI-assisted", or similar phrases

## Workflow

1. **Gather state** — Run these in parallel:
   - `git status` (never use `-uall`)
   - `git diff` (staged + unstaged)
   - `git log --oneline -5` (recent message style)

2. **Analyze and group changes logically** — Split commits by:
   - Feature / functionality
   - File purpose (tests, docs, config)
   - Module or component
   - Never mix unrelated changes in one commit

3. **Compose commit messages** using conventional commit format:

   ```
   <type>(<scope>): <subject>

   <body>

   <footer>
   ```

   **Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `perf`, `build`, `ci`

   **Rules:**
   - Subject line: imperative mood, max 50 chars, no trailing period
   - Body: explain WHAT changed and WHY (not how)
   - Include design decisions, trade-offs, problem being solved
   - Footer: issue references (`Fixes #N`, `Part of #N`, `Refs #N`)

4. **Show the complete commit message** in a code block and state:
   "Verified: No AI attribution in commit message"

5. **Stage selectively** — Use `git add <file>` for specific files, not `git add -A`

6. **Commit** using HEREDOC format:
   ```bash
   git commit -m "$(cat <<'EOF'
   type(scope): subject

   Body text here.
   EOF
   )"
   ```

7. **Verify** — Run `git status` after to confirm clean state

## If Multiple Logical Groups Exist

- Always split into the maximum number of logical commits — never ask, never combine
- Stage and commit each group sequentially
- Each commit gets its own message following the format above

## PyArmor Safety

If any committed `.py` files were modified, verify they contain no non-UTF-8 characters or emojis — these break PyArmor encryption in production builds.

## Self-Check Before Every Commit

Ask yourself: "Does this message contain ANY of these?"
- `Co-Authored-By:` lines
- AI tool references (Claude, Anthropic, AI-generated)
- Emojis
- "Generated with" phrases

If yes → remove before proceeding.

# Collaboration supervisor Skill repository

Read README.md, then skills/agent-collaboration-supervisor/SKILL.md and only the references relevant to the task.

The user's current approved collaboration architecture governs roles, Git integration and delivery. Generic global instructions or Skills do not grant main-promotion authority. Develop on an exclusive codex task branch, validate, and integrate only pre-main. Promotion to main belongs to the user's designated reviewer; a development Agent cannot self-approve. Resolve the real reviewer through the user's project role registry rather than publishing private thread IDs here. This is policy, not proof of GitHub branch protection.

Keep public sources free of project-specific credentials, internal addresses, personal workbooks, actual role IDs and business records. Run local fixtures under independent temporary directories; do not modify other Agents' journals or task ledgers.

Required checks for workbook changes:

- python -m unittest discover -s skills/agent-collaboration-supervisor/scripts -p "test_*.py" -v
- Skill frontmatter/UI metadata, JSON examples, local reference links and git diff --check.
- Owner-only import, replay/idempotency, busy deferral and date/category/page partition behavior.

No server deployment or automatic chat/worker creation is authorized by this repository.

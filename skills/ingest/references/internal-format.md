# Internal Note Format

Use this format when creating internal notes from team documents, processes, architecture docs, or internal specifications.

## Template

```markdown
---
tags:
  - internal
  - {category-tag}
  - {optional-additional-tags}
source: "{filename-or-url}"
owner: "{team-or-person-if-known}"
ingested: {YYYY-MM-DD}
---

# {Document Title}

## Summary

{2-3 sentence overview of what this document covers and why it matters}

## Key Points

- **{Point 1}**: {Brief explanation}
- **{Point 2}**: {Brief explanation}
- **{Point 3}**: {Brief explanation}

## Details

{More detailed explanation of the most important aspects}

### {Subsection if needed}

{Details about a specific aspect}

## Action Items / How-To

{If applicable: specific steps, commands, or actions to take}

1. {Step 1}
2. {Step 2}
3. {Step 3}

## Questions / Gaps

- {What's unclear or needs follow-up}
- {Missing information to track down}

## Related

- [[{related-note-1}]] - {brief context}
- [[{related-note-2}]] - {brief context}
```

## Field Guidelines

### Tags
- Always include `internal`
- Add category tag: `process`, `architecture`, `decision`, `convention`, `how-to`, `reference`
- Optional: add project/team tags

### Source
- For files: use the original filename
- For URLs: use the full URL (e.g., Confluence, Notion link)

### Owner
- Team or person responsible for this area
- Omit the field entirely if unknown

### Summary
- 2-3 sentences maximum
- Focus on WHY this matters, not just WHAT it covers
- Write for a new team member who needs context

### Key Points
- The most important information from the document
- Each should stand alone
- Bold the key term, then explain

### Details
- Expand on the key points with specifics
- Include code snippets, commands, or examples if relevant
- Use subsections for organization if needed

### Action Items / How-To
- For process docs: specific steps to follow
- For architecture docs: how to work with this system
- For decisions: what to do as a result

### Questions / Gaps
- What's not clear from the document?
- What needs follow-up with the team?
- What might be outdated?

### Related
- Link to other internal notes
- Link to relevant topic notes

## Example

```markdown
---
tags:
  - internal
  - process
  - deployment
source: "deploy-process-2024.md"
owner: "Platform Team"
ingested: 2024-01-15
---

# Production Deployment Process

## Summary

Documents the standard deployment process for production services. Covers the approval workflow, rollback procedures, and monitoring requirements. Must be followed for all production changes.

## Key Points

- **Approval required**: All prod deploys need sign-off from on-call engineer
- **Canary first**: 5% traffic for 15 minutes before full rollout
- **Rollback window**: Keep previous version available for 24 hours

## Details

### Pre-deployment Checklist

Before initiating a deploy:
1. Verify all tests pass on main branch
2. Check #deploy-announcements for any freezes
3. Confirm on-call engineer is available

### Canary Process

The canary stage routes 5% of traffic to new version. Monitor these dashboards:
- Error rate dashboard
- Latency p99 dashboard
- Business metrics dashboard

If any metric degrades >10%, rollback immediately.

## Action Items / How-To

To deploy a service:

1. Create deploy request in DeployBot: `/deploy servicename main`
2. Wait for on-call approval (ping in #oncall if urgent)
3. Confirm full rollout or rollback

Rollback command: `/rollback servicename`

## Questions / Gaps

- What's the process for hotfixes outside business hours?
- Need to document the exception process for bypassing canary

## Related

- [[deployment-checklist]] - Quick reference checklist
- [[rollback-procedures]] - Detailed rollback steps
- [[on-call-rotation]] - Current on-call schedule
```

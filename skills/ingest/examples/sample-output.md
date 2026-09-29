# Sample Ingest Output

This shows an example of what the `/ingest` skill creates when processing a document.

## Example Session

**User input:**
```
/ingest ~/Downloads/rest-api-design-guide.pdf --title "REST API Design Best Practices"
```

**Created note:** `literature/lit-rest-api-design-best-practices.md`

---

## Sample Note Content

```markdown
---
tags:
  - literature
  - api-design
  - rest
source: "rest-api-design-guide.pdf"
ingested: 2024-01-15
---

# REST API Design Best Practices

## Summary

A comprehensive guide to designing RESTful APIs that are consistent, intuitive, and easy to maintain. Covers resource naming, HTTP methods, error handling, versioning, and pagination patterns used by major tech companies.

## Key Concepts

- **Resource-oriented design**: URLs should represent resources (nouns), not actions. Use HTTP methods to indicate the action.
- **HATEOAS**: Hypermedia As The Engine Of Application State - responses include links to related resources and available actions.
- **Idempotency**: GET, PUT, DELETE should be idempotent. POST creates new resources and is not idempotent.
- **Consistent error format**: Use standard HTTP status codes with a consistent JSON error body structure.

## Highlights

Key design principles from the guide:

- Use plural nouns for collections: `/users` not `/user`
- Nest resources to show relationships: `/users/{id}/orders`
- Use query parameters for filtering/sorting: `/users?status=active&sort=created_at`
- Version in URL path for major versions: `/v1/users`
- Return 201 with Location header for POST creating resources
- Use 204 No Content for successful DELETE

Error response format recommended:
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid email format",
    "details": [...]
  }
}
```

## Questions

- How to handle breaking changes within a major version?
- What's the best pagination strategy for large datasets (offset vs cursor)?
- How to design APIs that need to return different representations of the same resource?

## Related

- [[api-versioning]] - Strategies for API version management
- [[http-status-codes]] - Reference for when to use which codes
- [[pagination-patterns]] - Comparison of offset vs cursor pagination
```

---

## What the Skill Does

1. **Extracts text** from the PDF using `pdftotext` or `pandoc`
2. **Reads config** from `~/.config/claude-note/config.toml` to find vault path
3. **Analyzes content** to identify key concepts, highlights, and questions
4. **Checks for duplicates** in existing vault notes
5. **Creates the note** at `{vault_root}/literature/lit-rest-api-design-best-practices.md`
6. **Confirms** what was created

## Internal Note Example

For internal documents, use `--internal`:

```
/ingest ~/work/auth-service-spec.md --internal
```

Creates: `internal/int-auth-service-spec.md` with internal note format.

## Dry Run Example

Preview extraction without writing:

```
/ingest paper.pdf --dry-run
```

Output shows:
- Extracted summary, concepts, highlights
- Target file path
- Any existing similar notes found
- No files are written

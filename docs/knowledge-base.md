# Knowledge Base Guide

## Overview

The Knowledge Base stores per-container pattern classifications that the AI uses during evaluations. It's the system's institutional memory of your environment.

## Classifications

| Type | Meaning | AI Behavior |
|------|---------|-------------|
| **Expected** | Known benign pattern | Don't lower health score |
| **Problem** | Confirmed issue | Always flag, score negatively |
| **Watch** | Needs monitoring | Extra attention, trend alerts |
| **Ignore** | Pure noise | Exclude from findings |

## How It Works

1. **Pattern Detection:** After each non-healthy eval, DockLlama extracts notable patterns
2. **Pending Patterns:** Patterns seen 3+ times are surfaced for classification
3. **User Classification:** You classify patterns via the API or dashboard
4. **Injection:** Classified patterns are injected into future AI evaluations

## API Endpoints

```
GET    /api/containers/{name}/knowledge          # List entries
POST   /api/containers/{name}/knowledge          # Add entry
PUT    /api/containers/{name}/knowledge/{id}     # Update
DELETE /api/containers/{name}/knowledge/{id}     # Delete

GET    /api/patterns/pending                     # Surfaced patterns
GET    /api/patterns/pending/counts              # Badge counts
POST   /api/patterns/pending/{id}/classify       # Classify → knowledge
DELETE /api/patterns/pending/{id}                # Dismiss
```

## Best Practices

- Write clear `user_explanation` values — the AI reads them during evals
- Use "Watch" for patterns you're unsure about until you gather more data
- Export knowledge between similar containers (e.g., two PostgreSQL instances)

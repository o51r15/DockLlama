# Prompt Customization

## Three Layers of Context

DockLlama uses three complementary systems to customize AI behavior:

### 1. Context Prompts
Free-text instructions appended to the system prompt. Tell the AI what this container does and what's normal.

```
This is a PostgreSQL database. Deadlock warnings (SQLSTATE 40P01)
are routine transaction collisions. Do not lower the health score.
```

### 2. Few-Shot Examples
Show the AI a sample log with the correct assessment. Most effective for small models.

```yaml
examples:
  - label: "Normal shutdown"
    log_snippet: "FATAL: terminating connection..."
    correct_score: 95
    correct_status: "healthy"
    reasoning: "Clean admin shutdown sequence"
```

### 3. Known Patterns (Metadata Tags)
Regex patterns that tag matching log lines with context before the AI sees them.

```yaml
known_patterns:
  - pattern: "deadlock detected"
    tag: "[ROUTINE: normal transaction collision]"
```

## Base Prompts

The system-level prompts (eval, digest, log_eval) can be customized via Settings → Prompts. Create variants and activate them without restarting.

## Knowledge Base (Dynamic)

Unlike the above (manual config), the Knowledge Base is populated from user feedback on detected patterns. Both systems inject into the same eval prompt and work together.

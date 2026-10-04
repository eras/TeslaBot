# Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for components, runtime flows,
configuration, persistence, integrations, and development entry points.

# Commit Policy

- After completing and verifying a task that changes files, always commit the
  task's changes without waiting for a separate commit request.
- Stage only files belonging to the task. Leave unrelated changes untouched,
  and never commit secrets or temporary/generated agent files.

# Logging Policy

- Preserve diagnostic information in logs. Do not remove payloads, command
  arguments, vehicle data, identifiers, exception messages, or tracebacks, or
  replace them with generic messages or exception class names only.
- Do not silence SDK loggers, disable their propagation, or lower existing
  diagnostic verbosity to reduce information available for troubleshooting.
- Retain detailed DEBUG logging and useful INFO lifecycle messages. Log failures
  with their context and exception traceback while preserving error handling.
- Do not introduce new redaction or logging suppression without explicit user
  approval. Existing authorization-command redaction predating multi-control
  support is retained; it is not a mandate to redact other diagnostics.
- Tests should verify useful diagnostic details remain available, rather than
  enforce blanket suppression of payloads or exception details.
- Detailed logs may contain sensitive data. Treat log files accordingly; do not
  solve that concern by silently reducing application observability.

# Agent Guide

This document defines the operational contract for LLM agents interacting with the pipeline.

## Stable actions

An agent may safely:

- load config,
- validate registry,
- run preflight,
- submit a pipeline run,
- inspect manifests,
- inspect routing decisions,
- inspect QA flags,
- update config values in a reviewed change.

An agent must not:

- infer model license eligibility from memory,
- silently swap in an unclassified provider,
- treat missing artifacts as success,
- treat mock artifacts as production success,
- overwrite a finished run directory without explicit operator intent.

## Required read order for model decisions

When selecting a backend, the agent must:

1. read `configs/model_registry.yaml`,
2. apply `production` or other active environment policy,
3. inspect provider enablement in `configs/app.example.yaml` or the real deployment config,
4. only then choose or override a provider.

## Error handling expectations

If a model is requested but is:

- absent from registry → return `UNCLASSIFIED_MODEL`
- in registry but blocked by policy → return `MODEL_BLOCKED_BY_POLICY`
- allowed but disabled in config → return `MODEL_DISABLED`
- enabled but not executable → return `MODEL_UNAVAILABLE`
- mock requested outside explicit mock mode → return `MOCK_NOT_ALLOWED`

## Preflight expectations

Before a non-mock provider run, an agent should run:

```bash
python -m open_sprite_pipeline.cli preflight --config configs/app.example.yaml
```

Preflight success means the configured paths and executables are present. It does
not prove model weights are correct or that inference works. A provider is only
real-run validated after it produces non-mock assets and rendered outputs.

## Run interpretation

A run is only considered successful when:

- `manifest.json` exists,
- at least one item result exists,
- each successful item has a primary asset path,
- each successful item has render outputs,
- QA metrics were computed.

## Human review triggers

An agent should recommend review when:

- all providers for an item failed,
- render completeness is below threshold,
- coverage is near zero,
- extraction confidence is low,
- a part-aware request was routed to a non-part-aware model,
- a restricted or unclassified provider was requested.

## Safe config changes

When editing config, the agent should preserve:

- `registry_path`
- artifact roots
- provider ids
- schema version values
- render preset names

## Minimal operator summary format

When summarizing a run, the agent should report:

- run id,
- number of items detected,
- provider chosen per item,
- whether fallbacks were used,
- review flags,
- manifest location.

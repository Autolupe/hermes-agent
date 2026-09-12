# Combined worker context and delivery safeguards

This candidate combines the published delivery and quiet-repair safeguards at `4948dc3cd818353a04d05edbeb151d19294a7b01` with the merged worker-context changes at `3cbc95adb5d1c11f26928f6426eb29547cb5873d` (native pull requests 9, 10 and 11). It creates one source set for the next installation review. No installed source or service was changed.

## Integration

The shared ancestor is `832e6daba3f5ae2a15cca9f6431224f2378708dd`. There was one conflicting file, `hermes_cli/kanban_workspace_policy.py`, at two points: the stored Git base and captured database context each needed a property, and workspace persistence needed to record both the selected base and permission to capture context. The resolution retains both sides.

Required dispatch now collects the original database task context after verified workspace persistence. It uses the original request and connection, retains the selected Git base, and captures the matching initial task view once. Requested skills must finish loading before agent startup; a failed or timed-out wait stays failed even if the background loader later finishes.

The incoming capture tests used an uncontracted persistent workspace. Their shared fixture now supplies an explicit non-code test contract, matching the existing delivery gate. No production admission rule was loosened. New integration cases verify capture with the selected base and refusal if that base changes before, during or after capture.

A definition comparison confirms the merge leaves all existing database lifecycle functions unchanged except the two dispatch callers and the worker-context builder. Completion, claim identity, held-worker fences, historical repair and board retirement remain the published implementation. The task-show tool retains its response format while sharing the immutable collector.

## Evidence and remaining work

All 159 focused checks passed across the incoming context/render/task-view/skill-loading tests and the selected-base tests. The final combined test receipt records the full exact-source validation and publication identity.

The installed native checkout remains `3a170fb835bda574bda5c44afabae61c94f34cd3` at this inspection. The separate activation work still owns the complete pre-import startup inputs, protected runtime enrollment/bootstrap and original-request trusted completion connection. Captured database context does not supply those missing pieces. Protected launch remains unsupported, and this integration does not waive it.

The watchdog installer is separately owned and still being finalized. The next installation source review must use this combined commit, retain the installed live-only repairs, and follow the supported parked upgrade and rollback procedure. A service restart, successful automatic worker or complete card is not claimed.

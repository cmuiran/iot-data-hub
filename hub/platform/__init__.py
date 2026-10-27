"""The platform plane: products, product template versions, device instances and their access.

Engineering devices come from ``config/devices/*.yaml`` (unchanged). Platform devices live in
the database (migration 0009) and are built from a product template version into the very same
device models (``hub.models``), so drivers, control, alarms and history treat both alike.
See docs/fa/platform/phase0-audit.md (ADR-001 .. ADR-010).
"""

"""
app/enums.py — Canonical job status enumerations.

Single source of truth for all job status strings used across:
  - app/api/routes/plan.py
  - app/api/routes/run.py
  - worker/executor.py
  - graphs/execution/nodes/exporter.py

str, Enum means .value is a plain string — passes through Pydantic and
pymongo transparently (no serialization shims needed).
"""

from enum import Enum


class PlanningJobStatus(str, Enum):
    PLAN_INITIATED     = "PLAN_INITIATED"
    NEED_CLARIFICATION = "NEED_CLARIFICATION"
    PLAN_READY         = "PLAN_READY"
    PLAN_EDIT_REQUESTED = "PLAN_EDIT_REQUESTED"
    PLAN_CANCELED      = "PLAN_CANCELED"


class ExecutionJobStatus(str, Enum):
    QUEUED          = "QUEUED"
    RUNNING         = "RUNNING"
    COMPLETED       = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED          = "FAILED"

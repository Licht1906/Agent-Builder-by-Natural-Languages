"""What marks a row as simulated — one convention, shared by writer and reader.

The seed script (`scripts/simulate_pm_runs.py`) stamps every run it invents
with a `conversation_id` starting with `SIM_PREFIX` and names every agent it
invents with `SIM_AGENT_PREFIX`. The dashboard counts those rows and says so
on screen; the decision endpoint refuses to resume a graph for one (there is
no checkpoint behind it); `--reset` deletes by the same marks.

A prefix rather than a column because simulation is a rehearsal of the
dashboard, not a feature of the schema: the day real runs exist the script
is retired and nothing in `run_costs` remembers it was ever there.
"""

from sqlalchemy import ColumnElement

from src.db.models import RunApproval, RunCost

SIM_PREFIX = "sim-"
SIM_AGENT_PREFIX = "[SIM] "


def is_simulated_conversation(conversation_id: str) -> bool:
    return conversation_id.startswith(SIM_PREFIX)


def simulated_run_costs() -> ColumnElement[bool]:
    return RunCost.conversation_id.like(f"{SIM_PREFIX}%")


def simulated_run_approvals() -> ColumnElement[bool]:
    return RunApproval.conversation_id.like(f"{SIM_PREFIX}%")

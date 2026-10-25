/* Who counts as an admin, mirroring the backend.
 *
 * The source of truth is `ADMIN_EQUIVALENT_ROLES` in `src/db/models.py`, which
 * `require_admin` enforces on every admin route. This file exists because the
 * two drifted: the nav asked for `role === "admin"` exactly, so `agent_admin` —
 * the role whose whole job is approving UC-1 agents — was served a sidebar with
 * no Admin section, while the very endpoints behind it answered 200 to that
 * same account. A gate narrower than the backend's does not protect anything;
 * it only hides work from the person meant to do it.
 *
 * Keep this in step with `ADMIN_EQUIVALENT_ROLES`. */
export const ADMIN_EQUIVALENT_ROLES = ["admin", "agent_admin"];

export const isAdminEquivalent = (role) =>
  ADMIN_EQUIVALENT_ROLES.includes(String(role || "").toLowerCase());

/* The PM (ADR-0051) is its own role, not an admin: it opens the PM control room
 * and nothing in the Admin section. Mirrors `PM_CONTROL_ROOM_ROLES` in
 * `src/db/models.py` — `{pm} ∪ ADMIN_EQUIVALENT_ROLES` — which `require_pm`
 * enforces on every `/pm/*` route. Keep the two in step. */
export const PM_ROLE = "pm";

export const isPm = (role) => String(role || "").toLowerCase() === PM_ROLE;

export const canOpenPmControlRoom = (role) => isPm(role) || isAdminEquivalent(role);

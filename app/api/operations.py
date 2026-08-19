"""What the verdict engine can actually do: the typed operation registry, over HTTP (#206, C2.4).

A rule names an operation; it never carries one. `AGENTS.md` §2.2 is the reason there is no field
anywhere in this API that could hold executable text, and this endpoint is the other half of that
arrangement: an author needs to be able to see which names exist and what operands each one takes,
or "select from the registry" is advice without a way to follow it.

**The list is the registry, read at request time.** `verdict.registry.REGISTRY` is walked when the
request arrives — there is no list of operation names in this module, and there is nothing here to
keep in step with `verdict/`. That matters more than it looks: a hand-written list that fell behind
would tell an author an operation does not exist when it does, or worse, that one exists when it
does not, and the second answer is only discovered when a real drawing is being checked.
`tests/api/test_rules_api.py` registers a spec the endpoint has never heard of and asserts it comes
back, so the derivation is proven rather than asserted.

**Where `register_all()` is called, and why here.** At import of this module, not per request.
Every module in `verdict/operations/` defines its specs and an installer, and until #206 nothing
called the installers — importing the package left the registry empty, so this endpoint would have
answered "there are no operations" with complete confidence. Importing is the right moment for two
reasons. It happens once, during `create_app`, so the registry is populated before the first request
rather than as a side effect of serving one; and a failure — two callers disagreeing about what is
registered — surfaces at startup, where it stops a deployment, instead of on whichever request
happened to arrive first. `register_all()` is idempotent, so calling it per request would also be
safe; it would just make the answer depend on work done while answering.

**Scope.** No `{project_id}`: the registry is a property of the engine, identical for every project,
and `app/models/` has no per-project operations to leak. It is still guarded — an unauthenticated
caller learns nothing about how this system decides anything.

Source: backend proposal §10.2, §11 · Design: `docs/DESIGN_PLATFORM.md` §4.1 ·
Verification: `tests/api/test_rules_api.py`
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.rules import READERS
from app.auth import Principal, require_role
from app.schemas.rules import OperationOut
from verdict.operations import register_all
from verdict.registry import REGISTRY

router = APIRouter(tags=["operations"])

# Installed at import, once, while the application is being built. See the module docstring for why
# this is here rather than inside the endpoint.
register_all()


@router.get(
    "/operations",
    response_model=list[OperationOut],
    summary="Every operation a rule may name, with its signature",
)
def list_operations(
    principal: Annotated[Principal, Depends(require_role(*READERS))],
) -> list[OperationOut]:
    """Every reviewed operation in the typed registry, in name order.

    Read from `verdict.registry.REGISTRY` as it stands right now. Nothing in this module names an
    operation, so this endpoint cannot fall out of step with the engine: an operation the engine can
    run appears here, and one it cannot does not.

    Each entry is a signature — a name, a version, whether it decides an outcome or derives an
    intermediate value, and what operands it takes. Not the implementation, and not a way to invoke
    one: this API never runs a check on request.
    """
    del principal  # the dependency is the check; the endpoint needs nothing from the caller

    return [
        OperationOut(
            name=spec.name,
            version=spec.version,
            kind=spec.kind.value,
            operands={name: arity.value for name, arity in spec.operands.items()},
        )
        for spec in sorted(REGISTRY.values(), key=lambda spec: spec.name)
    ]


__all__ = ["list_operations", "router"]

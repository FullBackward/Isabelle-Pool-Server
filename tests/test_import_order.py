"""D15: a session keeps the caller's import order.

In Isabelle the LATER import wins a shared short name (claude-work/2026-10-25-import-order:
``imports A B`` reads ``foo`` as ``B.foo``). IPS sorted a session's imports before it
wrote the wrapper theory, so Cartesian_Space (imports Transposition first) read
``transpose`` as ``Transposition.transpose`` and failed.
"""

from __future__ import annotations

from types import SimpleNamespace

from evaluation.scripts.theory_splitter import extract_imports
from server.app.api.v1.routes import sessions as session_routes
from server.app.services.session_manager_helpers import SessionManagerHelpersMixin
from server.repl.src.python.thy_init import ThyInit

CART = [
    "HOL-Combinatorics.Transposition",
    "HOL-Analysis.Finite_Cartesian_Product",
    "HOL-Analysis.Linear_Algebra",
]


def test_the_session_keeps_the_import_order_and_drops_duplicates():
    helpers = SessionManagerHelpersMixin()
    assert helpers._normalize_theories(CART + [" " + CART[0], ""]) == CART


def test_another_order_is_another_session():
    helpers = SessionManagerHelpersMixin()
    other = sorted(CART)
    key = helpers.build_dependency_key(CART, "HOL-Analysis")
    assert key == helpers.build_dependency_key(list(CART), "HOL-Analysis")
    assert key != helpers.build_dependency_key(other, "HOL-Analysis")
    assert helpers._build_wrapper_theory_name(
        CART, "HOL-Analysis"
    ) != helpers._build_wrapper_theory_name(other, "HOL-Analysis")


def test_the_wrapper_imports_in_the_given_order():
    text = ThyInit().generate_thy_file(CART, "wrap_x")
    quoted = [f'"{t}"' for t in CART]
    assert " ".join(quoted) in text


def test_heap_theories_follow_the_requested_ones(monkeypatch):
    monkeypatch.setattr(
        session_routes,
        "_resolve_heap_for_session",
        lambda *_: (["P.Base", "P.Top", "A.B"], "P", ["/d"], "x"),
    )
    request = SimpleNamespace(
        theories=["Z.Z", "A.B"],
        field=None,
        task_group=None,
        heap_session="P",
        project=None,
    )
    theories, *_ = session_routes._session_inputs(request, heap_pool=None)
    assert theories == ["Z.Z", "A.B", "P.Base", "P.Top"]


def test_the_splitter_keeps_the_header_order():
    header = (
        'theory Cartesian_Space\n  imports\n    "HOL-Combinatorics.Transposition"\n'
        '    "HOL-Analysis.Finite_Cartesian_Product"\n'
        '    "HOL-Analysis.Linear_Algebra"\n    "HOL-Analysis.Linear_Algebra"\nbegin\nend\n'
    )
    assert extract_imports(header) == CART

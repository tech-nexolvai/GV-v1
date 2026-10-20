"""The simple full check never throws away a paid answer (#1676): a Stop (or the runaway guard)
during the compare calls lets the calls in flight finish, verifies every answered drawing and
makes the outputs; every answer and blind look is saved with the chat and verified again at $0
(`run.replay`); the calls in flight are held against the per-answer cap. A real chat engine on a
made-up set, the compare calls and the blind look replaced; no model call, no money."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant import proof, set_pack
from app.review.assistant.chat_engine import ChatEngine, EngineSettings
from app.review.assistant.chat_request import MODELS
from app.review.assistant.chat_tools import ChatTools
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import compare as compare_module
from app.review.assistant.simple_check import run as run_module
from app.review.assistant.simple_check.schema import CompareOutcome, CompareResult, Pair
from app.review.assistant.simple_check.verify import BlindAnswer
from app.runs.rates import load_model_rates
from tests.review.assistant.chat_fakes import FakeLedger, FakeSandbox
from tests.review.assistant.test_simple_verify import FILE, _documents

RATES = load_model_rates(
    Path(__file__).resolve().parents[3] / "deploy" / "model_rates.us-east-1.json"
)
DOLLARS = 1_000_000
OVERALL = Pair(
    vendor_label="p2.D1",
    architect_label="p1.D1",
    measures="overall counter width",
    vendor_from="wall_face (left)",
    vendor_to="wall_face (right)",
    architect_from="wall_face (left)",
    architect_to="wall_face (right)",
    verdict="different",
    note="",
)


def _pdf(path: Path, pages: int = 4) -> None:
    page = canvas.Canvas(str(path), pagesize=(600, 850))
    for number in range(pages):
        page.drawString(100, 800, f"PAGE {number + 1}")
        page.line(50, 400, 550, 400)
        page.showPage()
    page.save()


def _pack(folder: Path) -> Path:
    folder.mkdir(parents=True)
    docs = _documents()
    docs["page_map"]["files"] = [FILE]
    for name, key in (
        ("dims.json", "dims"),
        ("page_map.json", "page_map"),
        ("pairs.json", "pairs"),
        ("page_texts.json", "page_texts"),
    ):
        (folder / name).write_text(json.dumps(docs[key]), encoding="utf-8")
    (folder / "status.json").write_text(
        json.dumps(
            {"state": "ready", "version": set_pack.PACK_VERSION, "done": ["page_map", "dims"]}
        ),
        encoding="utf-8",
    )
    return folder


class _Run:
    """A chat engine running a simple full check on the made-up set."""

    def __init__(self, tmp_path: Path, simple: dict[str, Any] | None = None) -> None:
        pack = _pack(tmp_path / "pack")
        self.pack = pack
        self.workspace = ChatWorkspace.open(tmp_path / "chat", set_pack=pack)
        _pdf(self.workspace.uploads / FILE)
        (self.workspace.root / "uploads.json").write_text(
            json.dumps([{"name": FILE, "pages": 4, "kind": "the vendor's shop drawings"}]),
            encoding="utf-8",
        )
        self.cancel = threading.Event()
        self.ledger = FakeLedger()
        self.files: list[dict[str, Any]] = []

        def register(fid: str, name: str, size: int, mime: str, sha: str) -> dict[str, Any]:
            self.files.append({"id": fid, "name": name})
            return {"id": fid, "name": name, "size": size, "mime": mime, "url": f"/f/{fid}"}

        self.engine = ChatEngine(
            settings=EngineSettings(
                model=MODELS["claude-opus-5-5"],
                effort="high",
                thinking=True,
                lead_max_turns=40,
                helper_max_turns=20,
                message_cap_micros=15 * DOLLARS,
                package_cap_micros=70 * DOLLARS,
                rates=lambda _key: RATES,
                mode="full_check",
                full_check_mode="simple",
                simple=simple or {},
            ),
            provider=SimpleNamespace(name="anthropic"),  # type: ignore[arg-type]
            tools=ChatTools(
                workspace=self.workspace,
                sandbox=FakeSandbox(self.workspace, lambda _c, _w: ("ok", 0)),
                page_count=4,
                results=None,
                register_file=register,
            ),
            ledger=self.ledger,
            emit=lambda _event, _data: None,
            history=(),
            user_content=[{"type": "text", "text": "Full check"}],
            package_spent=lambda: 0,
            read_picture=lambda pid: (self.workspace.pictures / f"{pid}.png").read_bytes(),
            cancel=self.cancel,
            sleep=lambda _seconds: False,
        )

    def findings(self, outcome: Any) -> list[dict[str, Any]]:
        return [
            b["finding"]
            for b in outcome.blocks
            if b.get("type") == "finding" and b["finding"].get("created_by") == "simple_check"
        ]

    def run_dir(self) -> Path:
        runs = sorted((self.workspace.root / run_module.SIMPLE_DIR).iterdir())
        return runs[-1]


def _answer(item: Any) -> CompareOutcome:
    labels = [label.ref for label in item.vendor.labels]
    pairs = (OVERALL,) if "p2.D1" in labels else ()
    return CompareOutcome(
        work_item=item.id,
        status="ok",
        result=CompareResult(
            work_item=item.id,
            pairs=pairs,
            other_items=(),
            unaccounted_labels=tuple(ref for ref in labels if ref != "p2.D1"),
        ),
        asked_model="claude-sonnet-5-5",
        model="claude-sonnet-5-5",
        effort="high",
        attempts=1,
        usage={"input_tokens": 9000, "output_tokens": 3000},
        cost_usd=0.05,
        seconds=1.0,
    )


class _Pictures:
    def __init__(self, folder: Path, uploads: Path) -> None:
        del folder, uploads

    def prefetch(self, items: Any) -> None:
        del items

    def pictures(self, item: Any) -> list[Any]:
        del item
        return [SimpleNamespace(picture=SimpleNamespace(tokens=4000))]


def _fake_compare(
    monkeypatch: pytest.MonkeyPatch, *, stop_after: int | None, cancel: threading.Event
) -> list[str]:
    """compare_all as the real one behaves: each item's pictures drawn first (the source), then
    its answer; after `stop_after` answers the reviewer presses Stop, and the items not started
    end failed while the one in flight still answers."""
    started: list[str] = []

    def compare_all(
        items: Any, *, source: Any, stop: threading.Event, on_outcome: Any, **_: Any
    ) -> list[Any]:
        done = []
        for n, item in enumerate(items):
            if stop.is_set():
                outcome = CompareOutcome(
                    work_item=item.id,
                    status="failed",
                    reason="the run was stopped before this drawing was compared",
                    asked_model="claude-sonnet-5-5",
                    effort="high",
                )
            else:
                source(item)
                started.append(item.id)
                if stop_after is not None and n == stop_after:
                    cancel.set()  # Stop pressed while this one is in flight
                    assert stop.wait(5)
                outcome = _answer(item)
            on_outcome(outcome)
            done.append(outcome)
        return done

    monkeypatch.setattr(compare_module, "compare_all", compare_all)
    monkeypatch.setattr(compare_module, "PictureSource", _Pictures)
    return started


def _agreeing(_engine: Any, verified: Any, _item: Any, _settings: Any) -> tuple[Any, str]:
    ends = verified.ends
    return (
        BlindAnswer(
            pick="D1",
            verdict="same_thing",
            vendor_from=ends["vendor_from"],
            vendor_to=ends["vendor_to"],
            architect_from=ends["architect_from"],
            architect_to=ends["architect_to"],
            vendor_ink_black="yes",
            model="claude-opus-5-5",
        ),
        "",
    )


def test_a_stop_mid_compare_keeps_and_verifies_every_paid_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    started = _fake_compare(monkeypatch, stop_after=1, cancel=check.cancel)
    looked: list[str] = []
    monkeypatch.setattr(
        blind_module, "look", lambda *a: looked.append("x") or _agreeing(*a)  # type: ignore[func-returns-value]
    )
    outcome = check.engine.run()
    assert outcome.stopped == "cancelled"
    work = next(b for b in outcome.blocks if b.get("type") == "work_items")
    statuses = {row["id"]: row["status"] for row in work["work_items"]}
    # The answer in flight when Stop came was kept; nothing new started after it.
    assert len(started) == 2 and all(statuses[i] == "answered" for i in started)
    assert [s for i, s in statuses.items() if i not in started] == ["failed"] * (len(statuses) - 2)
    # Every answered drawing verified: the red candidate is recorded, Not compared (no blind look
    # after a stop), never dropped, never red.
    found = check.findings(outcome)
    overall = next(f for f in found if f["vendor"] == '45 1/4"')
    assert overall["status"] == "check"
    assert "blind second look was not made (the check was stopped)" in overall["why"]
    assert looked == []
    assert "Stopped: 2 of 3 drawings were compared and verified" in outcome.text
    assert "got no blind second look (the check was stopped)" in outcome.text
    assert any("cloud drawing" in f["name"] for f in check.files)
    # Saved: every answer, and the candidate's look (not made), named in the block.
    folder = check.run_dir()
    assert work["run"] == folder.name
    assert sorted(p.stem for p in (folder / "compare").glob("*.json")) == sorted(statuses)
    saved = json.loads(next((folder / "blind").glob("*.json")).read_text())
    assert saved["outcome"] == "not_compared"
    # Usage: one row per answered drawing, charged to the answer.
    assert len(check.ledger.rows) == 2 and outcome.cost_micros > 0


def test_saved_answers_and_blind_looks_replay_to_the_same_items_at_no_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    assert outcome.stopped is None
    red = next(f for f in check.findings(outcome) if f["vendor"] == '45 1/4"')
    assert red["status"] == "different" and proof.proof_group(red) == "clouded"
    folder = check.run_dir()

    def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a replay makes no model call")

    monkeypatch.setattr(blind_module, "look", no_call)
    monkeypatch.setattr(compare_module, "compare_all", no_call)
    replayed = run_module.replay(
        folder, check.pack, main_file=FILE, read=lambda *_a: None, ink=lambda _l: None
    )
    items = [v for found in replayed.values() for v in found]
    again = next(v for v in items if v.vendor is not None and v.vendor.get("id") == "D1")
    assert again.outcome == "red"
    assert again.finding["critic"]["result"] == "passed"
    # Through the engine too: a full check naming the run makes no call and the same red.
    second = _Run(tmp_path / "again", {"replay": folder.name})
    target = second.workspace.root / run_module.SIMPLE_DIR / folder.name
    target.parent.mkdir(parents=True, exist_ok=True)
    (target).mkdir()
    for sub in ("compare", "blind"):
        (target / sub).mkdir()
        for path in (folder / sub).glob("*.json"):
            (target / sub / path.name).write_text(path.read_text())
    rerun = second.engine.run()
    reds = [f for f in second.findings(rerun) if proof.proof_group(f) == "clouded"]
    assert [f["vendor"] for f in reds] == ['45 1/4"']
    assert rerun.cost_micros == 0 and second.ledger.rows == []


def test_the_calls_in_flight_are_held_against_the_cap() -> None:
    """A drawing starts only when its cost, with what the calls in flight may still cost, fits
    the cap: with room for two, the third waits; each hold is let go when its answer comes."""
    hold = 2 * DOLLARS
    cap = 5 * DOLLARS
    spent = [0]
    engine = SimpleNamespace(
        _price=lambda _model, _tokens, _out: hold,
        _over=lambda estimate: "message" if spent[0] + estimate > cap else None,
    )
    run = run_module._Run(engine, run_module.SimpleOptions.of({}))  # type: ignore[arg-type]
    settings = SimpleNamespace(
        for_item=lambda _item: (MODELS["claude-sonnet-5-5"], "high"), max_tokens=64000
    )
    draw = run._guarded(
        lambda _item: [SimpleNamespace(picture=SimpleNamespace(tokens=1000))], settings
    )
    for name in ("a", "b"):
        draw(SimpleNamespace(id=name))
    assert run.reserved == 2 * hold
    with pytest.raises(run_module._NotStarted):
        draw(SimpleNamespace(id="c"))
    assert run.stop.is_set() and run.cap_hit == "message" and run.refused == {"c"}
    with pytest.raises(run_module._NotStarted):
        draw(SimpleNamespace(id="d"))  # once stopped, nothing more starts

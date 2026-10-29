"""Parameter declarations for ResearchStrategy files."""

from __future__ import annotations

import datetime as dt

import pytest

from q_backend.research import BoolParam, ChoiceParam, FloatParam, IntParam, ResearchStrategy, TimeParam
from q_backend.research.params import ParamError, coerce_params, declared_params, to_wire_spec


def _strategy(init_source: str, parameters: str | None, name: str = "Sample") -> type[ResearchStrategy]:
    """Build a ResearchStrategy subclass from source, so each test declares only what it needs."""
    lines = [
        "from __future__ import annotations",
        "import datetime as dt",
        "from q_backend.research import BoolParam, ChoiceParam, FloatParam, IntParam, ResearchStrategy, TimeParam",
        f"class {name}(ResearchStrategy):",
    ]
    if parameters is not None:
        lines.append(f"    parameters = {parameters}")
    lines.append(f"    def __init__(self{init_source}):")
    lines.append("        pass")
    lines.append("    def entry_strategy(self, frame, positions=()):")
    lines.append("        return None")
    namespace: dict[str, object] = {}
    exec("\n".join(lines), namespace)  # noqa: S102 - test-only source built from literals
    return namespace[name]  # type: ignore[return-value]


def test_complete_declarations_yield_specs_in_init_order() -> None:
    cls = _strategy(
        ", ma_period: int = 20, use_filter: bool = True, entry_after: dt.time = dt.time(17, 5), "
        "mode: str = 'fast', threshold: float = 1.5",
        "{"
        "'ma_period': IntParam(min=2, max=200, step=1, label='MA period', hint='bars'), "
        "'use_filter': BoolParam(label='Use filter'), "
        "'entry_after': TimeParam(label='Entry after'), "
        "'mode': ChoiceParam(choices=('fast', 'slow')), "
        "'threshold': FloatParam(min=0.5, max=3.0, step=0.1)}",
    )

    specs = declared_params(cls)

    assert [spec.name for spec in specs] == ["ma_period", "use_filter", "entry_after", "mode", "threshold"]
    wire = [to_wire_spec(spec) for spec in specs]
    assert wire[0] == {
        "name": "ma_period",
        "label": "MA period",
        "type": "int",
        "default": 20,
        "min": 2,
        "max": 200,
        "step": 1,
        "choices": None,
        "hint": "bars",
    }
    assert wire[1]["type"] == "bool" and wire[1]["default"] is True
    assert wire[2]["type"] == "time" and wire[2]["default"] == "17:05"
    assert wire[3]["type"] == "categorical" and wire[3]["choices"] == ["fast", "slow"]
    assert wire[4]["type"] == "float" and wire[4]["default"] == 1.5 and wire[4]["step"] == 0.1


def test_label_defaults_to_the_argument_name() -> None:
    cls = _strategy(", length: int = 5", "{'length': IntParam(min=1, max=50)}")

    assert to_wire_spec(declared_params(cls)[0])["label"] == "length"


def test_class_without_init_arguments_declares_nothing() -> None:
    cls = _strategy("", None)

    assert declared_params(cls) == ()


def test_undeclared_init_argument_is_named() -> None:
    cls = _strategy(", ma_period: int = 20, length: int = 3", "{'ma_period': IntParam(min=2, max=50)}")

    with pytest.raises(ParamError, match="'length'"):
        declared_params(cls)


def test_declaration_without_init_argument_is_named() -> None:
    cls = _strategy(", ma_period: int = 20", "{'ma_period': IntParam(min=2, max=50), 'ghost': IntParam(min=1, max=2)}")

    with pytest.raises(ParamError, match="'ghost'"):
        declared_params(cls)


def test_init_argument_without_default_is_named() -> None:
    cls = _strategy(", ma_period: int", "{'ma_period': IntParam(min=2, max=50)}")

    with pytest.raises(ParamError, match="'ma_period'.*default"):
        declared_params(cls)


@pytest.mark.parametrize(
    ("init", "declaration", "argument"),
    [
        (", n: int = True", "{'n': IntParam(min=0, max=5)}", "'n'"),
        (", flag: bool = 1", "{'flag': BoolParam()}", "'flag'"),
        (", when: dt.time = '17:00'", "{'when': TimeParam()}", "'when'"),
        (", mode: str = 'x'", "{'mode': ChoiceParam(choices=('a', 'b'))}", "'mode'"),
    ],
)
def test_default_must_match_declared_type(init: str, declaration: str, argument: str) -> None:
    cls = _strategy(init, declaration)

    with pytest.raises(ParamError, match=argument):
        declared_params(cls)


def test_default_outside_bounds_is_named() -> None:
    cls = _strategy(", n: int = 9", "{'n': IntParam(min=1, max=5)}")

    with pytest.raises(ParamError, match="'n'.*between 1 and 5"):
        declared_params(cls)


def test_time_default_must_be_whole_minutes() -> None:
    cls = _strategy(", when: dt.time = dt.time(17, 5, 30)", "{'when': TimeParam()}")

    with pytest.raises(ParamError, match="'when'.*whole minutes"):
        declared_params(cls)


def test_variadic_init_is_rejected() -> None:
    cls = _strategy(", *args, **kwargs", "{}")

    with pytest.raises(ParamError, match="\\*args|\\*\\*kwargs"):
        declared_params(cls)


def test_coerce_fills_defaults_and_converts_wire_values() -> None:
    cls = _strategy(
        ", ma_period: int = 20, use_filter: bool = True, entry_after: dt.time = dt.time(17, 5), " "mode: str = 'fast'",
        "{'ma_period': IntParam(min=2, max=200), 'use_filter': BoolParam(), "
        "'entry_after': TimeParam(), 'mode': ChoiceParam(choices=('fast', 'slow'))}",
    )
    specs = declared_params(cls)

    values = coerce_params(specs, {"entry_after": "09:30", "use_filter": False})

    assert values == {
        "ma_period": 20,
        "use_filter": False,
        "entry_after": dt.time(9, 30),
        "mode": "fast",
    }


@pytest.mark.parametrize(
    ("submitted", "message"),
    [
        ({"ma_period": "20"}, "'ma_period'.*integer"),
        ({"ma_period": True}, "'ma_period'.*integer"),
        ({"ma_period": 1}, "'ma_period'.*between 2 and 200"),
        ({"use_filter": 1}, "'use_filter'.*boolean"),
        ({"entry_after": "25:00"}, "'entry_after'.*HH:MM"),
        ({"entry_after": 930}, "'entry_after'.*HH:MM"),
        ({"mode": "medium"}, "'mode'.*one of"),
        ({"unknown": 1}, "'unknown'.*not a parameter"),
    ],
)
def test_coerce_rejects_invalid_values(submitted: dict, message: str) -> None:
    cls = _strategy(
        ", ma_period: int = 20, use_filter: bool = True, entry_after: dt.time = dt.time(17, 5), " "mode: str = 'fast'",
        "{'ma_period': IntParam(min=2, max=200), 'use_filter': BoolParam(), "
        "'entry_after': TimeParam(), 'mode': ChoiceParam(choices=('fast', 'slow'))}",
    )

    with pytest.raises(ParamError, match=message):
        coerce_params(declared_params(cls), submitted)

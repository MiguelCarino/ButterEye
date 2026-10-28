# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Details → Settings: the picture controls (upscaler, colour, LUT, debanding,
compare) and the battery/heat preset, saved into ``[general]`` and
``[[rules]]`` over FakeCore."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import api as core_api
from buttereye.core.api import PictureAdjust, Rule, RuleMatch, osapi
from buttereye.gui import settings_panel as sp
from tests.gui.helpers import fake_core


def _calls(bridge: Any) -> list[Any]:
    return [c for c in fake_core(bridge).fake_calls if c.name == "save_config"]


@pytest.fixture
def panel(make_bridge: Any, qtbot: Any) -> Any:
    bridge = make_bridge("all_ready")
    p = sp.SettingsPanel(bridge)
    qtbot.addWidget(p)
    qtbot.waitUntil(lambda: p.load is not None, timeout=5000)
    return p


def _saved_after(qtbot: Any, panel: Any, n: int) -> Any:
    qtbot.waitUntil(lambda: len(_calls(panel.bridge)) > n, timeout=5000)
    return _calls(panel.bridge)[-1].args[0]


# ---------------------------------------------------------------- pure helpers
def test_power_presets_put_their_rules_first_and_replace_each_other() -> None:
    mine = Rule(RuleMatch(width_max=1280), "fast")
    on = sp.with_power_rules((mine,), "battery")
    assert on == (Rule(RuleMatch(on_battery=True), "battery"), mine)
    assert sp.power_choice(on) == "battery"
    cool = sp.with_power_rules(on, "cool")
    assert cool[:2] == sp.POWER_RULES["cool"] and cool[2:] == (mine,)
    assert sp.power_choice(cool) == "cool"
    assert sp.with_power_rules(cool, "off") == (mine,)
    assert sp.power_choice((mine,)) == "off"
    # a preset rule the user moved down still counts
    assert sp.power_choice((mine, *sp.POWER_RULES["battery"])) == "battery"


# ---------------------------------------------------------------- picture
def test_every_new_control_has_a_name(panel: Any) -> None:
    widgets = [
        panel.upscaler,
        panel.deband,
        panel.grain,
        panel.compare,
        panel.even_pacing,
        panel.cadence_first,
        panel.power,
        panel.lut_import,
        panel.lut_clear,
        panel.picture_reset,
        *panel.picture_sliders.values(),
        *panel.picture_spins.values(),
    ]
    assert all(w.accessibleName() for w in widgets), [
        w.objectName() for w in widgets if not w.accessibleName()
    ]
    assert panel.picture_sliders["gamma"].accessibleName() == "Gamma, slider"
    assert panel.picture_spins["gamma"].accessibleName() == "Gamma"


def test_ravu_is_offered_and_saved(panel: Any, qtbot: Any) -> None:
    n = len(_calls(panel.bridge))
    panel.upscaler.setCurrentIndex(panel.upscaler.findData("ravu"))
    panel.upscaler.activated.emit(panel.upscaler.currentIndex())
    assert _saved_after(qtbot, panel, n).general.upscaling == "ravu"


def test_colour_sliders_save_once_they_are_still(panel: Any, qtbot: Any) -> None:
    n = len(_calls(panel.bridge))
    for v in range(1, 21):  # a drag: twenty steps
        panel.picture_sliders["brightness"].setValue(v)
    panel.picture_spins["saturation"].setValue(-30)
    assert panel.picture_spins["brightness"].value() == 20  # slider and box agree
    cfg = _saved_after(qtbot, panel, n)
    assert len(_calls(panel.bridge)) == n + 1  # one save, not twenty-one
    assert cfg.general.picture == PictureAdjust(brightness=20, saturation=-30)
    n = len(_calls(panel.bridge))
    panel.picture_reset.click()
    assert _saved_after(qtbot, panel, n).general.picture.neutral
    assert panel.picture_sliders["brightness"].value() == 0


def test_deband_strength_and_grain(panel: Any, qtbot: Any) -> None:
    assert not panel.grain.isEnabled()
    n = len(_calls(panel.bridge))
    panel.deband.setCurrentIndex(panel.deband.findData("strong"))
    panel.deband.activated.emit(panel.deband.currentIndex())
    g = _saved_after(qtbot, panel, n).general
    assert g.deband is True and g.deband_strength == "strong"
    assert panel.grain.isEnabled() and panel.grain.text() == "Automatic"
    n = len(_calls(panel.bridge))
    panel.grain.setValue(40)
    assert _saved_after(qtbot, panel, n).general.deband_grain == 40
    n = len(_calls(panel.bridge))
    panel.grain.setValue(panel.grain.minimum())  # back to Automatic
    assert _saved_after(qtbot, panel, n).general.deband_grain is None
    n = len(_calls(panel.bridge))
    panel.deband.setCurrentIndex(panel.deband.findData("off"))
    panel.deband.activated.emit(panel.deband.currentIndex())
    assert _saved_after(qtbot, panel, n).general.deband is False


def test_compare_split(panel: Any, qtbot: Any) -> None:
    n = len(_calls(panel.bridge))
    panel.compare.setChecked(True)
    assert _saved_after(qtbot, panel, n).general.compare == "split"


def test_even_pacing_and_cadence_first_are_saved(panel: Any, qtbot: Any) -> None:
    n = len(_calls(panel.bridge))
    panel.even_pacing.setChecked(True)
    assert _saved_after(qtbot, panel, n).general.even_pacing is True
    n = len(_calls(panel.bridge))
    panel.cadence_first.setChecked(True)
    g = _saved_after(qtbot, panel, n).general
    assert (g.cadence_first, g.even_pacing) == (True, True)


def test_a_lut_is_imported_into_butter_eyes_folder(
    panel: Any, qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got: list[tuple[Any, Path]] = []

    def fake_import(paths: Any, source: Path) -> str:
        got.append((paths, source))
        return "film.cube"

    monkeypatch.setattr(core_api, "import_lut", fake_import, raising=False)
    src = tmp_path / "Film Look.cube"
    n = len(_calls(panel.bridge))
    panel.import_lut(src)
    assert _saved_after(qtbot, panel, n).general.lut == "film.cube"
    assert got[0][1] == src and got[0][0] is not None  # the core's own Paths
    assert panel.lut_name.text() == "film.cube" and panel.lut_clear.isEnabled()
    n = len(_calls(panel.bridge))
    panel.lut_clear.click()
    assert _saved_after(qtbot, panel, n).general.lut is None


def test_a_file_that_is_not_a_lut_is_explained_and_nothing_saved(
    panel: Any, qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_paths: Any, _source: Path) -> str:
        raise ValueError("This is not a 3D LUT: it has no LUT_3D_SIZE line.")

    monkeypatch.setattr(core_api, "import_lut", refuse, raising=False)
    n = len(_calls(panel.bridge))
    panel.import_lut(tmp_path / "x.cube")
    qtbot.waitUntil(lambda: panel.lut_error.isVisibleTo(panel), timeout=5000)
    assert "LUT_3D_SIZE" in panel.lut_error.text()
    assert panel.lut_import.isEnabled()
    assert len(_calls(panel.bridge)) == n


# ---------------------------------------------------------------- power
def test_the_battery_preset_writes_rules(panel: Any, qtbot: Any) -> None:
    # the scenario's config predates the shipped Battery profile: no preset then
    assert not any(p.id == "battery" for p in panel.load.config.profiles)
    assert not panel.power.isEnabled()
    cfg = panel.load.config
    shipped = tuple(p for p in core_api.builtin_profiles() if p.id == "battery")
    assert shipped, "defaults.BATTERY_PROFILE_ID"
    load = dataclasses.replace(
        panel.load, config=dataclasses.replace(cfg, profiles=cfg.profiles + shipped)
    )
    panel._show(load)
    assert panel.power.isEnabled()
    n = len(_calls(panel.bridge))
    panel.power.setCurrentIndex(panel.power.findData("cool"))
    panel.power.activated.emit(panel.power.currentIndex())
    saved = _saved_after(qtbot, panel, n)
    assert saved.rules[:2] == sp.POWER_RULES["cool"]
    assert saved.rules[2:] == cfg.rules


def test_the_power_preset_is_shown_only_on_a_mac(
    make_bridge: Any, qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for mac in (True, False):
        monkeypatch.setattr(osapi, "IS_MACOS", mac)
        p = sp.SettingsPanel(make_bridge("all_ready"))
        qtbot.addWidget(p)
        assert p.power_box.isVisibleTo(p) is mac

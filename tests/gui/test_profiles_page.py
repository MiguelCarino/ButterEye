# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Profiles & Rules page (docs/design/GUI.md §4.4, §6 ``test_profiles_page``; U8).

Runs over FakeCore; ``validate_config``/``explain_rules`` are the real U2 rules
(pure, GUI thread). Asserts: built-ins read-only; delete blocked when a rule
uses the profile; inline validation and Save focusing the first invalid field;
Alt+Up/Alt+Down reorder; rule tester trace; used_defaults / read_only / unknown
keys / save-conflict states; unavailable panel; silent reload.
"""

from __future__ import annotations

import dataclasses
from fractions import Fraction
from types import MappingProxyType
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from buttereye.core.api import (
    BackendId,
    CapState,
    Config,
    ErrorCode,
    Feature,
    GeneralSettings,
    HdrClass,
    Profile,
    Reason,
    Rule,
    RuleMatch,
    Target,
    TargetKind,
)
from buttereye.core.testing import scenarios
from buttereye.gui.dialogs.rule_edit import RuleEditDialog, describe_match
from buttereye.gui.pages.profiles import ProfilesPage
from tests.gui.helpers import click_modal, fake_core

CUSTOM = Profile(
    id="anime",
    name="Anime",
    backend=BackendId.RIFE_NCNN,
    model="rife-v4.26_ensembleFalse",
    scale=None,
    target=Target(TargetKind.DISPLAY),
    sc_threshold=0.12,
    buffered_frames=None,
    concurrent_frames=None,
)
LOOSE = dataclasses.replace(CUSTOM, id="spare", name="Spare")


def custom_scenario(
    base: str = "all_ready", *, general: GeneralSettings | None = None
) -> scenarios.Scenario:
    sc = scenarios.get(base)
    cfg = sc.config_load.config
    cfg = dataclasses.replace(
        cfg,
        general=general or cfg.general,
        profiles=cfg.profiles + (CUSTOM, LOOSE),
        rules=cfg.rules + (Rule(RuleMatch(path_glob="*Anime*"), "anime"),),
    )
    return dataclasses.replace(
        sc, name=f"{base}+custom", config_load=dataclasses.replace(sc.config_load, config=cfg)
    )


def open_page(make_window: Any, qtbot: Any, scenario: Any = "all_ready") -> tuple[Any, Any]:
    win = make_window(scenario)
    win.go("profiles")
    page = win.page("profiles")
    assert isinstance(page, ProfilesPage)
    qtbot.waitUntil(lambda: page.config() is not None, timeout=3000)
    qtbot.wait(50)
    return win, page


def select_profile(page: ProfilesPage, pid: str) -> None:
    cfg = page.config()
    assert cfg is not None
    page.profile_list.setCurrentRow([p.id for p in cfg.profiles].index(pid))


def saves(win: Any) -> list[Any]:
    return [c for c in fake_core(win.bridge).fake_calls if c.name == "save_config"]


# --------------------------------------------------------------------- profiles
def test_builtins_are_read_only(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot)
    assert page.panel.state == "content"
    texts = [page.profile_list.item(i).text() for i in range(page.profile_list.count())]
    assert all("Built-in" in t for t in texts)
    select_profile(page, "quality")
    assert page.builtin_note.isVisible()
    for w in (page.name_edit, page.engine_combo, page.model_combo, page.sc_spin, page.hdr_skip):
        assert not w.isEnabled()
    assert not page.rename_button.isEnabled()
    assert not page.delete_button.isEnabled()
    assert page.duplicate_button.isEnabled()
    assert not page.reset_button.isEnabled()  # identical to the shipped profile
    # Duplicate gives an editable custom copy
    page.duplicate_button.click()
    cfg = page.config()
    assert cfg is not None
    copy = cfg.profiles[-1]
    assert not copy.builtin and copy.id == "quality-copy" and copy.name == "Quality (copy)"
    assert page.name_edit.isEnabled()
    assert page.has_unsaved_changes()


def test_reset_to_default_restores_shipped_builtin(make_window: Any, qtbot: Any) -> None:
    sc = scenarios.get("all_ready")
    cfg = sc.config_load.config
    changed = dataclasses.replace(cfg.profiles[0], sc_threshold=0.3)
    cfg = dataclasses.replace(cfg, profiles=(changed,) + cfg.profiles[1:])
    sc = dataclasses.replace(sc, config_load=dataclasses.replace(sc.config_load, config=cfg))
    _win, page = open_page(make_window, qtbot, sc)
    select_profile(page, "quality")
    assert page.reset_button.isEnabled()
    page.reset_button.click()
    new = page.config()
    assert new is not None and new.profiles[0].sc_threshold == pytest.approx(0.12)
    assert not page.reset_button.isEnabled()


def test_delete_blocked_when_referenced(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, custom_scenario())
    select_profile(page, "anime")
    assert page.name_edit.isEnabled()
    assert not page.delete_button.isEnabled()
    assert page.delete_reason.isVisible()
    assert page.delete_reason.text() == "Used by rule 4"
    assert page.delete_button.accessibleDescription() == "Used by rule 4"
    page.profile_list.setFocus()
    qtbot.keyClick(page.profile_list, Qt.Key.Key_Delete)
    cfg = page.config()
    assert cfg is not None and "anime" in {p.id for p in cfg.profiles}
    # an unreferenced custom profile can be deleted with Del
    select_profile(page, "spare")
    assert page.delete_button.isEnabled()
    page.profile_list.setFocus()
    qtbot.keyClick(page.profile_list, Qt.Key.Key_Delete)
    cfg = page.config()
    assert cfg is not None and "spare" not in {p.id for p in cfg.profiles}


def test_new_profile_with_insert_key(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot)
    n = page.profile_list.count()
    page.profile_list.setFocus()
    qtbot.keyClick(page.profile_list, Qt.Key.Key_Insert)
    assert page.profile_list.count() == n + 1
    cfg = page.config()
    assert cfg is not None
    p = cfg.profiles[-1]
    assert p.backend == "auto" and not p.builtin and p.id == "custom"
    assert QApplication.focusWidget() is page.name_edit
    assert "unsaved changes" in page.title()


def test_edits_use_frozen_replace_and_engine_rules(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, custom_scenario())
    select_profile(page, "anime")
    before = page.config()
    qtbot.keyClicks(page.name_edit, " HQ")
    after = page.config()
    assert before is not None and after is not None and after is not before
    assert before.profiles[-2].name == "Anime"  # the old value is untouched
    assert after.profiles[-2].name == "Anime HQ"
    # MVTools: model cleared, model combo disabled with a note
    i = page.engine_combo.findData(BackendId.MVTOOLS.value)
    page.engine_combo.setCurrentIndex(i)
    page.engine_combo.activated.emit(i)
    cfg = page.config()
    assert cfg is not None
    p = cfg.profiles[-2]
    assert p.backend is BackendId.MVTOOLS and p.model is None
    assert not page.model_combo.isEnabled()
    assert page.model_note.text() == "MVTools uses no model."
    assert page.concurrent_auto.text().startswith("Automatic (")
    # RIFE (Vulkan): scale note
    i = page.engine_combo.findData(BackendId.RIFE_NCNN.value)
    page.engine_combo.setCurrentIndex(i)
    page.engine_combo.activated.emit(i)
    assert page.scale_note.text() == (
        "At 4K, RIFE (Vulkan) works at a smaller size when the speed test says it can't keep up."
    )
    assert not page.scale_spin.isEnabled()
    assert page.concurrent_auto.text() == "Automatic (8)"  # §4.4: RIFE concurrent-frames 8


def test_trt_listed_only_after_opt_in(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, custom_scenario())
    select_profile(page, "anime")
    data = [page.engine_combo.itemData(i) for i in range(page.engine_combo.count())]
    assert BackendId.RIFE_TRT.value not in data


def test_trt_listed_after_opt_in(make_window: Any, qtbot: Any) -> None:
    sc = custom_scenario(general=GeneralSettings(trt_experimental=True))
    _win, page = open_page(make_window, qtbot, sc)
    select_profile(page, "anime")
    data = [page.engine_combo.itemData(i) for i in range(page.engine_combo.count())]
    assert BackendId.RIFE_TRT.value in data


def test_unavailable_engine_disabled_with_reason(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, custom_scenario("cpu_only"))
    qtbot.waitUntil(lambda: bool(page.engine_note.text()), timeout=3000)
    select_profile(page, "anime")
    combo = page.engine_combo
    i = combo.findData(BackendId.RIFE_NCNN.value)
    item = combo.model().item(i)
    assert not item.isEnabled()
    assert "llvmpipe only" in page.engine_note.text()
    assert "llvmpipe only" in combo.accessibleDescription()


def test_hdr_passthrough_disabled_until_spike(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, custom_scenario())
    select_profile(page, "anime")
    assert page.hdr_skip.isEnabled()
    assert not page.hdr_pass.isEnabled()
    assert page.hdr_note.text() == "Needs compatibility test M0(d)."


# --------------------------------------------------------------------- validation
def test_inline_validation_and_save_focuses_first_invalid(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, custom_scenario())
    select_profile(page, "anime")
    page.target_fixed.click()  # fixed rate with no rate yet -> invalid
    assert page.issue_badges["target"].isHidden()  # debounced
    qtbot.waitUntil(lambda: page.issue_badges["target"].isVisibleTo(page), timeout=1000)
    badge = page.issue_badges["target"]
    assert "fixed target needs a rate" in badge.text()
    assert badge.kind() == "blocking"
    row = page.profile_list.currentRow()
    assert "needs fixing" in page.profile_list.item(row).text()
    # Save refuses and moves focus to the first invalid field
    page.name_edit.setFocus()
    page.save_button.click()
    assert saves(win) == []
    assert QApplication.focusWidget() is page.target_display
    # fixing it clears the issue
    page.target_fps.setText("120000/1001")
    page.validate_now()
    assert page.issue_badges["target"].isHidden()
    assert page.issues() == ()


def test_invalid_issue_on_other_profile_selects_it(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, custom_scenario())
    select_profile(page, "anime")
    page.name_edit.clear()
    page.name_edit.textEdited.emit("")
    select_profile(page, "quality")
    page.validate_now()
    page.save()
    assert saves(win) == []
    cfg = page.config()
    assert cfg is not None
    assert cfg.profiles[page.profile_list.currentRow()].id == "anime"
    assert QApplication.focusWidget() is page.name_edit
    assert page.issue_badges["name"].text() == "The profile needs a name."


# --------------------------------------------------------------------- rules
def test_rules_table_and_alt_arrow_reorder(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot)
    page.tabs.setCurrentWidget(page.rules_tab)
    model = page.rules_model
    assert model.rowCount() == 3
    assert model.item(0, 1).text() == "fps ≤ 30 and height ≤ 1080"
    assert model.item(0, 2).text() == "Quality (quality)"
    view = page.rules_view
    view.setFocus()
    view.selectRow(0)
    qtbot.keyClick(view, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    cfg = page.config()
    assert cfg is not None
    assert [r.profile for r in cfg.rules] == ["balanced", "quality", "fast"]
    assert view.currentIndex().row() == 1
    qtbot.keyClick(view, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    cfg = page.config()
    assert cfg is not None and [r.profile for r in cfg.rules] == ["quality", "balanced", "fast"]
    assert not page.has_unsaved_changes()  # back to the loaded order
    # move down twice, then save with Ctrl+S
    qtbot.keyClick(view, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert page.up_button.isEnabled()
    qtbot.keyClick(view, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: not page.has_unsaved_changes(), timeout=3000)
    (call,) = saves(win)
    assert call.kwargs["expected_revision"] == "rev-1"
    saved: Config = call.args[0]
    assert [r.profile for r in saved.rules] == ["balanced", "quality", "fast"]


def test_rule_delete_and_edit_via_dialog(
    make_window: Any, qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _win, page = open_page(make_window, qtbot)
    page.tabs.setCurrentWidget(page.rules_tab)
    view = page.rules_view
    view.setFocus()
    view.selectRow(2)
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    cfg = page.config()
    assert cfg is not None and len(cfg.rules) == 2

    edited = Rule(RuleMatch(fps_min=Fraction(48)), "fast")

    def fake_dialog(self: Any, rule: Rule | None) -> Rule | None:
        assert rule is not None and rule.profile == "quality"
        return edited

    monkeypatch.setattr(ProfilesPage, "_open_rule_dialog", fake_dialog)
    view.selectRow(0)
    qtbot.keyClick(view, Qt.Key.Key_Return)
    cfg = page.config()
    assert cfg is not None and cfg.rules[0] == edited
    assert page.rules_model.item(0, 1).text() == "fps ≥ 48"


def test_rule_with_missing_profile_flagged(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, custom_scenario())
    select_profile(page, "anime")
    cfg = page.config()
    assert cfg is not None
    bad = dataclasses.replace(cfg, rules=cfg.rules + (Rule(RuleMatch(), "nope"),))
    page._set_cfg(bad, rules_changed=True)
    page.validate_now()
    assert any("Rule 5" in b.text() for b in page.rule_issues)
    assert "needs fixing" in page.rules_model.item(4, 0).text()
    assert page.rules_model.item(4, 2).text() == "nope (missing profile)"


# --------------------------------------------------------------------- tester
def test_rule_tester_trace(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot)
    page.tabs.setCurrentWidget(page.rules_tab)
    page.t_fps.setText("60")
    lines = page.trace_texts()
    assert lines[0] == "Rule 3 matched → Fast"
    assert lines[1] == "Rule 1 skipped: fps 60 > fps_max 30"
    assert lines[2] == "Rule 2 skipped: fps 60 > fps_max 30"
    assert lines[3].startswith("Rule 3: ")
    page.t_hdr.setCurrentIndex(page.t_hdr.findData(HdrClass.HDR10.value))
    lines = page.trace_texts()
    assert lines[0] == "No rule matched → Balanced (default)"
    assert lines[-1] == "Rule 3 skipped: HDR class hdr10 ≠ hdr_class sdr"
    hint = page.tester_hint.text()
    assert hint is not None and hint.startswith("buttereye profiles explain --fps 60")
    page.t_fps.setText("")
    assert page.trace_texts() == ["Enter a frame rate to test, such as 24000/1001."]
    # the tester follows unsaved edits
    page.t_fps.setText("24")
    assert page.trace_texts()[0] == "Rule 1 matched → Quality"
    view = page.rules_view
    view.setFocus()
    view.selectRow(0)
    page.delete_rule()
    assert page.trace_texts()[0] == "Rule 1 matched → Balanced"
    # [Use current session]: disabled without a session, fills the facts when there is one
    assert not page.use_session_button.isEnabled()
    win._on_facts(scenarios.facts("show.mkv", Fraction(30000, 1001), 2160, HdrClass.HDR10))
    page._update_session_button()
    assert page.use_session_button.isEnabled()
    page.use_session_button.click()
    assert page.t_fps.value() == Fraction(30000, 1001)
    assert page.t_height.value() == 2160
    assert page.t_path.text() == "/videos/show.mkv"
    assert page.t_hdr.currentData() == HdrClass.HDR10.value


# --------------------------------------------------------------------- states
def test_used_defaults_banner_read_only_and_start_fresh(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "config_invalid")
    banner = page.banners["invalid"]
    assert banner.title() == (
        "config.toml line 12, column 5: Expected '=' after a key. Defaults are in use."
    )
    assert banner.code == ErrorCode.CONFIG_INVALID.code
    names = [b.text() for b in banner.buttons]
    assert names == ["&Open folder", "Start &fresh…"]
    assert not page.new_button.isEnabled()
    assert not page.save_button.isEnabled()
    page.tabs.setCurrentWidget(page.rules_tab)
    assert not page.add_rule_button.isEnabled()
    click_modal(qtbot, "startFreshYes")
    banner.buttons[1].click()
    qtbot.waitUntil(lambda: "invalid" not in page.banners, timeout=2000)
    assert page.add_rule_button.isEnabled()
    assert page.save_button.isEnabled()  # saving replaces the file
    assert page.has_unsaved_changes()
    page.save()
    qtbot.waitUntil(lambda: not page.has_unsaved_changes(), timeout=3000)
    assert len(saves(win)) == 1


def test_start_fresh_cancel_keeps_read_only(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "config_invalid")
    click_modal(qtbot, "startFreshNo")
    page.start_fresh()
    assert "invalid" in page.banners
    assert not page.new_button.isEnabled()


def test_read_only_newer_schema(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "config_newer")
    banner = page.banners["read_only"]
    assert banner.title() == "Created by a newer ButterEye — read-only."
    assert banner.code == ErrorCode.CONFIG_NEWER_SCHEMA.code
    assert not page.save_button.isEnabled()
    assert not page.save_shortcut.isEnabled()
    assert not page.new_button.isEnabled()
    page.new_profile()
    page.save()
    assert saves(win) == []


def test_unknown_keys_info(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "config_conflict")
    banner = page.banners["unknown"]
    assert banner.title() == "3 unknown settings will be kept."
    assert banner.kind == "info"


def test_save_conflict_overwrite(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "config_conflict")
    page.new_profile()
    click_modal(qtbot, "overwrite")
    page.save()
    qtbot.waitUntil(lambda: len(saves(win)) == 2, timeout=3000)
    qtbot.waitUntil(lambda: not page.has_unsaved_changes(), timeout=3000)
    first, second = saves(win)
    assert first.kwargs["expected_revision"] == "rev-1"
    assert second.kwargs["expected_revision"] == "rev-disk"
    assert "custom" in {p.id for p in second.args[0].profiles}


def test_save_conflict_reload_theirs(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "config_conflict")
    core = fake_core(win.bridge)
    loads = core.fake_call_count("load_config")
    page.new_profile()
    click_modal(qtbot, "reloadTheirs")
    page.save()
    qtbot.waitUntil(lambda: core.fake_call_count("load_config") > loads, timeout=3000)
    qtbot.waitUntil(lambda: not page.has_unsaved_changes(), timeout=3000)
    cfg = page.config()
    assert cfg is not None and "custom" not in {p.id for p in cfg.profiles}
    assert len(saves(win)) == 1


def test_unavailable_shows_no_rows(make_window: Any, qtbot: Any) -> None:
    sc = scenarios.get("devbox_now")
    states = dict(sc.capabilities)
    states[Feature.CONFIG] = CapState(
        False,
        Reason.NOT_IMPLEMENTED,
        ErrorCode.NOT_IMPLEMENTED,
    )
    sc = dataclasses.replace(sc, name="no_config", capabilities=MappingProxyType(states))
    win = make_window(sc)
    win.go("profiles")
    page = win.page("profiles")
    qtbot.wait(150)
    assert page.panel.state == "unavailable"
    headline, _body, code = page.panel.unavailable_texts()
    assert headline == "Profiles can't be edited in this build yet."
    assert code == "BE-9001"
    assert page.profile_list.count() == 0
    assert page.rules_model.rowCount() == 0
    assert fake_core(win.bridge).fake_call_count("load_config") <= 1  # only the shell's


def test_silent_reload_after_external_save(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot)
    cfg = page.config()
    assert cfg is not None
    other = dataclasses.replace(cfg, rules=cfg.rules[:1])
    done: list[str] = []
    win.bridge.call(
        lambda core: core.save_config(other, expected_revision="rev-1"),
        owner=win,
        ok=done.append,
    )
    qtbot.waitUntil(lambda: bool(done), timeout=3000)
    qtbot.waitUntil(lambda: page.rules_model.rowCount() == 1, timeout=3000)
    assert not page.has_unsaved_changes()
    assert page.banners.get("external") is None


def test_external_change_while_editing_keeps_edits(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot)
    cfg = page.config()
    assert cfg is not None
    page.new_profile()
    other = dataclasses.replace(cfg, rules=cfg.rules[:1])
    done: list[str] = []
    win.bridge.call(
        lambda core: core.save_config(other, expected_revision="rev-1"),
        owner=win,
        ok=done.append,
    )
    qtbot.waitUntil(lambda: "external" in page.banners, timeout=3000)
    assert page.has_unsaved_changes()
    local = page.config()
    assert local is not None and "custom" in {p.id for p in local.profiles}


def test_ctrl_s_saves_and_title_marker(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot)
    assert page.title() == "Profiles & Rules"
    page.new_profile()
    assert page.title() == "Profiles & Rules — unsaved changes *"
    assert page.unsaved_label.isVisible()
    assert "unsaved changes" in win.windowTitle()
    qtbot.keyClick(page.name_edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: not page.has_unsaved_changes(), timeout=3000)
    assert len(saves(win)) == 1
    assert page.title() == "Profiles & Rules"
    assert "unsaved" not in win.windowTitle()
    # Revert brings back the last saved state
    page.new_profile()
    page.revert_button.click()
    assert not page.has_unsaved_changes()


# --------------------------------------------------------------------- dialog
def test_rule_edit_dialog_use_checkboxes(qtbot: Any) -> None:
    profiles = scenarios.builtin_profiles()
    dlg = RuleEditDialog(None, profiles)
    qtbot.addWidget(dlg)
    for key, box in dlg.use.items():
        assert not box.isChecked()
        assert not dlg.inputs[key].isEnabled()
    assert dlg.width_max.text() == "Not used"
    assert dlg.rule().match == RuleMatch()
    assert describe_match(dlg.rule().match) == "Always"
    dlg.use["fps_max"].setChecked(True)
    assert not dlg.ok_button.isEnabled()  # no rate typed yet
    assert dlg.problem.text() == "Enter a frame rate such as 30 or 24000/1001."
    dlg.fps_max.setText("24000/1001")
    assert dlg.ok_button.isEnabled()
    dlg.use["height_max"].setChecked(True)
    assert dlg.height_max.value() == 1080
    dlg.use["interlaced"].setChecked(True)
    dlg.interlaced.setCurrentIndex(1)
    dlg.profile.setCurrentIndex(dlg.profile.findData("fast"))
    rule = dlg.rule()
    assert rule == Rule(
        RuleMatch(fps_max=Fraction(24000, 1001), height_max=1080, interlaced=False), "fast"
    )
    assert describe_match(rule.match) == (
        "fps ≤ 24000/1001 (23.976) and height ≤ 1080 and not interlaced"
    )
    dlg.use["fps_min"].setChecked(True)
    dlg.fps_min.setText("60")
    assert not dlg.ok_button.isEnabled()
    assert "nothing can match" in dlg.problem.text()


def test_rule_edit_dialog_round_trips_existing_rule(qtbot: Any) -> None:
    rule = Rule(
        RuleMatch(
            fps_min=Fraction(48),
            width_max=1920,
            hdr_class=HdrClass.HLG,
            display_hz_min=144.0,
            path_glob="~/Videos/**",
        ),
        "cpu",
    )
    dlg = RuleEditDialog(rule, scenarios.builtin_profiles())
    qtbot.addWidget(dlg)
    assert dlg.rule() == rule
    assert dlg.ok_button.isEnabled()
    dlg.use["path_glob"].setChecked(False)
    assert dlg.rule().match.path_glob is None


def test_add_rule_through_dialog(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot)
    page.tabs.setCurrentWidget(page.rules_tab)
    page.rules_view.setFocus()
    page.rules_view.selectRow(0)

    def fill() -> None:
        dlg = QApplication.activeModalWidget()
        assert isinstance(dlg, RuleEditDialog)
        dlg.use["display_hz_min"].setChecked(True)
        dlg.display_hz_min.setValue(144.0)
        dlg.profile.setCurrentIndex(dlg.profile.findData("fast"))
        dlg.ok_button.click()

    def attempt(tries: int = 0) -> None:
        if isinstance(QApplication.activeModalWidget(), QDialog):
            fill()
        elif tries < 100:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(20, lambda: attempt(tries + 1))

    from PySide6.QtCore import QTimer

    QTimer.singleShot(0, attempt)
    qtbot.keyClick(page.rules_view, Qt.Key.Key_Insert)
    cfg = page.config()
    assert cfg is not None and len(cfg.rules) == 4
    assert cfg.rules[1] == Rule(RuleMatch(display_hz_min=144.0), "fast")
    assert page.rules_model.item(1, 1).text() == "display ≥ 144 Hz"


# --------------------------------------------------------------------- a11y (all tabs)
def test_a11y_rules_tab_expert_and_dialog(make_window: Any, qtbot: Any) -> None:
    from tests.gui.test_a11y_sweep import check_names_and_buddies, check_tab_cycle

    win, page = open_page(make_window, qtbot, custom_scenario())
    win.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is win, timeout=2000)
    select_profile(page, "anime")
    page.expert_button.setChecked(True)
    page.target_fixed.click()
    problems = check_names_and_buddies(page, win)
    problems += check_tab_cycle(win, win.sidebar, qtbot)
    page.tabs.setCurrentWidget(page.rules_tab)
    page.rules_view.selectRow(0)
    qtbot.wait(20)
    problems += check_names_and_buddies(page, win)
    problems += check_tab_cycle(win, win.sidebar, qtbot)
    assert not problems, "\n".join(problems)

    dlg = RuleEditDialog(None, scenarios.builtin_profiles(), win)
    qtbot.addWidget(dlg)
    for box in dlg.use.values():
        box.setChecked(True)
    dlg.show()
    qtbot.waitExposed(dlg)
    dlg.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is dlg, timeout=2000)
    problems = check_names_and_buddies(dlg, dlg)
    problems += check_tab_cycle(dlg, dlg.use["fps_min"], qtbot)
    dlg.reject()
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------- real core (U2)
def test_real_core_save_conflict_and_invalid_file(
    qtbot: Any, xdg_env: Any, tmp_path: Any, slow_callbacks: Any
) -> None:
    """F15 GUI side against the real U2 providers, in temporary XDG dirs only."""
    from PySide6.QtCore import QSettings

    from buttereye.core.api import DetachPolicy
    from buttereye.gui.bridge import CoreBridge
    from buttereye.gui.main_window import MainWindow

    bridge = CoreBridge(debug=True)
    with qtbot.waitSignal(bridge.ready, timeout=10000):
        bridge.start()
    try:
        settings = QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat)
        win = MainWindow(bridge, settings, open_setup_when_missing=False)
        win._may_close = True
        qtbot.addWidget(win)
        win.show()
        win.go("profiles")
        page = win.page("profiles")
        assert isinstance(page, ProfilesPage)
        qtbot.waitUntil(lambda: page.config() is not None, timeout=5000)
        config_file = xdg_env.config / "buttereye" / "config.toml"
        assert not config_file.exists()

        select_profile(page, "quality")
        page.duplicate_profile()
        page.name_edit.setText("Film night")
        page.name_edit.textEdited.emit("Film night")
        page.save()
        qtbot.waitUntil(lambda: not page.has_unsaved_changes(), timeout=5000)
        text = config_file.read_text(encoding="utf-8")
        assert 'id = "quality-copy"' in text and 'name = "Film night"' in text

        # changed on disk behind our back -> real ConfigConflict -> Overwrite
        config_file.write_text(text.replace("Film night", "Theirs"), encoding="utf-8")
        page.new_profile()
        click_modal(qtbot, "overwrite")
        page.save()
        qtbot.waitUntil(lambda: not page.has_unsaved_changes(), timeout=5000)
        text = config_file.read_text(encoding="utf-8")
        assert "Film night" in text and 'id = "custom"' in text

        # invalid TOML -> defaults, banner with the real line/column, read-only editors
        config_file.write_text("schema_version = 1\n[general\n", encoding="utf-8")
        page.refresh()
        qtbot.waitUntil(lambda: "invalid" in page.banners, timeout=5000)
        assert page.banners["invalid"].title().startswith("config.toml line 2, column ")
        assert not page.new_button.isEnabled()
        win.close()
    finally:
        bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=3.0)


def test_layout_gives_the_editor_the_width(make_window: Any, qtbot: Any) -> None:
    """At 800 px the list stays narrow, the Model combo shows its whole text, and the
    fixed-rate field exists only while "Fixed rate" is chosen."""
    win, page = open_page(make_window, qtbot, custom_scenario())
    win.resize(800, 600)
    select_profile(page, "anime")
    qtbot.wait(100)
    assert page.profile_list.width() < page.form_box.width()
    combo = page.model_combo
    assert combo.currentText()
    assert combo.width() >= combo.fontMetrics().horizontalAdvance(combo.currentText())
    assert combo.toolTip() == combo.currentText()
    select_profile(page, "spare")  # user profile: editable
    qtbot.wait(50)
    assert page.target_fixed.isEnabled()
    page.target_display.click()
    assert not page.target_fps.isVisibleTo(page)
    assert not page.target_fps_label.isVisibleTo(page)
    page.target_fixed.click()
    assert page.target_fps.isVisibleTo(page) and page.target_fps_label.isVisibleTo(page)
    page.target_2x.click()
    assert not page.target_fps.isVisibleTo(page)

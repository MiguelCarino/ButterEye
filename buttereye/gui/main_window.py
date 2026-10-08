# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The shell (docs/design/GUI.md §4.1): menus, sidebar, page stack, banners,
status bar, global keys, startup and close flows.

Global keys: Ctrl+1...Ctrl+7 (pages), Ctrl+O, Ctrl+Shift+A, Ctrl+T, Ctrl+D,
Ctrl+Shift+D, F5 (refresh the current page; on System that runs the checks),
Ctrl+Shift+C, F6 (sidebar -> page -> status bar), F1, Ctrl+Q. Ctrl+S belongs
to the Profiles page alone. Pages must not bind any of these keys themselves:
they implement ``Page.handle_action`` instead.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QSize, Qt
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEyeError,
    Capabilities,
    CapabilitiesChanged,
    ConfigLoad,
    DetachPolicy,
    ErrorCode,
    Event,
    EventsDropped,
    Feature,
    JobChanged,
    JobId,
    NotAvailable,
    OrphansFound,
    RenderJobState,
    SessionAdded,
    SessionChanged,
    SessionEnded,
    SessionId,
    SessionSnapshot,
    ShutdownReport,
    SourceFacts,
    internal_error,
    render,
    unavailable_text,
)
from buttereye.gui import a11y
from buttereye.gui.bridge import CoreBridge
from buttereye.gui.context import GuiContext
from buttereye.gui.pages import PAGE_REGISTRY, PageMissing, PageSpec, load_page_class
from buttereye.gui.pages.base import MissingPage, Page, PageAction
from buttereye.gui.quit_dialog import quit_needs, run_close_flow
from buttereye.gui.widgets.banner import Action, Banner
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.state_panel import split_headline
from buttereye.gui.widgets.status_badge import BadgeKind

_log = logging.getLogger(__name__)

APP_NAME = "ButterEye"
#: First-launch size (bounded to SCREEN_FRACTION of the available screen).
DEFAULT_SIZE = QSize(1100, 720)
#: Smallest useful window; bounded by the screen too (small or 2x-scaled).
MINIMUM_SIZE = QSize(640, 440)
SCREEN_FRACTION = 0.9
SETUP_WIZARD = "buttereye.gui.dialogs.setup_wizard:SetupWizard"

#: Every key the shell owns (tests assert no page binds one of them).
GLOBAL_KEYS: tuple[str, ...] = tuple(f"Ctrl+{i}" for i in range(1, 8)) + (
    "Ctrl+O",
    "Ctrl+Shift+A",
    "Ctrl+T",
    "Ctrl+D",
    "Ctrl+Shift+D",
    "F5",
    "Ctrl+Shift+C",
    "F6",
    "F1",
    "Ctrl+Q",
)


class _PageSlot:
    __slots__ = ("spec", "page", "scroll", "shown")

    def __init__(self, spec: PageSpec, page: Page, scroll: QScrollArea) -> None:
        self.spec = spec
        self.page = page
        self.scroll = scroll
        self.shown = False


class MainWindow(QMainWindow):
    """The ButterEye main window."""

    def __init__(
        self,
        bridge: CoreBridge,
        settings: QSettings,
        *,
        log_path: Path | None = None,
        registry: tuple[PageSpec, ...] = PAGE_REGISTRY,
        open_setup_when_missing: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("mainWindow")
        self.bridge = bridge
        self.settings = settings
        self.log_path = log_path
        self.shutdown_timeout_s = 5.0
        self.open_setup_when_missing = open_setup_when_missing
        self.last_shutdown: ShutdownReport | None = None
        self._may_close = False
        self._row = -1
        self._in_close_flow = False
        self._facts: SourceFacts | None = None
        self._sessions: dict[SessionId, SessionSnapshot] = {}
        self._jobs: dict[JobId, RenderJobState] = {}
        self._banners: dict[str, Banner] = {}
        self._setup_dialog: QDialog | None = None
        self._orphans: OrphansFound | None = None
        self.config_load: ConfigLoad | None = None

        self.ctx = GuiContext(
            bridge,
            settings,
            announce=self._announce,
            status=self.set_status,
            go=self.go,
            facts=lambda: self._facts,
        )

        self._build_body()
        self._slots: list[_PageSlot] = [self._make_slot(spec) for spec in registry]
        self.sidebar.blockSignals(True)
        for slot in self._slots:
            self.page_stack.addWidget(slot.scroll)
            item = QListWidgetItem(slot.spec.translated_title())
            item.setData(Qt.ItemDataRole.UserRole, slot.spec.page_id)
            self.sidebar.addItem(item)
        self.sidebar.setCurrentRow(-1)
        self.sidebar.blockSignals(False)
        self._fit_sidebar()
        self._build_actions()
        self._build_menus()

        self._remove_listener = a11y.add_announcement_listener(self._mirror_announcement)
        self.destroyed.connect(self._remove_listener_on_destroy)

        bridge.ready.connect(self._on_ready)
        bridge.fatal.connect(self._on_fatal)
        bridge.event.connect(self._on_event)
        bridge.unresponsive.connect(self._on_unresponsive)

        self._restore_size()
        if self._slots:
            self.sidebar.setCurrentRow(0)
        if bridge.is_ready and bridge.capabilities is not None:
            self._on_ready(bridge.capabilities)
        elif bridge.startup_error is not None:
            self._on_fatal(bridge.startup_error)

    # ------------------------------------------------------------------ building
    def _build_body(self) -> None:
        self.root = QStackedWidget(self)
        self.setCentralWidget(self.root)

        body = QWidget()
        outer = QVBoxLayout(body)
        self.banner_area = QWidget(body)
        self.banner_area.setObjectName("globalBanners")
        self._banner_layout = QVBoxLayout(self.banner_area)
        self._banner_layout.setContentsMargins(0, 0, 0, 0)
        self.banner_area.hide()
        outer.addWidget(self.banner_area)

        row = QHBoxLayout()
        self.sidebar = QListWidget(body)
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setAccessibleName(self.tr("Sections"))
        self.sidebar.currentRowChanged.connect(self._on_row_changed)
        row.addWidget(self.sidebar, 0)

        right = QVBoxLayout()
        self.play_help = QLabel(PLAY_HELP_TEXT(), body)
        self.play_help.setWordWrap(True)
        self.play_help.setTextFormat(Qt.TextFormat.PlainText)
        self.play_help.hide()
        right.addWidget(self.play_help)
        self.page_stack = QStackedWidget(body)
        self.page_stack.setObjectName("pageStack")
        right.addWidget(self.page_stack, 1)
        row.addLayout(right, 1)
        outer.addLayout(row, 1)
        self.root.addWidget(body)
        self._body = body

        self.fatal_panel = QWidget()
        self.fatal_panel.setObjectName("fatalPanel")
        self._fatal_layout = QVBoxLayout(self.fatal_panel)
        self.root.addWidget(self.fatal_panel)

        bar = QStatusBar(self)
        self.status_label = QLabel(bar)
        self.status_label.setObjectName("statusText")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        # An announcement mirror, not a control: out of the Tab chain (which would
        # otherwise put it between the sidebar and the page); F6 still reaches it.
        self.status_label.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.status_label.setAccessibleName(self.tr("Status"))
        bar.addWidget(self.status_label, 1)
        self.setStatusBar(bar)

    def _fit_sidebar(self) -> None:
        """Size the sidebar to its longest (translated) item instead of
        QListWidget's generic ~256 px hint; recomputed on font changes."""
        sb = self.sidebar
        if sb.count() == 0:
            return
        sb.ensurePolished()
        bar = sb.verticalScrollBar()
        scroll_w = bar.sizeHint().width() if bar is not None else 0
        margin = sb.style().pixelMetric(QStyle.PixelMetric.PM_LayoutHorizontalSpacing)
        width = sb.sizeHintForColumn(0) + 2 * sb.frameWidth() + scroll_w + 2 * max(margin, 0)
        sb.setMinimumWidth(width)
        sb.setMaximumWidth(width)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange):
            self._fit_sidebar()
        super().changeEvent(event)

    def _make_slot(self, spec: PageSpec) -> _PageSlot:
        page = self._instantiate(spec)
        scroll = QScrollArea()
        scroll.setObjectName(f"scroll.{spec.page_id}")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Not a Tab stop: its focus frame would sit outside the page stack and be
        # clipped away, so focus would seem to vanish. The page's own controls
        # take Tab focus, and the scroll area still scrolls them into view.
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidget(page)
        scroll.setAccessibleName(spec.translated_title())
        facts = getattr(page, "facts_changed", None)
        if facts is not None and hasattr(facts, "connect"):
            facts.connect(self._on_facts)
        page.title_changed.connect(self._update_title)
        return _PageSlot(spec, page, scroll)

    def _instantiate(self, spec: PageSpec) -> Page:
        title = spec.translated_title()
        try:
            cls = load_page_class(spec)
        except PageMissing:
            _log.info("page %s not in this build", spec.page_id)
            return MissingPage(self.ctx, spec.page_id, title)
        except Exception as exc:
            _log.exception("page %s failed to import", spec.page_id)
            return MissingPage(self.ctx, spec.page_id, title, internal_error(exc))
        try:
            page = cls(self.ctx)
        except Exception as exc:
            _log.exception("page %s failed to build", spec.page_id)
            return MissingPage(self.ctx, spec.page_id, title, internal_error(exc))
        if not page.windowTitle():
            page.setWindowTitle(title)
        return page

    def _action(
        self,
        text: str,
        slot: Callable[[], object],
        shortcut: str | None = None,
        *,
        name: str,
    ) -> QAction:
        act = QAction(text, self)
        act.setObjectName(name)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        act.triggered.connect(lambda _checked=False: slot())
        self.addAction(act)
        return act

    def _build_actions(self) -> None:
        a = self._action
        self.act_open = a(
            self.tr("&Open and Play…"),
            lambda: self.page_action("sessions", PageAction.OPEN_AND_PLAY, switch=True),
            "Ctrl+O",
            name="act.open",
        )
        self.act_attach = a(
            self.tr("&Attach to Running mpv…"),
            lambda: self.page_action("sessions", PageAction.ATTACH, switch=True),
            "Ctrl+Shift+A",
            name="act.attach",
        )
        self.act_setup = a(self.tr("Run &Setup…"), self.open_setup, name="act.setup")
        self.act_quit = a(self.tr("&Quit"), self.close, "Ctrl+Q", name="act.quit")
        self.act_toggle = a(
            self.tr("&Interpolation On/Off"),
            lambda: self.page_action("sessions", PageAction.TOGGLE_INTERPOLATION),
            "Ctrl+T",
            name="act.toggle",
        )
        self.act_detach = a(
            self.tr("&Detach"),
            lambda: self.page_action("sessions", PageAction.DETACH),
            "Ctrl+D",
            name="act.detach",
        )
        self.act_disable_detach = a(
            self.tr("Disable &and Detach"),
            lambda: self.page_action("sessions", PageAction.DISABLE_AND_DETACH),
            "Ctrl+Shift+D",
            name="act.disableDetach",
        )
        self.act_checks = a(self.tr("Run &Checks"), self.run_checks, name="act.checks")
        self.act_refresh = a(
            self.tr("&Refresh This Page"), self.refresh_current, "F5", name="act.refresh"
        )
        self.act_report = a(
            self.tr("Create &Bug Report…"),
            lambda: self.page_action("system", PageAction.CREATE_REPORT, switch=True),
            name="act.report",
        )
        self.act_copy_cmd = a(
            self.tr("Copy &Equivalent Command"),
            self.copy_equivalent_command,
            "Ctrl+Shift+C",
            name="act.copyCommand",
        )
        self.act_licences = a(
            self.tr("&Licences"),
            lambda: self.page_action("about", PageAction.SHOW_LICENCES, switch=True),
            name="act.licences",
        )
        self.act_about = a(
            self.tr("&About ButterEye"),
            lambda: self.page_action("about", PageAction.SHOW_ABOUT, switch=True),
            "F1",
            name="act.about",
        )
        self.act_cycle = a(
            self.tr("Move &Focus to Next Area"), self.cycle_focus, "F6", name="act.cycleFocus"
        )
        self.page_actions: list[QAction] = []
        for i, slot in enumerate(self._slots[:9]):
            pid = slot.spec.page_id
            act = a(
                self.tr("&{n} {page}").format(
                    n=i + 1, page=slot.spec.translated_title().replace("&", "&&")
                ),
                lambda pid=pid: self.go(pid),  # type: ignore[misc]
                f"Ctrl+{i + 1}",
                name=f"act.page.{pid}",
            )
            self.page_actions.append(act)
        self._live_actions = (self.act_toggle, self.act_detach, self.act_disable_detach)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        file_menu = QMenu(self.tr("&File"), self)
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_attach)
        file_menu.addAction(self.act_setup)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)
        playback = QMenu(self.tr("&Playback"), self)
        playback.addAction(self.act_toggle)
        playback.addAction(self.act_detach)
        playback.addAction(self.act_disable_detach)
        tools = QMenu(self.tr("&Tools"), self)
        tools.addAction(self.act_checks)
        tools.addAction(self.act_refresh)
        tools.addAction(self.act_report)
        tools.addAction(self.act_copy_cmd)
        go_menu = QMenu(self.tr("&Go"), self)
        for act in self.page_actions:
            go_menu.addAction(act)
        go_menu.addSeparator()
        go_menu.addAction(self.act_cycle)
        help_menu = QMenu(self.tr("&Help"), self)
        help_menu.addAction(self.act_licences)
        help_menu.addAction(self.act_about)
        for m in (file_menu, playback, tools, go_menu, help_menu):
            mb.addMenu(m)
        self.menus = (file_menu, playback, tools, go_menu, help_menu)

    # ------------------------------------------------------------------ pages
    @property
    def pages(self) -> tuple[Page, ...]:
        return tuple(s.page for s in self._slots)

    def page(self, page_id: str) -> Page | None:
        for s in self._slots:
            if s.spec.page_id == page_id:
                return s.page
        return None

    def current_page(self) -> Page | None:
        i = self._row
        return self._slots[i].page if 0 <= i < len(self._slots) else None

    def current_page_id(self) -> str | None:
        i = self._row
        return self._slots[i].spec.page_id if 0 <= i < len(self._slots) else None

    def go(self, page_id: str) -> None:
        for i, s in enumerate(self._slots):
            if s.spec.page_id == page_id:
                self.sidebar.setCurrentRow(i)
                return
        _log.warning("go(): unknown page %r", page_id)

    def _on_row_changed(self, row: int) -> None:
        current = self._row
        if row == current or not 0 <= row < len(self._slots):
            return
        old = self._slots[current].page if 0 <= current < len(self._slots) else None
        if old is not None and self._has_unsaved(old) and not self._confirm_discard(old):
            self.sidebar.blockSignals(True)
            self.sidebar.setCurrentRow(current)
            self.sidebar.blockSignals(False)
            return
        self._row = row
        self.page_stack.setCurrentIndex(row)
        slot = self._slots[row]
        self.play_help.setVisible(slot.spec.page_id == "sessions")
        self._update_title()
        if not slot.shown:
            slot.shown = True
            self._refresh(slot.page)

    @staticmethod
    def _has_unsaved(page: Page) -> bool:
        try:
            return bool(page.has_unsaved_changes())
        except Exception:
            _log.exception("has_unsaved_changes failed")
            return False

    def _confirm_discard(self, page: Page) -> bool:
        box = a11y.message_box(
            self,
            icon=QMessageBox.Icon.Question,
            title=self.tr("Unsaved changes"),
            text=self.tr("Discard unsaved changes to {page}?").format(page=self._plain_title(page)),
        )
        box.setObjectName("discardChanges")
        discard = box.addButton(self.tr("&Discard"), QMessageBox.ButtonRole.DestructiveRole)
        discard.setObjectName("discard")
        stay = box.addButton(self.tr("&Stay"), QMessageBox.ButtonRole.RejectRole)
        stay.setObjectName("stay")
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return box.clickedButton() is discard

    def _plain_title(self, page: Page) -> str:
        """The page's sidebar name, without the "unsaved changes *" decoration
        ``Page.title()`` carries while dirty (that one stays in the window title)."""
        for s in self._slots:
            if s.page is page:
                return s.spec.translated_title()
        return page.windowTitle() or page.title()

    def _update_title(self) -> None:
        page = self.current_page()
        title = page.title() if page is not None else ""
        self.setWindowTitle(f"{title} — {APP_NAME}" if title else APP_NAME)

    def _refresh(self, page: Page) -> None:
        try:
            page.refresh()
        except Exception:
            _log.exception("refresh of %s failed", type(page).__name__)

    def refresh_current(self) -> None:
        page = self.current_page()
        if page is not None:
            self._refresh(page)

    def run_checks(self) -> None:
        self.go("system")
        page = self.page("system")
        if page is None:
            return
        if not page.handle_action(PageAction.RUN_CHECKS):
            self._refresh(page)

    def page_action(self, page_id: str, action: PageAction, *, switch: bool = False) -> bool:
        """Dispatch a shell command to a page (``Page.handle_action``)."""
        page = self.page(page_id)
        if page is None:
            return False
        if switch:
            self.go(page_id)
            if self.current_page() is not page:
                return False  # the user chose to stay on a page with unsaved edits
        try:
            handled = page.handle_action(action)
        except Exception:
            _log.exception("%s.handle_action(%s) failed", type(page).__name__, action)
            handled = False
        if not handled and action not in (PageAction.SHOW_ABOUT,):
            self._announce(self.tr("Not available on this page in this build."), False)
        return handled

    def copy_equivalent_command(self) -> None:
        page = self.current_page()
        text: str | None = None
        if page is not None:
            hint = page.cli_hint()
            if hint is not None:
                text = hint.text()
            else:
                for w in page.findChildren(CliHint):
                    if w.isVisibleTo(page) and w.hint() is not None:
                        text = w.text()
                        break
        if not text:
            self._announce(self.tr("This page has no equivalent command."), False)
            return
        QGuiApplication.clipboard().setText(text)
        self._announce(self.tr("Copied: {command}").format(command=text), False)

    def cycle_focus(self) -> None:
        """F6: sidebar -> current page -> status bar -> sidebar."""
        fw = QGuiApplication.focusObject()
        slot = self._slots[self._row] if 0 <= self._row < len(self._slots) else None
        in_sidebar = fw is self.sidebar
        in_page = (
            slot is not None
            and isinstance(fw, QWidget)
            and (fw is slot.scroll or slot.scroll.isAncestorOf(fw))
        )
        if in_sidebar and slot is not None:
            target = _first_focusable(slot.page)
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)
            else:  # nothing to focus on this page: next area
                self.status_label.setFocus(Qt.FocusReason.OtherFocusReason)
        elif in_page:
            self.status_label.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.sidebar.setFocus(Qt.FocusReason.OtherFocusReason)

    # ------------------------------------------------------------------ status
    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setAccessibleDescription(text)

    def _announce(self, text: str, assertive: bool) -> None:
        a11y.announce(self.status_label, text, assertive=assertive)

    def _mirror_announcement(self, _w: QWidget, text: str, _assertive: bool) -> None:
        if shiboken6.isValid(self) and shiboken6.isValid(self.status_label):
            self.set_status(text)

    def _remove_listener_on_destroy(self, _obj: object = None) -> None:
        self._remove_listener()

    # ------------------------------------------------------------------ banners
    def show_banner(
        self,
        key: str,
        kind: BadgeKind,
        title: str,
        body: str = "",
        code: str | None = None,
        actions: tuple[Action, ...] = (),
    ) -> Banner:
        self.hide_banner(key)
        banner = Banner(kind, title, body, code, (), actions, self.banner_area)
        banner.setObjectName(f"banner.{key}")
        self._banners[key] = banner
        self._banner_layout.addWidget(banner)
        self.banner_area.show()
        return banner

    def hide_banner(self, key: str) -> None:
        old = self._banners.pop(key, None)
        if old is not None:
            self._banner_layout.removeWidget(old)
            old.hide()
            old.deleteLater()
        if not self._banners:
            self.banner_area.hide()

    def banner(self, key: str) -> Banner | None:
        return self._banners.get(key)

    # ------------------------------------------------------------------ startup
    def _on_ready(self, caps: Capabilities) -> None:
        self._apply_caps(caps)
        self._seed()
        if caps.states.get(Feature.CONFIG) is not None and caps.ok(Feature.CONFIG):
            self.bridge.call(
                lambda core: core.load_config(), owner=self, ok=self._on_config, err=self._quiet
            )
        if caps.states.get(Feature.DISCOVER) is not None and caps.ok(Feature.DISCOVER):
            self.bridge.call(lambda core: core.discover(), owner=self, ok=_ignore, err=self._quiet)

    def _seed(self) -> None:
        self.bridge.call(
            lambda core: core.sessions(), owner=self, ok=self._seed_sessions, err=self._quiet
        )
        self.bridge.call(
            lambda core: core.render_jobs(), owner=self, ok=self._seed_jobs, err=self._quiet
        )

    def _seed_sessions(self, snaps: tuple[SessionSnapshot, ...]) -> None:
        self._sessions = {s.sid: s for s in snaps}

    def _seed_jobs(self, jobs: tuple[RenderJobState, ...]) -> None:
        self._jobs = {j.id: j for j in jobs}

    def _quiet(self, err: ButterEyeError) -> None:
        if not isinstance(err, NotAvailable):
            _log.warning("startup call failed: %s", err)

    def _on_config(self, load: ConfigLoad) -> None:
        self.config_load = load
        if not load.exists and self.open_setup_when_missing:
            self.open_setup()

    def _apply_caps(self, caps: Capabilities) -> None:
        st = caps.states.get(Feature.LIVE)
        live_ok = st is not None and st.available
        tip = ""
        if st is not None and not st.available:
            tip = split_headline(render(unavailable_text(Feature.LIVE, st)))[0]
        for act in self._live_actions:
            act.setEnabled(live_ok)
            act.setStatusTip(tip)
            act.setToolTip(tip)
        # File > Attach is greyed out, with the reason, while attaching isn't available
        att = caps.states.get(Feature.ATTACH)
        why = ""
        if att is not None and not att.available:
            why = split_headline(render(unavailable_text(Feature.ATTACH, att)))[0]
        self.act_attach.setEnabled(not why)
        self.act_attach.setStatusTip(why)
        self.act_attach.setToolTip(why)

    def _on_fatal(self, err: ButterEyeError) -> None:
        _clear_layout(self._fatal_layout)
        heading = a11y.heading(QLabel(self.tr("ButterEye couldn't start")))
        heading.setTextFormat(Qt.TextFormat.PlainText)
        self._fatal_layout.addWidget(heading)
        banner = Banner.from_error(err, (), self.fatal_panel)
        banner.setObjectName("fatalBanner")
        self._fatal_layout.addWidget(banner)
        if self.log_path is not None:
            log = a11y.selectable(
                QLabel(self.tr("Log: {path}").format(path=str(self.log_path))),
                name=self.tr("Log file"),
            )
            log.setObjectName("fatalLog")
            log.setTextFormat(Qt.TextFormat.PlainText)
            self._fatal_layout.addWidget(log)
        quit_button = QPushButton(self.tr("&Quit"))
        quit_button.setObjectName("fatalQuit")
        quit_button.clicked.connect(self.close)
        self._fatal_layout.addWidget(quit_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._fatal_layout.addStretch(1)
        self.root.setCurrentWidget(self.fatal_panel)
        quit_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._announce(
            self.tr("ButterEye couldn't start: {cause}").format(cause=render(err.cause)), True
        )

    def _on_unresponsive(self, stuck: bool) -> None:
        if stuck:
            log = str(self.log_path) if self.log_path is not None else self.tr("not available")
            self.show_banner(
                "unresponsive",
                "blocking",
                self.tr("ButterEye's engine is not responding. Log: {path}").format(path=log),
            )
            self._announce(self.tr("ButterEye's engine is not responding."), True)
        else:
            if "unresponsive" in self._banners:
                self._announce(self.tr("ButterEye's engine is responding again."), False)
            self.hide_banner("unresponsive")

    # ------------------------------------------------------------------ events
    def _on_event(self, ev: Event) -> None:
        if isinstance(ev, SessionAdded | SessionChanged):
            self._sessions[ev.snapshot.sid] = ev.snapshot
        elif isinstance(ev, SessionEnded):
            self._sessions.pop(ev.sid, None)
        elif isinstance(ev, JobChanged):
            self._jobs[ev.job.id] = ev.job
        elif isinstance(ev, CapabilitiesChanged):
            self._apply_caps(ev.caps)
            if "orphans" in self._banners:
                self._on_orphans(self._orphans)
        elif isinstance(ev, OrphansFound):
            self._on_orphans(ev)
        elif isinstance(ev, EventsDropped):
            self._seed()
        for slot in self._slots:
            try:
                slot.page.on_event(ev)
            except Exception:
                _log.exception("%s.on_event failed", type(slot.page).__name__)

    def _feature_ok(self, feature: Feature) -> bool:
        caps = self.bridge.capabilities
        return caps is not None and feature in caps.states and caps.ok(feature)

    def _on_orphans(self, ev: OrphansFound | None) -> None:
        """Banner for leftover ButterEye players; the wording follows what this
        build can do with them (ATTACH available or not), so it updates itself
        when attaching lands (CapabilitiesChanged re-runs this)."""
        self._orphans = ev
        found = [c for c in ev.candidates if c.refused is None] if ev is not None else []
        if not found:
            self.hide_banner("orphans")
            return
        title = self.tr("ButterEye found %n player(s) it started earlier.", None, len(found))
        dismiss: Action = (self.tr("&Dismiss"), lambda: self.hide_banner("orphans"))
        if self._feature_ok(Feature.ATTACH):
            body = self.tr("They keep playing. Attach to them from the Play page.")
            actions: tuple[Action, ...] = (
                (self.tr("Open &Play page"), lambda: self.go("sessions")),
                dismiss,
            )
        else:
            body = self.tr(
                "They keep playing. This build can't reconnect to them; "
                "press q in their mpv window to close them."
            )
            actions = (dismiss,)
        self.show_banner("orphans", "info", title, body, actions=actions)

    def _on_facts(self, facts: object) -> None:
        self._facts = facts if isinstance(facts, SourceFacts) else None

    # ------------------------------------------------------------------ setup
    def open_setup(self) -> None:
        """File > Run Setup... (U6's wizard, loaded lazily)."""
        if self._setup_dialog is not None and shiboken6.isValid(self._setup_dialog):
            self._setup_dialog.raise_()
            self._setup_dialog.activateWindow()
            return
        cls = _load_class(SETUP_WIZARD)
        if cls is None:
            self.show_banner(
                "setup",
                "info",
                self.tr("Setup isn't in this build yet."),
                self.tr("Check your system on the System page; settings can't be created here."),
                ErrorCode.NOT_IMPLEMENTED.code,
                actions=((self.tr("&Dismiss"), lambda: self.hide_banner("setup")),),
            )
            return
        try:
            dlg = cls(self.ctx, self)
        except Exception as exc:
            _log.exception("setup wizard failed to open")
            err = internal_error(exc)
            self.show_banner("setup", "blocking", render(err.cause), "", err.code.code)
            return
        if not isinstance(dlg, QDialog):
            _log.error("%s is not a QDialog", SETUP_WIZARD)
            return
        self._setup_dialog = dlg
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.finished.connect(self._on_setup_finished)
        dlg.open()

    def _on_setup_finished(self, _result: int) -> None:
        self._setup_dialog = None
        self.bridge.call(
            lambda core: core.load_config(),
            owner=self,
            ok=self._on_config_after_setup,
            err=self._quiet,
        )

    def _on_config_after_setup(self, load: ConfigLoad) -> None:
        self.config_load = load

    # ------------------------------------------------------------------ closing
    @property
    def live_sessions(self) -> tuple[SessionSnapshot, ...]:
        return tuple(s for s in self._sessions.values() if not s.ended)

    @property
    def jobs(self) -> tuple[RenderJobState, ...]:
        return tuple(self._jobs.values())

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._may_close:
            self._save_size()
            event.accept()
            return
        if self._in_close_flow:
            event.ignore()
            return
        self._in_close_flow = True
        try:
            allowed = self._close_flow()
        finally:
            self._in_close_flow = False
        if allowed:
            self._may_close = True
            self._save_size()
            event.accept()
        else:
            event.ignore()

    def _close_flow(self) -> bool:
        for slot in self._slots:
            if self._has_unsaved(slot.page):
                self.go(slot.spec.page_id)
                if not self._confirm_discard(slot.page):
                    return False
        if not self.bridge.is_ready:
            self.last_shutdown = self.bridge.shutdown(
                DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=self.shutdown_timeout_s
            )
            return True
        self.bridge.drain_now()  # latest session/job state before deciding
        needs = quit_needs(self._sessions.values(), self._jobs.values())

        live = tuple(s.sid for s in needs.live)

        def shutdown(policy: DetachPolicy, cancel_jobs: bool) -> ShutdownReport | None:
            # timeout_s is the core's per-stage budget; the bridge waits for the
            # core's whole close (close_wait_s) and, if even that runs out,
            # reports the sessions it saw detach instead of "all failed".
            return self.bridge.shutdown(
                policy, cancel_jobs=cancel_jobs, timeout_s=self.shutdown_timeout_s, live=live
            )

        ok, report = run_close_flow(self, needs, shutdown)
        if ok:
            self.last_shutdown = report
        return ok

    def _available_size(self) -> QSize:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return DEFAULT_SIZE
        return screen.availableGeometry().size()

    def _restore_size(self) -> None:
        """Saved size (clamped to this screen), else a real default.

        Every page sits in a QScrollArea, so ``sizeHint()`` is tiny (about
        280x254: sidebar plus one word per line); never fall back to it.
        """
        avail = self._available_size()
        fit = QSize(int(avail.width() * SCREEN_FRACTION), int(avail.height() * SCREEN_FRACTION))
        self.setMinimumSize(MINIMUM_SIZE.boundedTo(fit))
        size = self.settings.value("window/size")
        if isinstance(size, QSize) and size.isValid():
            target = size.boundedTo(avail)  # saved on a bigger monitor
        else:
            target = DEFAULT_SIZE.boundedTo(fit)
        self.resize(target.expandedTo(self.minimumSize()))

    def _save_size(self) -> None:
        self.settings.setValue("window/size", self.size())  # size only, never position
        self.settings.sync()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def PLAY_HELP_TEXT() -> str:  # noqa: N802 - translated at call time
    return QCoreApplication.translate(
        "MainWindow",
        "Keyboard shortcuts for playback work inside the mpv window (ButterEye helper), "
        "not system-wide.",
    )


def _ignore(_value: object) -> None:
    return None


def _first_focusable(root: QWidget) -> QWidget | None:
    w = root.nextInFocusChain()
    seen = 0
    while w is not None and w is not root and seen < 10000:
        seen += 1
        if (
            root.isAncestorOf(w)
            and w.isVisibleTo(root)
            and w.isEnabled()
            and w.focusPolicy() & Qt.FocusPolicy.TabFocus
        ):
            return w
        w = w.nextInFocusChain()
    return None


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget() if item is not None else None
        if w is not None:
            w.hide()
            w.deleteLater()


def _load_class(target: str) -> type[Any] | None:
    module, _, attr = target.partition(":")
    try:
        if importlib.util.find_spec(module) is None:
            return None
    except ImportError, ValueError:
        return None
    try:
        mod = importlib.import_module(module)
    except Exception:
        _log.exception("%s failed to import", module)
        return None
    cls = getattr(mod, attr, None)
    return cls if isinstance(cls, type) else None


__all__ = ["APP_NAME", "GLOBAL_KEYS", "MainWindow"]

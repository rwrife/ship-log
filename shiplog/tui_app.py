"""The Textual application for ``shiplog tui`` (imported lazily).

Kept in its own module so :mod:`shiplog.tui` can import it *inside* a function and
fall back to a friendly hint when Textual isn't installed. Everything stateful and
testable already lives in :mod:`shiplog.tui` (the pure selection layer); this file
is just the view wiring on top of it.

Layout::

    ┌ search ───────────────────────────────────────────────┐
    │ [ /-to-search input ........................... ]      │
    ├ list (left) ──────────────┬ detail (right) ────────────┤
    │  id   when   type  summary │  full rationale for the    │
    │  ...                       │  selected entry            │
    ├ status ───────────────────┴────────────────────────────┤
    │ 12/40 entries · type: all · search: foo                 │
    └ footer (key hints) ─────────────────────────────────────┘

Keyboard-first: ``/`` focus search, ``t``/``T`` cycle the type filter,
``Esc`` clear-or-blur, ``q`` quit, arrows + Enter move the selection.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Static

from .models import Entry, EntryType
from .render import _short_ts  # reuse the exact ls timestamp trimming
from .tui import FilterState, cycle_type, detail_lines, select_entries, status_summary

# One accent color per type, matching render.py's table hues (dead-ends shout).
_TYPE_COLOR: dict[str, str] = {
    EntryType.DECISION.value: "bold green",
    EntryType.ATTEMPT.value: "yellow",
    EntryType.DEADEND.value: "bold red",
    EntryType.NOTE.value: "cyan",
}


class ShipLogApp(App):
    """Full-screen, filterable browser over a repo's ship-log entries."""

    CSS = """
    Screen { layout: vertical; }
    #search { dock: top; height: 3; border: round $accent; }
    #body { height: 1fr; }
    #list { width: 55%; border-right: solid $panel; }
    #detail { width: 45%; padding: 0 1; }
    #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("slash", "focus_search", "Search", show=True, key_display="/"),
        Binding("t", "cycle_type", "Type+", show=True),
        Binding("T", "cycle_type_back", "Type-", show=False),
        Binding("escape", "clear_or_blur", "Clear", show=True),
    ]

    def __init__(self, entries: list[Entry], *, repo_label: str = "") -> None:
        super().__init__()
        self._entries = entries
        self._repo_label = repo_label
        self._state = FilterState()
        self._visible: list[Entry] = []

    # -- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        title = "ship-log ⚓"
        if self._repo_label:
            title += f"  ·  {self._repo_label}"
        self.title = title
        yield Header(show_clock=False)
        yield Input(placeholder="search summary / why / tags / files …", id="search")
        with Horizontal(id="body"):
            table: DataTable = DataTable(id="list", cursor_type="row", zebra_stripes=True)
            yield table
            yield Static("", id="detail")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#list", DataTable)
        table.add_columns("id", "when", "type", "summary")
        self._refresh()
        table.focus()

    # -- data flow --------------------------------------------------------

    def _refresh(self) -> None:
        """Recompute the visible set from current state and repaint table + status."""
        self._visible = select_entries(self._entries, self._state)
        table = self.query_one("#list", DataTable)
        # Preserve the cursor row index across refilters where sensible.
        prev = table.cursor_row if table.row_count else 0
        table.clear()
        for e in self._visible:
            table.add_row(
                Text(e.id, style="dim"),
                Text(_short_ts(e.ts), style="dim"),
                Text(e.type.value, style=_TYPE_COLOR.get(e.type.value, "white")),
                e.summary,
                key=e.id,
            )
        if self._visible:
            table.move_cursor(row=min(prev, len(self._visible) - 1))
            self._show_detail(table.cursor_row)
        else:
            self.query_one("#detail", Static).update(
                Text("no entries match — clear filters (Esc) or broaden your search.",
                     style="dim italic")
            )
        self._update_status()

    def _update_status(self) -> None:
        self.query_one("#status", Static).update(
            status_summary(self._state, shown=len(self._visible), total=len(self._entries))
        )

    def _show_detail(self, row_index: int) -> None:
        """Render the detail pane for the entry at ``row_index`` of the visible set."""
        if not self._visible or row_index < 0 or row_index >= len(self._visible):
            return
        entry = self._visible[row_index]
        color = _TYPE_COLOR.get(entry.type.value, "white")
        body = Text()
        body.append(f"⚓ {entry.id}\n", style=f"{color}")
        for line in detail_lines(entry):
            label, _, value = line.partition(":")
            body.append(label + ":", style="bold")
            body.append(value + "\n")
        self.query_one("#detail", Static).update(body)

    # -- events -----------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._state.query = event.value
            self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the search box hands focus back to the list for navigation.
        if event.input.id == "search":
            self.query_one("#list", DataTable).focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row is not None:
            self._show_detail(event.cursor_row)

    # -- actions (key bindings) ------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_cycle_type(self) -> None:
        self._state.type_ = cycle_type(self._state.type_)
        self._refresh()

    def action_cycle_type_back(self) -> None:
        self._state.type_ = cycle_type(self._state.type_, reverse=True)
        self._refresh()

    def action_clear_or_blur(self) -> None:
        """Esc: clear the search if focused/non-empty, else return focus to the list.

        A single, intuitive key that first undoes a search, then steps back to the
        table — never an accidental quit (``q`` is the only quit).
        """
        search = self.query_one("#search", Input)
        if self.focused is search or search.value:
            search.value = ""
            self._state.query = ""
            self._refresh()
            self.query_one("#list", DataTable).focus()
        else:
            self.query_one("#list", DataTable).focus()

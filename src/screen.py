"""
The screens drawn on the graphical display, and the keys that drive them.

The control menu was written for a terminal: print a numbered list, read a
whole typed line back. That works on a 24-line console and not at all on a
panel six rows tall, so this module gives the same menu a second face — one
highlighted row at a time, moved with the keypad and confirmed with a key,
with the list scrolling underneath the selection.

Four keys do everything, and the strip along the bottom of every screen says
which four, because an operator standing at a boiler has no manual and the caps
say ``2`` and ``8``, not "up" and "down":

  ``2``  move up          ``#``  select / accept
  ``8``  move down        ``*``  back

The layout is fixed at 128x64. Rows are 13 px rather than 8: Persian needs
that much to be readable, because a Naskh letter reaches about eight rows above
the baseline and the ج and ژ hang three rows below it, so the 5x7 cell it
replaces could only ever have drawn a smudge. The cost is honest and visible —
three body rows instead of six — and it buys text an operator can read in a
boiler room rather than text that merely occupies the panel. Anything longer
than three rows scrolls, and a bar down the right-hand edge shows how far
through it you are; on a screen this small the difference between "the list ends
here" and "there are two more" is otherwise invisible.

Rows are measured in pixels rather than characters now. Persian letters are
variable width, so a string's width depends on which letterforms it shapes into
and not on how many characters it has.

Nothing here knows what a boiler is. Screens are given lines and items; the
menu builds them.
"""

from __future__ import annotations

from text_shaper import has_rtl
from display_canvas import (
    Canvas,
    HEIGHT,
    WIDTH,
    fits,
    text_width,
    truncate,
    wrap_all,
)
from keypad_layout import (
    CANCEL,
    DEL,
    ENTER,
    NEXT,
    SCROLL_DOWN,
    SCROLL_UP,
    LineEditor,
    cap_for,
)

# -- geometry ---------------------------------------------------------------

# Rows are 13 px because that is what Persian needs. The title and legend get
# the same 13, and three body rows are what is left: 13 + 39 + 12 = 64.
TITLE_HEIGHT = 13
ROW_HEIGHT = 13
BODY_ROWS = 3
BODY_HEIGHT = BODY_ROWS * ROW_HEIGHT  # 39

BODY_TOP = TITLE_HEIGHT
LEGEND_TOP = BODY_TOP + BODY_HEIGHT  # 52
LEGEND_HEIGHT = HEIGHT - LEGEND_TOP  # 12

# The right-hand gutter the scroll bar lives in. Selection highlights stop
# short of it, so the bar stays readable on a highlighted row.
GUTTER = 4
BODY_WIDTH = WIDTH - GUTTER

TEXT_X = 2
# Both fonts are drawn from the top of the row and land on one shared baseline
# inside it, so a Persian descender and an ASCII digit sit on the same line.
TEXT_OFFSET = 0

# Widths available to text, in pixels. These replaced character-column
# counts: Persian letterforms are variable width, so how much fits depends on
# which letters a line is made of and not on how many it has.
BODY_COLUMNS = BODY_WIDTH - TEXT_X  # 122
BAR_COLUMNS = WIDTH - TEXT_X * 2  # 124


# The scroll keys, drawn as their caps with an arrowhead beside each rather
# than spelled out. "2/8 Move" is eight of the twenty-one columns a legend
# has; this is four, and an arrow says which way it goes better than the word
# does.
SCROLL_KEYS = ("\x01scroll", "")

_SCROLL_WIDTH = 6 + 6 + 4 + 6 + 6  # cap, arrow, gap, cap, arrow


def scroll_legend(*extra: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    """The standard strip: move, select, back — plus anything a screen adds."""
    return (
        SCROLL_KEYS,
        (cap_for(ENTER), "OK"),
        (cap_for(CANCEL), "Back"),
    ) + extra


# Legend labels the panel draws in Persian. Translated here rather than at every
# call site because the keycaps themselves are the same in both languages, and a
# legend is only ever assembled from these few words.
FA_LABELS = {
    "OK": "تأیید",
    "Back": "بازگشت",
    "Done": "انجام",
    "More": "بیشتر",
    "Yes": "بله",
    "No": "خیر",
    "On": "روشن",
    "Off": "خاموش",
}

# Titles the panel draws in Persian, keyed by the English title passed in. The
# menu passes English titles from one place and the terminal keeps printing
# English, so only the panel needs the lookup.
FA_TITLES = {
    "Menu": "منو",
    "Boiler room": "اتاق دیگ",
    "Status": "وضعیت",
    "Keypad": "صفحه‌کلید",
    "Schedule": "زمان‌بندی",
    "Temperatures": "دماها",
    "Safety limits": "حدود ایمنی",
    "Relay control": "راه‌اندازی رله",
    "Unit modes": "حالت‌ها",
    "Sensor readings": "خوانش‌ها",
    "App configuration": "پیکربندی",
    "Device mapping": "نگاشت دستگاه",
    "Error": "خطا",
    "Done": "انجام شد",
    "No change": "بدون تغییر",
    "Reported": "ارسال شد",
    "No targets": "بدون هدف",
    "No days": "بدون روز",
    "No rules": "بدون قانون",
    "No exceptions": "بدون استثنا",
}

# Lines that are fixed text on the panel rather than data the menu formats.
FA_LINES = {
    "Starting up ...": "در حال راه‌اندازی ...",
    "Agent stopped.": "عامل متوقف شد.",
    "Keypad did not start:": "صفحه‌کلید اجرا نشد:",
    "no keypad fitted": "صفحه‌کلید وصل نیست",
    "Back to menu": "بازگشت به منو",
    "No relays configured.": "رله‌ای پیکربندی نشده است.",
    "No boilers or pumps": "دیگ یا پمپی در نگاشت نیست",
    "in the device mapping.": "",
    "At least one target": "دست‌کم یک هدف",
    "must be selected.": "باید انتخاب شود.",
    "At least one day": "دست‌کم یک روز",
    "No weekly rules": "قانون هفتگی برای حذف نیست",
    "to delete.": "",
    "No date exceptions": "استثنای تاریخی برای حذف نیست",
    "No more weekly rules.": "قانون هفتگی دیگری نیست.",
    "Nothing to choose from.": "موردی برای انتخاب نیست.",
    "Mode reported to the server.": "حالت به سرور ارسال شد.",
}


def _label(label: str) -> str:
    return FA_LABELS.get(label, label)


def _entry_width(entry: tuple[str, str]) -> int:
    if entry == SCROLL_KEYS:
        return _SCROLL_WIDTH
    cap, label = entry
    return text_width(f"{cap} {_label(label)}" if label else cap)


class Screen:
    """Draws screens on a display and reads the keypad that answers them."""

    def __init__(self, display, device, *, echo=None):
        self.display = display
        self.device = device
        self.canvas = Canvas()

        # The mock display prints its frames; routing them through the menu's
        # own output keeps them from interleaving with it.
        set_writer = getattr(display, "set_writer", None)
        if set_writer is not None and echo is not None:
            set_writer(echo)

    # -- plumbing ------------------------------------------------------------

    async def render(self) -> None:
        await self.display.show(self.canvas)

    async def _key(self) -> str:
        """One keypress. Raises EOFError once the input device has gone."""
        return await self.device.read_key()

    # -- chrome --------------------------------------------------------------

    def _row(self, y: int, text: str, *, right_edge: int | None = None, on: bool = True) -> None:
        """
        Draw one line of body text, aligned to the language it is written in.

        Persian is set from the right edge and Latin from the left, because a
        reader expects the start of the line where they start reading. On a panel
        this narrow that matters: a left-aligned Persian label puts its first
        word at the far end from the eye and leaves the ragged edge on the side
        the reading begins.

        ``right_edge`` defaults to the body's right edge, which is where the
        scroll bar would otherwise sit, so a long line runs into the gutter
        rather than under the bar.
        """
        edge = WIDTH - GUTTER if right_edge is None else right_edge
        line = truncate(text, edge - TEXT_X)
        if has_rtl(line):
            self.canvas.text_right(edge, y, line, on=on)
        else:
            self.canvas.text(TEXT_X, y, line, on=on)

    def frame(
        self,
        title: str,
        *,
        right: str = "",
        legend: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Clear the canvas and draw the title bar and legend strip."""
        canvas = self.canvas
        canvas.clear()
        title = FA_TITLES.get(title, title)

        canvas.fill_rect(0, 0, WIDTH, TITLE_HEIGHT, True)
        room = BAR_COLUMNS
        if right:
            room = max(1, BAR_COLUMNS - text_width(right) - 4)
            canvas.text_right(WIDTH - TEXT_X, 0, right, on=False)
        shown = truncate(title.upper(), room)
        if has_rtl(shown):
            canvas.text_right(WIDTH - TEXT_X, 0, shown, on=False)
        else:
            canvas.text(TEXT_X, 0, shown, on=False)

        self._legend(legend)

    def _legend(self, entries: tuple[tuple[str, str], ...]) -> None:
        """
        The strip along the bottom: which key does what, on this screen.

        Always drawn, and drawn last, so there is no screen an operator can
        reach without being told the way out of it.
        """
        canvas = self.canvas
        canvas.fill_rect(0, LEGEND_TOP, WIDTH, LEGEND_HEIGHT, True)
        if not entries:
            return

        room = WIDTH - TEXT_X * 2
        content = sum(_entry_width(entry) for entry in entries)

        # Widest spacing that still fits, then the caps on their own. A strip
        # that has been cut in half says less than nothing.
        for gap in (18, 12, 6):
            if content + gap * (len(entries) - 1) <= room:
                break
        else:
            gap = 1
            entries = tuple(
                entry if entry == SCROLL_KEYS else (entry[0], "") for entry in entries
            )
            content = sum(_entry_width(entry) for entry in entries)

        total = content + gap * (len(entries) - 1)
        x = max(TEXT_X, (WIDTH - total) // 2)
        y = LEGEND_TOP + TEXT_OFFSET

        for index, entry in enumerate(entries):
            if index:
                x += gap
            if entry == SCROLL_KEYS:
                x = canvas.text(x, y, cap_for(SCROLL_UP), on=False)
                canvas.triangle_up(x, y + 3, on=False)
                x += 6 + 4
                x = canvas.text(x, y, cap_for(SCROLL_DOWN), on=False)
                canvas.triangle_down(x, y + 3, on=False)
                x += 6
                continue
            cap, label = entry
            x = canvas.text(x, y, f"{cap} {_label(label)}" if label else cap, on=False)

    def _scrollbar(self, top: int, visible: int, total: int) -> None:
        """A thumb on the right edge showing which slice of a list is shown."""
        if total <= visible:
            return

        canvas = self.canvas
        track_x = WIDTH - GUTTER + 1
        canvas.vline(track_x + 1, BODY_TOP, BODY_HEIGHT)

        height = max(4, BODY_HEIGHT * visible // total)
        span = max(1, total - visible)
        offset = (BODY_HEIGHT - height) * min(top, span) // span
        canvas.fill_rect(track_x, BODY_TOP + offset, 3, height, True)

    def _body_row(self, slot: int) -> int:
        return BODY_TOP + slot * ROW_HEIGHT

    # -- widgets -------------------------------------------------------------

    async def select(
        self,
        title: str,
        items: list[str],
        *,
        index: int = 0,
        legend: tuple[tuple[str, str], ...] | None = None,
    ) -> int | None:
        """
        Pick one row from a list. Returns its position, or None for back.

        The selection wraps at both ends: a list of eleven options is two
        presses from the bottom rather than nine, which on a keypad matters.

        ``next`` selects as well as ``enter``. It means "finish this field and
        go on", and a list has no fields — it is one choice — so on a screen
        like this the two keys have to mean the same thing. A screen whose
        legend names ``next`` is telling the truth rather than offering a key
        that does nothing.
        """
        total = len(items)
        if total == 0:
            await self.message(title, ["Nothing to choose from."])
            return None

        index = max(0, min(index, total - 1))
        top = 0
        strip = scroll_legend() if legend is None else legend

        while True:
            # Keep the selection on screen, moving the window as little as it
            # takes — a list that jumps around under the highlight is unreadable.
            if index < top:
                top = index
            elif index >= top + BODY_ROWS:
                top = index - BODY_ROWS + 1
            top = max(0, min(top, max(0, total - BODY_ROWS)))

            self.frame(title, right=f"{index + 1}/{total}", legend=strip)

            for slot in range(BODY_ROWS):
                position = top + slot
                if position >= total:
                    break
                y = self._body_row(slot)
                selected = position == index
                if selected:
                    self.canvas.fill_rect(0, y, BODY_WIDTH, ROW_HEIGHT, True)
                self._row(
                    y + TEXT_OFFSET,
                    items[position],
                    on=not selected,
                )

            self._scrollbar(top, BODY_ROWS, total)
            await self.render()

            key = await self._key()
            if key == SCROLL_UP:
                index = (index - 1) % total
            elif key == SCROLL_DOWN:
                index = (index + 1) % total
            elif key == ENTER or key == NEXT:
                return index
            elif key == CANCEL:
                return None

    async def page(self, title: str, lines: list[str]) -> None:
        """
        Show a block of text, scrolling a row at a time.

        ``#`` pages forward while there is more below and closes the screen at
        the bottom, so holding one key reads the whole thing; ``*`` leaves at
        any point.
        """
        # Translate the fixed lines here, before wrapping, so a Persian line is
        # measured at the width it will actually be drawn at. Lines built from
        # live data pass through untouched.
        wrapped = wrap_all([FA_LINES.get(line.strip(), line) for line in lines], BODY_COLUMNS)
        if not wrapped:
            return

        total = len(wrapped)
        top = 0
        limit = max(0, total - BODY_ROWS)

        while True:
            at_end = top >= limit
            if total > BODY_ROWS:
                strip = (
                    SCROLL_KEYS,
                    (cap_for(ENTER), _label("Done" if at_end else "More")),
                    (cap_for(CANCEL), "Back"),
                )
            else:
                strip = ((cap_for(ENTER), _label("Done")), (cap_for(CANCEL), _label("Back")))

            right = f"{min(top + BODY_ROWS, total)}/{total}" if total > BODY_ROWS else ""
            self.frame(title, right=right, legend=strip)

            for slot in range(BODY_ROWS):
                position = top + slot
                if position >= total:
                    break
                self.canvas.text(
                    TEXT_X,
                    self._body_row(slot) + TEXT_OFFSET,
                    wrapped[position],
                )

            self._scrollbar(top, BODY_ROWS, total)
            await self.render()

            key = await self._key()
            if key == SCROLL_UP:
                top = max(0, top - 1)
            elif key == SCROLL_DOWN:
                top = min(limit, top + 1)
            elif key == CANCEL:
                return
            elif key == ENTER:
                if at_end:
                    return
                top = min(limit, top + BODY_ROWS)

    async def message(self, title: str, lines: list[str]) -> None:
        await self.page(title, lines)

    async def read_line(
        self,
        prompt: str,
        *,
        title: str = "Enter value",
        mask: bool = False,
    ) -> str:
        """
        Collect an answer, showing it as it is typed.

        The same ``LineEditor`` the keypad uses for a whole line, so ``*``
        abandons the answer and every prompt in the menu already reads an empty
        answer as "back".

        ``mask`` shows one dot per digit instead of the digits — for the device
        password, which is the only secret anybody types here. The count still
        shows, because on a keypad with no tactile feedback that is what tells
        an operator whether a press registered.
        """
        editor = LineEditor()
        strip = (
            (cap_for(ENTER), "OK"),
            (cap_for(NEXT), "Next"),
            (cap_for(DEL), "Del"),
            (cap_for(CANCEL), "Back"),
        )

        # The question is at the end of a prompt, so when one is too long to
        # fit it is the opening that gets dropped, not the ask.
        question = wrap_all([prompt.rstrip().rstrip(":")], BODY_COLUMNS)
        question = question[-(BODY_ROWS - 1) :]

        while True:
            self.frame(title, legend=strip)

            for slot, line in enumerate(question):
                self._row(self._body_row(slot) + TEXT_OFFSET, line)

            entry_y = self._body_row(BODY_ROWS - 1)
            self.canvas.hline(0, entry_y - 1, WIDTH)

            text = "*" * len(editor.text) if mask else editor.text
            # Show the tail once an answer outgrows the row: what was just
            # typed is what needs checking. Sliced by width, not by count,
            # because a Persian prompt would otherwise lose the wrong end.
            visible = text
            while text_width(visible) > BODY_COLUMNS - 8 and visible:
                visible = visible[1:]
            end = self.canvas.text(TEXT_X + 2, entry_y + TEXT_OFFSET, visible)
            self.canvas.fill_rect(end, entry_y + 1, 4, ROW_HEIGHT - 2, True)

            await self.render()

            key = await self._key()
            if editor.feed(key):
                return editor.text

    async def splash(self, title: str, lines: list[str], *, legend=()) -> None:
        """
        Draw a screen and leave it there. Nothing is read.

        Titles and fixed lines are translated on the way in. Lines the menu
        formats from live data - a temperature, a status line - are left alone:
        those need translating where they are built, not here, because only the
        code that knows what a number means can say it in Persian.
        """
        self.frame(title, legend=legend)
        for slot, line in enumerate(lines[:BODY_ROWS]):
            self._row(
                self._body_row(slot) + TEXT_OFFSET,
                FA_LINES.get(line.strip(), line),
            )
        await self.render()

    async def select_checkboxes(
        self,
        title: str,
        items: list[str],
        *,
        index: int = 0,
        legend: tuple[tuple[str, str], ...] | None = None,
    ) -> tuple[list[bool], int] | tuple[None, None]:
        """
        Select multiple items with checkboxes.

        ENTER/OK toggles the checkbox at the current position.
        NEXT advances to the next step (returns current selection).
        CANCEL cancels (returns None, None).

        Returns (selected_list, last_index) or (None, None) if cancelled.
        """
        total = len(items)
        if total == 0:
            await self.message(title, ["Nothing to choose from."])
            return None, None

        selected = [False] * total
        index = max(0, min(index, total - 1))
        top = 0
        strip = scroll_legend() if legend is None else legend

        while True:
            # Keep the selection on screen
            if index < top:
                top = index
            elif index >= top + BODY_ROWS:
                top = index - BODY_ROWS + 1
            top = max(0, min(top, max(0, total - BODY_ROWS)))

            self.frame(title, right=f"{index + 1}/{total}", legend=strip)

            for slot in range(BODY_ROWS):
                position = top + slot
                if position >= total:
                    break
                y = self._body_row(slot)
                is_highlighted = position == index
                # The tick goes on the side the reader starts from, which for
                # Persian is the right. Putting it on the left of a Persian
                # label puts the mark a whole word away from what it marks.
                checkbox = "[x]" if selected[position] else "[ ]"
                text = (
                    f"{items[position]} {checkbox}"
                    if has_rtl(items[position])
                    else f"{checkbox} {items[position]}"
                )

                if is_highlighted:
                    self.canvas.fill_rect(0, y, BODY_WIDTH, ROW_HEIGHT, True)
                    self._row(y + TEXT_OFFSET, text, on=False)
                else:
                    self._row(y + TEXT_OFFSET, text, on=True)

            self._scrollbar(top, BODY_ROWS, total)
            await self.render()

            key = await self._key()
            if key == SCROLL_UP:
                index = (index - 1) % total
            elif key == SCROLL_DOWN:
                index = (index + 1) % total
            elif key == ENTER:
                selected[index] = not selected[index]
            elif key == NEXT:
                return selected, index
            elif key == CANCEL:
                return None, None

    async def select_list(
        self,
        title: str,
        items: list[str],
        *,
        index: int = 0,
        legend: tuple[tuple[str, str], ...] | None = None,
    ) -> int | None:
        """
        Select one item from a list.

        ENTER/OK/NEXT selects the item.
        CANCEL cancels (returns None).

        Returns the selected index or None if cancelled.
        """
        total = len(items)
        if total == 0:
            await self.message(title, ["Nothing to choose from."])
            return None

        index = max(0, min(index, total - 1))
        top = 0
        strip = scroll_legend() if legend is None else legend

        while True:
            if index < top:
                top = index
            elif index >= top + BODY_ROWS:
                top = index - BODY_ROWS + 1
            top = max(0, min(top, max(0, total - BODY_ROWS)))

            self.frame(title, right=f"{index + 1}/{total}", legend=strip)

            for slot in range(BODY_ROWS):
                position = top + slot
                if position >= total:
                    break
                y = self._body_row(slot)
                is_highlighted = position == index
                text = items[position]

                if is_highlighted:
                    self.canvas.fill_rect(0, y, BODY_WIDTH, ROW_HEIGHT, True)
                    self._row(y + TEXT_OFFSET, text, on=False)
                else:
                    self._row(y + TEXT_OFFSET, text, on=True)

            self._scrollbar(top, BODY_ROWS, total)
            await self.render()

            key = await self._key()
            if key == SCROLL_UP:
                index = (index - 1) % total
            elif key == SCROLL_DOWN:
                index = (index + 1) % total
            elif key == ENTER or key == NEXT:
                return index
            elif key == CANCEL:
                return None

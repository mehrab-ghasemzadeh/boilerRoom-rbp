import asyncio
import datetime

from display_canvas import Canvas, WIDTH, HEIGHT

LINK_UP = (
    ".......",
    ".......",
    "......#",
    "....#.#",
    "..#.#.#",
    "#.#.#.#",
    ".......",
)
LINK_DOWN = (
    ".......",
    ".......",
    "......#",
    "....#.#",
    "..#.#.#",
    "#.#.#.#",
    ".......",
)
LINK_WIDTH = 7
LINK_HEIGHT = 7


def _draw_link(canvas, x, y, connected):
    art = LINK_UP if connected else LINK_DOWN
    for row, line in enumerate(art):
        for col, bit in enumerate(line):
            if bit == "#":
                canvas.pixel(x + col, y + row, on=False)


class ScreenSaver:
    TIMEOUT = 60.0
    FADE_STEPS = 4
    FADE_DELAY = 0.03
    REFRESH_INTERVAL = 30.0  # update time/date every 30 seconds

    def __init__(self):
        self._active = False
        self._fading = False
        self._x = 0
        self._y = 0
        self._box_width = 70
        self._box_height = 50

    def should_activate(self, idle_seconds):
        return not self._active and idle_seconds > self.TIMEOUT

    def activate(self, canvas, link_fn, warning_fn):
        if self._active:
            return
        self._active = True
        self._fading = False

        self._x = (WIDTH - self._box_width) // 2
        self._y = (HEIGHT - self._box_height) // 2

        canvas.clear()
        self._draw(canvas, link_fn, warning_fn)

    def deactivate(self):
        self._active = False
        self._fading = False

    async def fade_out(self, canvas):
        if not self._active or self._fading:
            return
        self._fading = True

        steps = self.FADE_STEPS
        for i in range(steps):
            slice_h = self._box_height // steps
            y = self._y + i * slice_h
            h = slice_h if i < steps - 1 else self._box_height - i * slice_h
            canvas.fill_rect(self._x, y, self._box_width, h, on=False)
            await asyncio.sleep(self.FADE_DELAY)

        self._active = False
        self._fading = False

    def active(self):
        return self._active

    async def run(self, canvas, show_fn, link_fn, warning_fn):
        """Refresh time/date/indicators periodically without moving the box."""
        try:
            while self._active:
                self._draw(canvas, link_fn, warning_fn)
                await show_fn(canvas)
                await asyncio.sleep(self.REFRESH_INTERVAL)
        except asyncio.CancelledError:
            pass

    def _draw(self, canvas, link_fn, warning_fn):
        padding = 6
        x = self._x + padding
        y = self._y + padding

        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M")
        date_str = now.strftime("%Y-%m-%d")

        canvas.fill_rect(self._x, self._y, self._box_width, self._box_height, on=True)

        canvas.text(x, y, time_str, on=False)

        y += 13
        canvas.text(x, y, date_str, on=False)

        y += 13
        ix = x
        if link_fn:
            connected = bool(link_fn())
            if connected:
                _draw_link(canvas, ix, y, True)
                ix += LINK_WIDTH + 2
        if warning_fn:
            warn = bool(warning_fn())
            if warn:
                canvas.text(ix, y, "(!)", on=False)
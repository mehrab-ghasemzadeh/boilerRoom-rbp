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

import asyncio
import datetime
from collections.abc import Callable

from text_shaper import has_rtl

import language
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
from legend_content import get_legend

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
    "Open": "باز کردن",
    "Status": "وضعیت",
    "Cancel": "انصراف",
    "Change": "تغییر",
    "Del": "حذف",
    "Delete": "حذف",
    "Next": "بعدی",
    "Select": "انتخاب",
    "Set": "تعیین",
    "Toggle": "تغییر",
    "View": "مشاهده",
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
    "Relay control": "راه اندازی رله ها",
    "Unit modes": "حالت‌ها",
    "Sensor readings": "خوانش ها",
    # "Sensor readings": "داده های سنسور ها",
    "App configuration": "پیکربندی برنامه",
    "Device mapping": "نگاشت دستگاه",
    "Thermal sensor IDs": "آیدی سنسورها",
    "Error": "خطا",
    "Done": "انجام شد",
    "No change": "بدون تغییر",
    "Reported": "ارسال شد",
    "No targets": "بدون هدف",
    "No days": "بدون روز",
    "No rules": "بدون قانون",
    "No exceptions": "بدون استثنا",
    "Sign in": "ورود",
    "Change schedule": "تغییر زمان‌بندی",
    "Active schedule": "زمان‌بندی فعال",
    "Confirm delete": "تأیید حذف",
    "Delete weekly rule": "حذف قانون هفتگی",
    "Delete exception": "حذف استثنا",
    "Exception": "استثنا",
    "Mode for": "حالت برای",
    "Rule": "قانون",
    "Stop the agent?": "عامل متوقف شود؟",
    "Select days": "انتخاب روزها",
    "Select targets": "انتخاب هدف‌ها",
    "Switch state": "تغییر وضعیت",
    "Enter value": "ورود مقدار",
}

# Lines that are fixed text on the panel rather than data the menu formats.
FA_LINES = {
    "Starting up ...": "در حال راه‌ اندازی ...",
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
    # Startup and shutdown.
    "Shutting down ...": "در حال خاموش کردن ...",
    "Equipment units:": "واحدهای تجهیزات:",
    "Relay states:": "وضعیت رله‌ها:",
    "Relays:": "رله‌ها:",
    "Temperature sensors:": "حسگرهای دما:",
    "Unit modes:": "حالت واحدها:",
    "Targets:": "هدف‌ها:",
    "No readings yet.": "هنوز خوانشی نیست.",
    "Nothing to see yet.": "هنوز چیزی دیده نشده است.",
    "No boilers in the device mapping.": "دیگی در نگاشت دستگاه نیست.",
    "No boiler has its own temperature set.": "برای هیچ دیگی دمای مستقل تعیین نشده است.",
    "Relay controller not available.": "کنترلر رله در دسترس نیست.",
    # Config and connection details.
    "API base URL:": "نشانی پایه API:",
    "WebSocket URL:": "نشانی WebSocket:",
    "Device username:": "نام کاربری دستگاه:",
    "Device ID:": "شناسه دستگاه:",
    "Read interval:": "بازه خواندن:",
    "Telemetry every:": "گزارش هر:",
    "Mapping source:": "منبع نگاشت:",
    "Mapping file:": "فایل نگاشت:",
    "Authenticated:": "احراز هویت:",
    "WebSocket:": "WebSocket:",
    "Database:": "پایگاه داده:",
    "Database: unavailable (": "پایگاه داده: در دسترس نیست (",
    "Outbox:   empty (": "صندوق خروجی: خالی (",
    "Active config:    v": "پیکربندی فعال:    v",
    "Desired config:   v": "پیکربندی خواسته:   v",
    "schedule: v": "زمان‌بندی: v",
    "(not set)": "(تعیین نشده)",
    "(not logged in)": "(وارد نشده)",
    "connected": "متصل",
    "disconnected": "قطع",
    # Reading and schedule prose.
    "Last readings at": "آخرین خوانش‌ ها در",
    "(cycle": "(چرخه",
    "Scheduling rules:": "قوانین زمان‌بندی:",
    "Schedule v": "زمان‌بندی v",
    "Schedule updated —": "زمان‌بندی به‌روزرسانی شد —",
    "Now running a locally edited v": "اکنون نسخه محلی ویرایش‌شده v اجرا می‌شود",
    "locally edited (revision": "ویرایش محلی (بازنگری",
    ", on top of published v": "، بر پایه نسخه منتشرشده v",
    "Back to the published schedule v": "بازگشت به زمان‌بندی منتشرشده v",
    "Back to the published config v": "بازگشت به پیکربندی منتشرشده v",
    ". This is now the room's schedule, not a local edit.": ". اکنون زمان‌بندی اتاق است، نه ویرایش محلی.",
    "Published — the server created schedule v": "منتشر شد — سرور زمان‌بندی v ساخت",
    "Publishing to the server ...": "در حال ارسال به سرور ...",
    "is already": "هم‌اکنون",
    "is now": "اکنون",
    "Invalid mode.": "حالت نامعتبر است.",
    "Invalid relay ID.": "شناسه رله نامعتبر است.",
    "Invalid selection.": "انتخاب نامعتبر است.",
    "Cancelled.": "لغو شد.",
    "Rejected:": "رد شد:",
    "The server refused it:": "سرور آن را نپذیرفت:",
    "is not a number from the list.": "شماره‌ای از فهرست نیست.",
    "There is no target": "هدفی وجود ندارد",
    "Unknown option:": "گزینه ناشناخته:",
    # Limits.
    "Limits (published config v": "حدود (پیکربندی منتشرشده v",
    "Limits updated —": "حدود به‌روزرسانی شد —",
    "Now running locally edited limits on v": "اکنون حدود ویرایش محلی روی v اجرا می‌شود",
    "There are no local limit edits to discard.": "ویرایش محلی حدی برای کنار گذاشتن نیست.",
    "There are no local edits to discard.": "ویرایش محلی برای کنار گذاشتن نیست.",
    "is cut off by a temperature limit (": "به‌وسیله حد دما قطع می‌شود (",
    ") — refusing to switch it on.": ") — روشن کردن آن انجام نمی‌شود.",
    "). The server's next publish replaces it.": "). انتشار بعدی سرور آن را جایگزین می‌کند.",
    "). The server's next publish replaces them.": "). انتشار بعدی سرور آن‌ها را جایگزین می‌کند.",
    "now follows the device-wide limit.": "اکنون از حد سراسری دستگاه پیروی می‌کند.",
    # Offline and warning prose.
    "No answer from the server — the change is running here and will be published when the device reconnects.":
        "پاسخی از سرور نیامد — تغییر اینجا اجرا می‌شود و با اتصال دوباره دستگاه منتشر خواهد شد.",
    "Offline — the mode will be reported when the device reconnects.":
        "آفلاین — حالت با اتصال دوباره دستگاه گزارش می‌شود.",
    "The change is still running here, and will be offered again on the next connection.":
        "تغییر همچنان اینجا اجرا می‌شود و در اتصال بعدی دوباره ارسال خواهد شد.",
    "The config changed while you were editing it — most likely the server published one. Nothing was saved; take another look and try again.":
        "پیکربندی هنگام ویرایش تغییر کرد — به‌احتمال زیاد سرور یکی منتشر کرده است. چیزی ذخیره نشد؛ دوباره نگاه کنید و تلاش کنید.",
    "The schedule changed while you were editing it — most likely the server published one. Nothing was saved; take another look and try again.":
        "زمان‌بندی هنگام ویرایش تغییر کرد — به‌احتمال زیاد سرور یکی منتشر کرده است. چیزی ذخیره نشد؛ دوباره نگاه کنید و تلاش کنید.",
    "No schedule yet — adding a rule starts one on this device.":
        "هنوز زمان‌بندی نیست — افزودن قانون یکی روی این دستگاه می‌سازد.",
    "No config yet — setting a limit starts one on this device.":
        "هنوز پیکربندی نیست — تعیین حد یکی روی این دستگاه می‌سازد.",
    "Note: the server keeps the last temperature it was given; this only changes what this device holds the boiler at.":
        "توجه: سرور آخرین دمای دریافتی را نگه می‌دارد؛ این تنها دمای نگه‌داشته‌شده توسط این دستگاه را تغییر می‌دهد.",
    "WARNING: it could not be written to disk, so it will not survive a restart.":
        "هشدار: روی دیسک نوشته نشد، بنابراین پس از راه‌ اندازی دوباره باقی نمی‌ماند.",
    "WARNING: they could not be written to disk, so they will not survive a restart.":
        "هشدار: روی دیسک نوشته نشدند، بنابراین پس از راه‌ اندازی دوباره باقی نمی‌مانند.",
    "— enter a number (sensor polling continues in background).":
        "— یک شماره وارد کنید (نمونه‌برداری حسگر در پس‌زمینه ادامه دارد).",
    "Reported to the server.": "به سرور ارسال شد.",
    "Local edits discarded. Nothing has ever been published to this device, so no programme is driving the relays — they stay where they are until the server sends one.":
        "ویرایش‌های محلی کنار گذاشته شد. هرگز چیزی برای این دستگاه منتشر نشده است، بنابراین هیچ برنامه‌ای رله‌ها را هدایت نمی‌کند — تا زمانی که سرور برنامه‌ای بفرستد در وضعیت کنونی می‌مانند.",
    "Local limits discarded. Nothing has ever been published to this device, so there are now NO temperature limits and no over-temperature cut until the server sends one.":
        "حدود محلی کنار گذاشته شد. هرگز چیزی برای این دستگاه منتشر نشده است، بنابراین اکنون هیچ حد دمایی و هیچ قطع بیش‌ازحد دما وجود ندارد تا سرور یکی بفرستد.",
    "Control menu ready on the": "منوی کنترل آماده روی",
    "App configuration:": "پیکربندی برنامه:",
    "Boiler temperatures:": "دماهای دیگ:",
    "No boilers or pumps in the device mapping.": "دیگ یا پمپی در نگاشت دستگاه نیست.",
    "Reported": "ارسال شد",
    # Unit and protocol fragments shown beside a value.
    "Boiler": "دیگ",
    "Relay": "رله",
    "Sensor": "حسگر",
    "days": "روز",
    "rows from": "ردیف از",
    "recovered post(s) on record)": "پیام بازیابی‌شده در سابقه)",
    "KB, keeping": "کیلوبایت، نگه‌داشتن",
    ", GPIO": "، GPIO",
    ": unavailable": ": در دسترس نیست",
    ") boiler": ") دیگ",
    "(revision": "(بازنگری",
    "-> manual; the schedule will leave it alone until you set it back.":
        "-> دستی؛ زمان‌بندی تا بازگرداندن آن به‌حالت خودکار دست نمی‌زند.",
    "WS hello_ack:     yes (server_time=": "WS hello_ack:     بله (server_time=",
    "Boiler Room Monitoring System Started": "سامانه پایش اتاق دیگ آغاز شد",
    "Temperature sensor mapping:": "نگاشت حسگرهای دما:",
    "Relay mapping:": "نگاشت رله‌ها:",
    "No device mapping yet — this device's wiring comes from the server":
        "هنوز نگاشت دستگاهی نیست — سیم‌کشی این دستگاه از سابقه سرور می‌آید.",
    "record. Sensors and relays stay idle until it arrives.":
        "حسگرها و رله‌ها تا رسیدن آن بی‌کار می‌مانند.",
    # Sign-in wizard. These are short lines on purpose: the panel body is three
    # rows and the wizard has to stay readable at the operator's pace.
    "Cannot use that:": "قابل استفاده نیست:",
    "Checking with the": "در حال بررسی با",
    "server ...": "سرور ...",
    "Accepted.": "پذیرفته شد.",
    "Saved here. You will": "اینجا ذخیره شد. دیگر",
    "not be asked again.": "پرسیده نخواهد شد.",
    "Not accepted.": "پذیرفته نشد.",
    "Check the username": "نام کاربری را",
    "and password, then": "و رمز را بررسی کنید، سپس",
    "try again.": "دوباره تلاش کنید.",
    "No answer from the": "پاسخی از",
    "server yet.": "سرور نیامده است.",
    "Kept and retried in": "نگه داشته شد و در",
    "the background;": "پس‌زمینه دوباره تلاش می‌شود؛",
    "saved when it works.": "هنگام موفقیت ذخیره می‌شود.",
    "Left unsigned.": "بدون ورود رها شد.",
    "The boilers keep to": "دیگ‌ها به",
    "the cached schedule.": "زمان‌بندی ذخیره‌شده پایبند می‌مانند.",
    "Restart to be asked.": "برای پرسش دوباره راه‌ اندازی کنید.",
    "Not signed in yet.": "هنوز وارد نشده‌اید.",
    "provisioning.": "راه‌ اندازی اولیه.",
    # Rule and exception detail lines. Each is built as an f-string, so these
    # are the fixed parts; the values beside them stay as they are.
    "Rule ": "قانون ",
    "Exception ": "استثنا ",
    "Type the username": "نام کاربری را بنویسید",
    "and password from": "و رمز را از",
    "the keypad.": "صفحه‌کلید وارد کنید.",
    "Digits only.": "تنها ارقام.",
    # Status and configuration pages.
    "Weekly rules:": "قوانین هفتگی:",
    "Weekly rules: none": "قوانین هفتگی: هیچ",
    "Exceptions:": "استثناها:",
    "Exceptions: none": "استثناها: هیچ",
    "Limits:": "حدود:",
    "Limits: no boilers in the device mapping.": "حدود: دیگی در نگاشت دستگاه نیست.",
    "Now:": "اکنون:",
    "Now: no schedule": "اکنون: بدون زمان‌بندی",
    "No weekly rules": "قانون هفتگی نیست",
    "No date exceptions": "استثنای تاریخی نیست",
    "No more weekly rules.": "قانون هفتگی دیگری نیست.",
    "No more exceptions.": "استثنای دیگری نیست.",
    "No schedule received yet.": "هنوز زمان‌بندی دریافت نشده است.",
    "No config received yet.": "هنوز پیکربندی دریافت نشده است.",
    "No device record fetched yet.": "هنوز سابقه دستگاه دریافت نشده است.",
    "No relays configured.": "رله‌ای پیکربندی نشده است.",
    "No boilers in the": "دیگی در",
    "in the device mapping.": "نگاشت دستگاه نیست.",
    "No boilers or pumps": "دیگ یا پمپی",
    "device mapping.": "در نگاشت دستگاه نیست.",
    "In force here; published when": "اینجا برقرار است؛ منتشر می‌شود وقتی",
    "the device reconnects.": "دستگاه دوباره متصل شود.",
    "The mode will be reported": "حالت گزارش می‌شود",
    "when the device reconnects.": "وقتی دستگاه دوباره متصل شود.",
    "No answer from the server.": "پاسخی از سرور نیامد.",
    "Agent stopped.": "عامل متوقف شد.",
    "  Agent stopped.": "  عامل متوقف شد.",
    "  Starting up ...": "  در حال راه‌ اندازی ...",
    "Mode reported to the server.": "حالت به سرور ارسال شد.",
    "Keypad did not start:": "صفحه‌کلید اجرا نشد:",
    "  Keypad did not start:": "  صفحه‌کلید اجرا نشد:",
    "Input: keyboard (mock hardware)": "ورودی: صفحه‌کلید (سخت‌افزار نمایشی)",
    "Input: keyboard, restricted to the device's keypad:":
        "ورودی: صفحه‌کلید، محدود به صفحه‌کلید دستگاه:",
}


# Option labels in the selectable lists. The values the handlers compare against
# are unchanged, so only the row text differs. Rows carrying live data (a
# temperature, a unit name, a target id) are not in here: they are built with
# the data already in them and pass through untouched.
FA_ROWS = {
    # Mode choices.
    "automatic": "اتومات",
    "manual": "دستی",
    "ON": "روشن",
    "OFF": "خاموش",
    # Weekday abbreviations, in the order the schedule stores them.
    "Mon": "دوشنبه",
    "Tue": "سه‌شنبه",
    "Wed": "چهارشنبه",
    "Thu": "پنج‌شنبه",
    "Fri": "جمعه",
    "Sat": "شنبه",
    "Sun": "یکشنبه",
    # Schedule edit menus.
    "Add weekly rule": "افزودن قانون هفتگی",
    "Remove weekly rule": "حذف قانون هفتگی",
    "Add date exception": "افزودن استثنا",
    "Remove exception": "حذف استثنا",
    # Per-item actions and confirmations.
    "View details": "مشاهده جزئیات",
    "Delete this rule": "حذف این قانون",
    "Delete this exception": "حذف این استثنا",
    "Back to list": "بازگشت به فهرست",
    "No, keep it": "خیر، نگهش دار",
    "Yes, delete it": "بله، حذفش کن",
    "No, keep running": "خیر، ادامه بده",
    "Yes, stop it": "بله، متوقف کن",
    # Temperature setpoint list.
    "Device-wide limit": "حد سراسری دستگاه",
    # Reading line labels.
}


def _translate_row(text: str) -> str:
    """The Persian form of a fixed option label, or the text unchanged."""
    if not language.is_persian():
        return text
    return FA_ROWS.get(text, text)


# Fixed labels that appear inside a line that also carries live values. A line
# like "  Time: 08:00-22:00" is built with an f-string, so it is not a key in
# FA_LINES and cannot be; these are substituted into the text instead. Order
# matters only in that a longer label must come before a shorter one that starts
# the same way.
FA_FRAGMENTS = (
    ("  Time: ", "  ساعت: "),
    ("  Days: ", "  روزها: "),
    ("  Action: ", "  کنش: "),
    ("  Targets: ", "  هدف‌ها: "),
    ("  Date: ", "  تاریخ: "),
    ("  Window: ", "  بازه: "),
    ("  Reason: ", "  دلیل: "),
    ("Rule ", "قانون "),
    ("Exception ", "استثنا "),
    # The config and connection block. These are label/value rows, so the label
    # is fixed and only the value varies.
    ("  API base URL:     ", "  نشانی پایه API:     "),
    ("  WebSocket URL:    ", "  نشانی WebSocket:    "),
    ("  Device username:  ", "  نام کاربری دستگاه:  "),
    ("  Device ID:        ", "  شناسه دستگاه:        "),
    ("  Read interval:    ", "  بازه خواندن:    "),
    ("  Telemetry every:  ", "  گزارش هر:  "),
    ("  Mapping source:   ", "  منبع نگاشت:   "),
    ("  Mapping file:     ", "  فایل نگاشت:     "),
    ("  Authenticated:    ", "  احراز هویت:    "),
    ("  WebSocket:        ", "  WebSocket:        "),
    ("  WS hello_ack:     ", "  WS hello_ack:     "),
    ("  Active config:    ", "  پیکربندی فعال:    "),
    ("  Desired config:   ", "  پیکربندی خواسته:   "),
    (" schedule: v", " زمان‌بندی: v"),
    ("  Database: ", "  پایگاه داده: "),
    (" rows from ", " ردیف از "),
    (" sensor(s), ", " حسگر، "),
    (" KB, keeping ", " کیلوبایت، نگه‌داشتن "),
    (" days", " روز"),
    # Mapping and reading lines.
    ("  Sensor ", "  حسگر "),
    ("  Relay ", "  رله "),
    ("(role=", "(نقش="),
    (", unit=", "، واحد="),
    (", GPIO ", "، GPIO "),
    ("°C", " °C"),
    ("  Last readings at ", "  آخرین خوانش‌ ها در "),
    (" (cycle ", " (چرخه "),
    (": unavailable", ": در دسترس نیست"),
    (":   °C", ":   °C"),
    # Schedule and limits summary lines.
    ("Schedule v", "زمان‌بندی v"),
    ("Schedule updated — ", "زمان‌بندی به‌روزرسانی شد — "),
    ("Limits updated — ", "حدود به‌روزرسانی شد — "),
    ("Now running a locally edited v", "اکنون نسخه محلی ویرایش‌شده v اجرا می‌شود"),
    ("Now running locally edited limits on v", "اکنون حدود ویرایش محلی روی v اجرا می‌شود"),
    ("(revision ", "(بازنگری "),
    (", on top of published v", "، بر پایه نسخه منتشرشده v"),
    ("The server's next publish replaces it", "انتشار بعدی سرور آن را جایگزین می‌کند"),
    ("The server's next publish replaces them", "انتشار بعدی سرور آن‌ها را جایگزین می‌کند"),
    (" now follows the device-wide limit", " اکنون از حد سراسری دستگاه پیروی می‌کند"),
    ("— refusing to switch it on.", "— روشن کردن آن انجام نمی‌شود."),
    ("Back to the published schedule v", "بازگشت به زمان‌بندی منتشرشده v"),
    ("Back to the published config v", "بازگشت به پیکربندی منتشرشده v"),
    (") boiler", ") دیگ"),
    (" relay ", " رله "),
    ("enter a number", "یک شماره وارد کنید"),
    ("is already at ", " هم‌اکنون روی "),
    ("is already ", " هم‌اکنون "),
    ("is now ", " اکنون "),
    # Anti-freeze. Before the bare ON/OFF words below, which would otherwise
    # take the "ON" out of "Freeze ON" and leave the word Freeze in English.
    ("Freeze ON ", "ضدیخ روشن "),
    ("Anti-freeze: on below ", "ضدیخ: روشن زیر "),
    (", off above ", "، خاموش بالای "),
    ("  NO protection — ", "  بدون حفاظت — "),
    # Before the bare "water probe" below, which would otherwise take half of
    # this phrase and leave the rest of the sentence in English.
    ("no water probe to watch", "حسگر آبی برای پایش نیست"),
    ("water probes", "حسگر آب"),
    ("water probe", "حسگر آب"),
    ("  ENGAGED by ", "  فعال‌شده توسط "),
    ("  holding on: ", "  روشن نگه‌داشته: "),
    ("  not engaged", "  فعال نیست"),
    ("[set on this device]", "[تنظیم‌شده روی دستگاه]"),
    # Mode and state words, wherever they land in a line. Both are whole words
    # so this cannot touch a name that happens to contain them.
    (" automatic", " خودکار"),
    (" manual", " دستی"),
    (" ON", " روشن"),
    ("Panel language:", " زبان پنل: "),
    (" OFF", " خاموش"),
    ("ON", "روشن"),
    ("OFF", "خاموش"),
    ("is cut off by a temperature limit (", " به‌وسیله حد دما قطع می‌شود ("),
    # Weekday abbreviations, as the schedule stores them.
)


# The link indicator in the title bar, as (column, row) text: the wifi mark —
# three arcs opening downward over the emitter. Disconnected it is struck
# through. The slash rather than a dimmer or partial version of the same shape,
# because at seven pixels across "less ink" and "no ink" are the same picture,
# and a heating panel that has lost the server has to say so in a way nobody has
# to squint at.
LINK_UP = (
    ".......",
    ".......",
    ".....#.",
    "...#.#.",
    ".#.#.#.",
    ".......",
    ".......",
)
LINK_DOWN = (
    ".......",
    ".......",
    ".....#.",
    "...#.#.",
    ".#.#.#.",
    ".......",
    ".......",
)
LINK_WIDTH = 7
LINK_HEIGHT = 7


def _draw_link(canvas, x: int, y: int, connected: bool) -> None:
    """
    Draw the connection icon with its top-left at ``(x, y)``.

    The bar is filled and everything else is drawn inverted into it, so ``on``
    is False throughout.
    """
    art = LINK_UP if connected else LINK_DOWN
    for row, line in enumerate(art):
        for col, bit in enumerate(line):
            if bit == "#":
                canvas.pixel(x + col, y + row, on=False)


def _clock_text() -> str:
    """
    The time for the title bar, as HH:MM.

    Local wall-clock time, not UTC. Everything the device *stores* is UTC and
    stays that way, but the bar is for the person standing in front of it: a
    header reading 16:38 in the evening reads as a device that has stopped
    rather than one that is correct. Twenty-four hours rather than twelve,
    because a clock on a heating panel is read in the evening as often as the
    morning and an AM/PM flag is three pixels nobody can read at this size.

    This depends on the Pi's own timezone being set, since that is what "local"
    means here. Schedule times come from the server in the *schedule's*
    timezone, so on a box left on UTC this bar will not match them; that is
    worth fixing on the device rather than working around in the panel.
    """
    return datetime.datetime.now().strftime("%H:%M")


def _translate_line(text: str) -> str:
    """
    The Persian form of a body line, or the line unchanged.

    A whole fixed line is a key in FA_LINES. A line that mixes Persian-translated
    words with live values — a rule's time, a unit's name, a count — matches no
    key, so its fixed labels are substituted in place. Substituting only the
    labels is what keeps the values intact: a number, a device name and a relay
    id come through untouched because nothing here matches them.
    """
    if not language.is_persian():
        return text
    stripped = text.strip()
    whole = FA_LINES.get(stripped)
    if whole is not None:
        return whole
    for label, persian in FA_FRAGMENTS:
        if label in text:
            text = text.replace(label, persian)
    return text


def _label(label: str) -> str:
    if not language.is_persian():
        return label
    return FA_LABELS.get(label, label)


def _entry_width(entry: tuple[str, str]) -> int:
    if entry == SCROLL_KEYS:
        return _SCROLL_WIDTH
    cap, label = entry
    return text_width(f"{cap} {_label(label)}" if label else cap)


class Screen:
    """Draws screens on a display and reads the keypad that answers them."""

    def __init__(self, display, device, *, echo=None, link=None, warning=None):
        self.display = display
        self.device = device
        self.canvas = Canvas()

        # Whether the link to the server is up, asked fresh at every frame so a
        # reconnect shows up without waiting for the next redraw. None means the
        # caller does not know, which is drawn as disconnected rather than as a
        # reassuring blank.
        self.link = link

        # Whether there is a gas warning (any sensor > 400). Asked fresh at
        # every frame so the indicator updates immediately.
        self.warning = warning

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
        legend: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """
        Clear the canvas and draw the title bar and legend strip.

        The bar carries the page title, the connection icon (only when connected),
        the warning indicator (! when any gas sensor > 400), and the time.
        They are placed from opposite ends of the bar and the title is cut to
        whatever room is left, so no two can ever overlap however long the title
        turns out to be.
        """
        canvas = self.canvas
        canvas.clear()
        title = FA_TITLES.get(title, title) if language.is_persian() else title

        canvas.fill_rect(0, 0, WIDTH, TITLE_HEIGHT, True)

        clock = _clock_text()
        connected = bool(self.link and self.link())
        warning = bool(self.warning and self.warning())
        
        # Calculate widths for the right-side elements (clock, connection icon, warning icon)
        # Warning indicator is "(!)" = 3 chars
        warning_width = text_width("(!)") if warning else 0
        # Connection icon width + gap
        link_width = LINK_WIDTH + 2 if connected else 0
        # Clock width + gap
        clock_width = text_width(clock) + 2
        
        # Total used width on the right (for LTR) or left (for RTL)
        used = clock_width + link_width + warning_width
        room = max(0, BAR_COLUMNS - used - 4)
        shown = truncate(title.upper(), room)

        rtl = has_rtl(shown)
        if rtl:
            canvas.text_right(WIDTH - TEXT_X, 0, shown, on=False)
            x = TEXT_X
            # Clock on far left
            canvas.text(x, 0, clock, on=False)
            x += clock_width
            # Connection icon next to clock (only if connected)
            if connected:
                _draw_link(canvas, x, (TITLE_HEIGHT - LINK_HEIGHT) // 2, connected)
                x += link_width
            # Warning indicator (!) next to connection
            if warning:
                canvas.text(x, 0, "(!)", on=False)
        else:
            canvas.text(TEXT_X, 0, shown, on=False)
            x = WIDTH - TEXT_X - clock_width
            # Clock on far right
            canvas.text_right(WIDTH - TEXT_X, 0, clock, on=False)
            x -= clock_width
            # Warning indicator (!) left of clock
            if warning:
                x -= warning_width
                canvas.text(x, 0, "(!)", on=False)
            # Connection icon left of warning (only if connected)
            if connected:
                x -= link_width
                _draw_link(canvas, x, (TITLE_HEIGHT - LINK_HEIGHT) // 2, connected)

        self._legend(legend)

    def _legend(self, entries: tuple[tuple[str, str], ...]) -> None:
        """
        The strip along the bottom: which key does what, on this screen.

        Always drawn, and drawn last, so there is no screen an operator can
        reach without being told the way out of it.

        Legend format from legend_content.py: (key_cap, (english, persian))
        Special: SCROLL_KEYS ("\x01scroll", "") for scroll indicator
        """
        canvas = self.canvas
        canvas.fill_rect(0, LEGEND_TOP, WIDTH, LEGEND_HEIGHT, True)
        if not entries:
            return

        is_persian = language.is_persian()

        def _label(label: str) -> str:
            if not is_persian:
                return label
            return FA_LABELS.get(label, label)

        def _entry_width(entry: tuple) -> int:
            if entry == SCROLL_KEYS:
                return _SCROLL_WIDTH
            key_cap = entry[0]
            # New format: (key_cap, (english, persian))
            if len(entry) >= 2 and isinstance(entry[1], tuple):
                en, fa = entry[1]
                label = fa if is_persian else en
            else:
                # Old format: (key_cap, label) - label is English, needs translation
                label = entry[1] if len(entry) >= 2 else ""
                label = _label(label)
            return text_width(f"{key_cap} {label}" if label else key_cap)

        room = WIDTH - TEXT_X * 2
        content = sum(_entry_width(entry) for entry in entries)

        # Widest spacing that still fits, then the caps on their own
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
            key_cap = entry[0]
            # New format: (key_cap, (english, persian))
            if len(entry) >= 2 and isinstance(entry[1], tuple):
                en, fa = entry[1]
                label = fa if is_persian else en
            else:
                # Old format: (key_cap, label) - label is English, needs translation
                label = entry[1] if len(entry) >= 2 else ""
                label = _label(label)
            x = canvas.text(x, y, f"{key_cap} {label}" if label else key_cap, on=False)

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
        strip = get_legend("Default Select") if legend is None else legend

        while True:
            # Keep the selection on screen, moving the window as little as it
            # takes — a list that jumps around under the highlight is unreadable.
            if index < top:
                top = index
            elif index >= top + BODY_ROWS:
                top = index - BODY_ROWS + 1
            top = max(0, min(top, max(0, total - BODY_ROWS)))

            self.frame(title, legend=strip)

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
                    _translate_row(items[position]),
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
        wrapped = wrap_all([_translate_line(line) for line in lines], BODY_COLUMNS)
        if not wrapped:
            return

        total = len(wrapped)
        top = 0
        limit = max(0, total - BODY_ROWS)

        while True:
            at_end = top >= limit
            if total > BODY_ROWS:
                strip = get_legend("Default Page")
            else:
                strip = get_legend("Default Page")

            self.frame(title, legend=strip)

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

    async def watch(
        self,
        title: str,
        rows: Callable[[], list[str]],
        *,
        interval: float,
        legend: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        """
        Show a list that refreshes on its own.

        The same keys as a page: the scroll keys move a row at a
        time, and accept or back leaves. The list is re-read every
        ``interval`` seconds whether or not anything was pressed,
        because what it shows changes while nobody is looking at
        it — a probe fitted while the operator is watching for it
        should appear without a keypress.
        """
        top = 0
        strip = get_legend("Default Page") if legend is None else legend

        while True:
            items = rows() or ["Nothing to see yet."]
            total = len(items)
            limit = max(0, total - BODY_ROWS)
            top = min(top, limit)

            self.frame(title, legend=strip)

            for slot in range(BODY_ROWS):
                position = top + slot
                if position >= total:
                    break
                self._row(
                    self._body_row(slot) + TEXT_OFFSET,
                    _translate_line(items[position]),
                )

            self._scrollbar(top, BODY_ROWS, total)
            await self.render()

            try:
                # The key wait doubles as the refresh timer: a
                # press is handled at once, and silence redraws
                # with whatever the list says now.
                key = await asyncio.wait_for(self._key(), timeout=interval)
            except asyncio.TimeoutError:
                continue

            if key == SCROLL_UP:
                top = max(0, top - 1)
            elif key == SCROLL_DOWN:
                top = min(limit, top + 1)
            else:
                return

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
        strip = get_legend("Default Read Line")

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
        strip = get_legend("Default Splash") if not legend else legend
        self.frame(title, legend=strip)
        for slot, line in enumerate(lines[:BODY_ROWS]):
            self._row(
                self._body_row(slot) + TEXT_OFFSET,
                _translate_line(line),
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
        strip = get_legend("Default Checkboxes") if legend is None else legend

        while True:
            # Keep the selection on screen
            if index < top:
                top = index
            elif index >= top + BODY_ROWS:
                top = index - BODY_ROWS + 1
            top = max(0, min(top, max(0, total - BODY_ROWS)))

            self.frame(title, legend=strip)

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
                item = _translate_row(items[position])
                text = (
                    f"{item} {checkbox}"
                    if has_rtl(item)
                    else f"{checkbox} {item}"
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
        strip = get_legend("Default List") if legend is None else legend

        while True:
            if index < top:
                top = index
            elif index >= top + BODY_ROWS:
                top = index - BODY_ROWS + 1
            top = max(0, min(top, max(0, total - BODY_ROWS)))

            self.frame(title, legend=strip)

            for slot in range(BODY_ROWS):
                position = top + slot
                if position >= total:
                    break
                y = self._body_row(slot)
                is_highlighted = position == index
                text = _translate_row(items[position])

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

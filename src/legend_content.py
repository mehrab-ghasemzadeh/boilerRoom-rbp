"""
Centralized legend content for all control menu screens.

Keys map to keypad actions:
- 2: Up
- 8: Down
- 4: Back / Cancel
- 5: Select / Enter
- 6: Next

Each legend is a list of tuples: (key_cap, (english, persian))
"""

from keypad_layout import cap_for, ENTER, NEXT, CANCEL, SCROLL_UP, SCROLL_DOWN

def _t(en: str, fa: str) -> tuple[str, str]:
    """Translation helper returning (english, persian) tuple."""
    return (en, fa)

# Scroll keys - same as in screen.py
SCROLL_KEYS = ("\x01scroll", "")

# Standard scroll legend used by most list screens
SCROLL_LEGEND = (SCROLL_KEYS,) + (
    (cap_for(ENTER), _t("select", "انتخاب")),
    (cap_for(CANCEL), _t("back", "بازگشت")),
)

STD_LEGEND = SCROLL_LEGEND

# Standard legend with Next instead of Enter
SCROLL_NEXT_LEGEND = (SCROLL_KEYS,) + (
    (cap_for(NEXT), _t("next", "بعدی")),
    (cap_for(CANCEL), _t("back", "بازگشت")),
)

# Main menu - no scroll, has Next for entering submenus
ROOT_LEGEND = (SCROLL_KEYS,) + (
    # (cap_for(NEXT), _t("enter", "ورود")),
    (cap_for(ENTER), _t("select", "انتخاب")),
    (cap_for(CANCEL), _t("status", "وضعیت")),
)


# Relay control - both Enter and Next toggle
RELAY_LEGEND = (SCROLL_KEYS,) + (
    (cap_for(ENTER), _t("toggle", "تغییر")),
    # (cap_for(NEXT), _t("toggle", "تغییر")),
    (cap_for(CANCEL), _t("back", "بازگشت")),
)

STATIC_LEGEND = (SCROLL_KEYS,) + (
    (cap_for(CANCEL), _t("back", "بازگشت")),
)

SENSOR_IDS_LEGEND = (SCROLL_KEYS,) + (
    (cap_for(NEXT), _t("reset", "بازنشانی")),
    (cap_for(CANCEL), _t("back", "بازگشت")),
)

# Legend content dictionary - keys are English screen titles
LEGENDS = {
    # Main menu
    "Main Menu": ROOT_LEGEND,

    # Status / Read-only screens
    "Status": SCROLL_LEGEND,
    "Last Readings": STATIC_LEGEND,
    "Sensor IDs": SENSOR_IDS_LEGEND,
    "Boiler Room Status": SCROLL_LEGEND,
    "Boiler Room Config": SCROLL_LEGEND,
    "Sensor Mapping": SCROLL_LEGEND,
    "App Config": SCROLL_LEGEND,
    "App configuration": SCROLL_LEGEND,

    # Relay control
    "Relay Control": RELAY_LEGEND,
    "Relay Confirmation": (SCROLL_LEGEND,)+(
        (cap_for(ENTER), _t("confirm", "تایید")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),

    # Temperature settings
    "Temperatures": SCROLL_LEGEND,
    "Set Temperature": (SCROLL_LEGEND,)+(
        (cap_for(ENTER), _t("save", "ذخیره")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),

    # Unit modes
    "Unit Modes": SCROLL_LEGEND,
    "Mode Options": SCROLL_LEGEND,

    # Schedules
    "Schedule Editor": SCROLL_NEXT_LEGEND,
    "View Schedule": SCROLL_LEGEND,
    "Edit Schedule Entry": (
        (cap_for(ENTER), _t("edit", "ویرایش")),
        (cap_for(NEXT), _t("next field", "فیلد بعد")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),
    "Select Day Type": (
        (cap_for(ENTER), _t("select", "انتخاب")),
        (cap_for(CANCEL), _t("back", "بازگشت")),
    ),
    "Edit Time": (
        (cap_for(ENTER), _t("save", "ذخیره")),
        (cap_for(NEXT), _t("next", "بعدی")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),
    "Edit Temperature": (
        (cap_for(ENTER), _t("save", "ذخیره")),
        (cap_for(NEXT), _t("next", "بعدی")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),
    "Edit Enabled": (
        (cap_for(ENTER), _t("toggle", "تغییر")),
        (cap_for(CANCEL), _t("back", "بازگشت")),
    ),
    "Confirm Delete": (
        (cap_for(ENTER), _t("yes", "بله")),
        (cap_for(CANCEL), _t("no", "خیر")),
    ),
    "Confirm Discard": (
        (cap_for(ENTER), _t("discard", "انصراف")),
        (cap_for(CANCEL), _t("keep", "حفظ")),
    ),
    "Confirm Reset": (
        (cap_for(ENTER), _t("reset", "بازنشانی")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),

    # Antifreeze
    "Antifreeze": SCROLL_LEGEND,
    "Edit Antifreeze": (
        (cap_for(ENTER), _t("save", "ذخیره")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),

    # Language
    "Language": SCROLL_LEGEND,

    # Credentials / First boot
    "Sign In": (
        (cap_for(ENTER), _t("continue", "ادامه")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),
    "Device Username": (
        (cap_for(ENTER), _t("next", "بعدی")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),
    "Device Password": (
        (cap_for(ENTER), _t("sign in", "ورود")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),
    "Checking Credentials": STD_LEGEND,

    # Confirmation dialogs
    "Stop the agent?": (
        (cap_for(ENTER), _t("select", "انتخاب")),
        (cap_for(CANCEL), _t("back", "بازگشت")),
    ),
    "Menu error": STD_LEGEND,

    # Keypad fallback
    "Keypad": STD_LEGEND,

    # Screen.py widget defaults (for page, watch, select, select_checkboxes, select_list, read_line, splash)
    "Default Page": STD_LEGEND,
    "Default Select": (
        (cap_for(ENTER), _t("select", "انتخاب")),
        (cap_for(CANCEL), _t("back", "بازگشت")),
    ),
    "Default Checkboxes": (
        (cap_for(ENTER), _t("toggle", "انتخاب")),
        (cap_for(NEXT), _t("next", "بعدی")),
        (cap_for(CANCEL), _t("back", "بازگشت")),
    ),
    "Default List": SCROLL_LEGEND,
    "Default Read Line": (
        (cap_for(ENTER), _t("submit", "تایید")),
        (cap_for(CANCEL), _t("cancel", "انصراف")),
    ),
    "Default Splash": STD_LEGEND,
}


def get_legend(screen_title: str):
    """Get legend for a screen title, with fallback to default."""
    return LEGENDS.get(screen_title, STD_LEGEND)
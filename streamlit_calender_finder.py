from __future__ import annotations

import gc
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from math import ceil
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import recurring_ical_events
import streamlit as st
from icalendar import Calendar


# ============================================================
# CONFIGURATION
# ============================================================

APP_TIMEZONE = "Europe/Zurich"

WORKDAY_START = time(8, 0)
WORKDAY_END = time(17, 0)

MAX_FILES = 10
MAX_FILE_SIZE_MB = 5
MAX_TOTAL_UPLOAD_MB = 20

MAX_VEVENTS_PER_FILE = 5_000
MAX_ANALYSIS_DAYS = 90
MAX_ESTIMATED_EXPANDED_EVENTS = 50_000

SLOT_OPTIONS = {
    "30 Minuten": 30,
    "1 Stunde": 60,
    "1.5 Stunden": 90,
    "2 Stunden": 120,
    "2.5 Stunden": 150,
    "3 Stunden": 180,
}


# Only these properties are needed for availability calculations.
# Everything else is removed after parsing.
SAFE_EVENT_PROPERTIES = {
    "UID",
    "DTSTART",
    "DTEND",
    "DURATION",
    "RRULE",
    "RDATE",
    "EXDATE",
    "EXRULE",
    "RECURRENCE-ID",
    "STATUS",
    "TRANSP",
}


# ============================================================
# SAFE EXCEPTION
# ============================================================

class SafeCalendarError(Exception):
    """Validation error that can safely be shown to the user."""


# ============================================================
# DATE / TIME HELPERS
# ============================================================

def normalize_dt(
    value: date | datetime,
    tz: ZoneInfo,
) -> datetime:
    """
    Convert ICS date or datetime values to a timezone-aware datetime.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)

        return value.astimezone(tz)

    if isinstance(value, date):
        return datetime.combine(
            value,
            time.min,
            tzinfo=tz,
        )

    raise SafeCalendarError(
        "Ein Termin enthält ein ungültiges Datumsformat."
    )


def parse_ics_date_value(
    raw_value: object,
    tz: ZoneInfo,
) -> date | None:
    """
    Parse common calendar metadata date formats.
    """

    if not raw_value:
        return None

    value = str(raw_value).strip()

    formats = (
        "%Y%m%d",
        "%Y%m%dT%H%M%S",
        "%Y%m%dT%H%M%SZ",
        "%Y%m%dT%H%M",
        "%Y%m%dT%H%MZ",
    )

    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)

        except ValueError:
            continue

        if value.endswith("Z"):
            parsed = (
                parsed
                .replace(tzinfo=ZoneInfo("UTC"))
                .astimezone(tz)
            )

        else:
            parsed = parsed.replace(tzinfo=tz)

        return parsed.date()

    return None


# ============================================================
# CALENDAR COVERAGE
# ============================================================

def get_event_date_bounds(
    cal: Calendar,
    tz: ZoneInfo,
) -> tuple[date | None, date | None]:
    """
    Determine minimum and maximum dates from VEVENT entries.
    """

    earliest: date | None = None
    latest: date | None = None

    for component in cal.walk("VEVENT"):

        try:
            start_raw = component.decoded("DTSTART")
            start_dt = normalize_dt(start_raw, tz)

        except Exception:
            continue

        try:

            if component.get("DTEND") is not None:

                end_dt = normalize_dt(
                    component.decoded("DTEND"),
                    tz,
                )

            elif component.get("DURATION") is not None:

                end_dt = (
                    start_dt
                    + component.decoded("DURATION")
                )

            elif (
                isinstance(start_raw, date)
                and not isinstance(start_raw, datetime)
            ):

                # RFC 5545:
                # all-day events without DTEND occupy one full day
                end_dt = start_dt + timedelta(days=1)

            else:

                # Conservative fallback
                end_dt = start_dt + timedelta(hours=1)

        except Exception:

            if (
                isinstance(start_raw, date)
                and not isinstance(start_raw, datetime)
            ):
                end_dt = start_dt + timedelta(days=1)

            else:
                end_dt = start_dt + timedelta(hours=1)

        start_date = start_dt.date()
        end_date = end_dt.date()

        if earliest is None:
            earliest = start_date

        else:
            earliest = min(
                earliest,
                start_date,
            )

        if latest is None:
            latest = end_date

        else:
            latest = max(
                latest,
                end_date,
            )

    return earliest, latest


def get_calendar_coverage_dates(
    cal: Calendar,
    tz: ZoneInfo,
) -> tuple[date | None, date | None]:
    """
    Determine calendar coverage.

    Preference:
    1. X-CLIPSTART / X-CLIPEND
    2. X-CALSTART / X-CALEND
    3. VEVENT minimum / maximum
    """

    clip_start = parse_ics_date_value(
        cal.get("X-CLIPSTART"),
        tz,
    )

    clip_end = parse_ics_date_value(
        cal.get("X-CLIPEND"),
        tz,
    )

    cal_start = parse_ics_date_value(
        cal.get("X-CALSTART"),
        tz,
    )

    cal_end = parse_ics_date_value(
        cal.get("X-CALEND"),
        tz,
    )

    start_date = clip_start or cal_start
    end_date = clip_end or cal_end

    # Export end metadata often represents the first instant
    # after the requested export window.
    if end_date is not None:
        end_date -= timedelta(days=1)

    event_start, event_end = get_event_date_bounds(
        cal,
        tz,
    )

    start_date = start_date or event_start
    end_date = end_date or event_end

    if (
        start_date is None
        or end_date is None
        or end_date < start_date
    ):
        return None, None

    return start_date, end_date


# ============================================================
# FILE VALIDATION
# ============================================================

def looks_like_ics(raw: bytes) -> bool:
    """
    Basic server-side sanity check.

    The .ics file extension alone must never be trusted.
    """

    stripped = raw.lstrip(
        b"\xef\xbb\xbf\x00\t\r\n "
    )

    if not stripped.startswith(
        b"BEGIN:VCALENDAR"
    ):
        return False

    # Avoid scanning arbitrary unlimited data.
    tail = raw[-4096:]

    if b"END:VCALENDAR" not in tail:
        return False

    return True


# ============================================================
# DATA MINIMISATION
# ============================================================

def pseudonymize_uid(
    value: object,
    file_index: int,
    event_index: int,
) -> str:
    """
    Replace original ICS UIDs with SHA-256 hashes.

    The UID is needed internally to associate recurring events
    and recurrence overrides, but the original identifier does
    not need to be retained.
    """

    if value is None:
        source = (
            f"missing:"
            f"{file_index}:"
            f"{event_index}"
        )

    else:
        source = str(value)

    return sha256(
        source.encode(
            "utf-8",
            errors="ignore",
        )
    ).hexdigest()


def sanitize_calendar(
    cal: Calendar,
    file_index: int,
) -> Calendar:
    """
    Create a privacy-minimised calendar.

    The following are NOT retained:

    SUMMARY
    DESCRIPTION
    LOCATION
    ORGANIZER
    ATTENDEE
    CONTACT
    URL
    CLASS
    COMMENT
    ATTACH
    VALARM
    Custom X-properties

    Only scheduling information remains.
    """

    safe = Calendar()

    safe.add(
        "PRODID",
        "-//Secure Appointment Finder//EN",
    )

    safe.add(
        "VERSION",
        "2.0",
    )

    # Timezone information may be necessary to correctly
    # interpret calendar events.
    for component in cal.subcomponents:

        if component.name == "VTIMEZONE":

            safe.add_component(
                deepcopy(component)
            )

    for event_index, component in enumerate(
        cal.walk("VEVENT"),
        start=1,
    ):

        cleaned = deepcopy(component)

        # Remove all unnecessary properties.
        for key in list(cleaned.keys()):

            if key not in SAFE_EVENT_PROPERTIES:
                del cleaned[key]

        # Remove nested components such as alarms.
        cleaned.subcomponents = []

        original_uid = cleaned.get("UID")

        if "UID" in cleaned:
            del cleaned["UID"]

        cleaned.add(
            "UID",
            pseudonymize_uid(
                original_uid,
                file_index,
                event_index,
            ),
        )

        safe.add_component(cleaned)

    return safe


# ============================================================
# SAFE FILE PARSING
# ============================================================

def parse_and_sanitize_file(
    raw: bytes,
    file_index: int,
    tz: ZoneInfo,
) -> tuple[Calendar, date, date]:
    """
    Validate, parse and immediately minimise one ICS file.
    """

    if not raw:
        raise SafeCalendarError(
            "Eine Datei ist leer."
        )

    max_bytes = (
        MAX_FILE_SIZE_MB
        * 1024
        * 1024
    )

    if len(raw) > max_bytes:
        raise SafeCalendarError(
            f"Eine Datei ist grösser als "
            f"{MAX_FILE_SIZE_MB} MB."
        )

    if not looks_like_ics(raw):
        raise SafeCalendarError(
            "Eine Datei sieht nicht wie ein "
            "gültiger iCalendar Export aus."
        )

    try:
        cal = Calendar.from_ical(raw)

    except Exception as exc:

        # Do not expose parser internals to the user.
        raise SafeCalendarError(
            "Eine Datei konnte nicht sicher "
            "als iCalendar gelesen werden."
        ) from exc

    events = cal.walk("VEVENT")

    if not events:
        raise SafeCalendarError(
            "Eine Datei enthält keine Termine."
        )

    if len(events) > MAX_VEVENTS_PER_FILE:
        raise SafeCalendarError(
            f"Eine Datei enthält mehr als "
            f"{MAX_VEVENTS_PER_FILE:,} Termine "
            "und wird aus Sicherheitsgründen "
            "nicht verarbeitet."
        )

    coverage_start, coverage_end = (
        get_calendar_coverage_dates(
            cal,
            tz,
        )
    )

    if (
        coverage_start is None
        or coverage_end is None
    ):
        raise SafeCalendarError(
            "Für eine Datei konnte kein gültiger "
            "Kalenderzeitraum bestimmt werden."
        )

    safe = sanitize_calendar(
        cal,
        file_index,
    )

    # The full original parsed calendar is not retained.
    del cal

    return (
        safe,
        coverage_start,
        coverage_end,
    )


# ============================================================
# COMMON DATE RANGE
# ============================================================

def common_overlap(
    ranges: list[tuple[date, date]],
) -> tuple[date | None, date | None]:

    if not ranges:
        return None, None

    start = max(
        item[0]
        for item in ranges
    )

    end = min(
        item[1]
        for item in ranges
    )

    if start > end:
        return None, None

    return start, end


# ============================================================
# RECURRENCE PROTECTION
# ============================================================

def first_rrule_value(
    rrule: object,
    key: str,
    default: object = None,
) -> object:

    try:
        value = rrule.get(key)

    except Exception:
        return default

    if value is None:
        return default

    if isinstance(
        value,
        (list, tuple),
    ):
        if not value:
            return default

        return value[0]

    return value


def estimate_expansion(
    calendars: list[Calendar],
    range_start: date,
    range_end: date,
) -> int:
    """
    Estimate recurrence expansion cost before actually expanding.

    This protects against deliberately or accidentally huge
    recurrence rules.
    """

    days = (
        range_end
        - range_start
    ).days + 1

    total = 0

    for cal in calendars:

        for event in cal.walk("VEVENT"):

            rrule = event.get("RRULE")

            if not rrule:

                total += 1

            else:

                freq = str(
                    first_rrule_value(
                        rrule,
                        "FREQ",
                        "DAILY",
                    )
                ).upper()

                try:

                    interval = max(
                        1,
                        int(
                            first_rrule_value(
                                rrule,
                                "INTERVAL",
                                1,
                            )
                        ),
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    interval = 1

                # These frequencies could generate enormous
                # numbers of occurrences.
                if freq in {
                    "SECONDLY",
                    "MINUTELY",
                }:

                    raise SafeCalendarError(
                        "Sehr hochfrequente "
                        "Wiederholungsregeln "
                        "werden aus Sicherheitsgründen "
                        "nicht verarbeitet."
                    )

                if freq == "HOURLY":

                    estimate = (
                        ceil(
                            days
                            * 24
                            / interval
                        )
                        + 2
                    )

                elif freq == "DAILY":

                    estimate = (
                        ceil(
                            days
                            / interval
                        )
                        + 2
                    )

                elif freq == "WEEKLY":

                    estimate = (
                        ceil(
                            days
                            / (
                                7
                                * interval
                            )
                        )
                        + 2
                    )

                elif freq == "MONTHLY":

                    estimate = (
                        ceil(
                            days
                            / (
                                28
                                * interval
                            )
                        )
                        + 2
                    )

                elif freq == "YEARLY":

                    estimate = (
                        ceil(
                            days
                            / (
                                365
                                * interval
                            )
                        )
                        + 2
                    )

                else:

                    estimate = (
                        days
                        + 2
                    )

                count_value = first_rrule_value(
                    rrule,
                    "COUNT",
                )

                if count_value is not None:

                    try:
                        estimate = min(
                            estimate,
                            max(
                                0,
                                int(
                                    count_value
                                ),
                            ),
                        )

                    except (
                        TypeError,
                        ValueError,
                    ):
                        pass

                total += estimate

            if (
                total
                > MAX_ESTIMATED_EXPANDED_EVENTS
            ):

                raise SafeCalendarError(
                    "Die Kalender würden zu sehr vielen "
                    "wiederkehrenden Terminen expandieren. "
                    "Bitte wähle einen kürzeren Zeitraum "
                    "oder exportiere einen kleineren "
                    "Kalenderausschnitt."
                )

    return total


# ============================================================
# EVENT END TIME
# ============================================================

def event_end(
    event: object,
    start_raw: date | datetime,
    start_dt: datetime,
    tz: ZoneInfo,
) -> datetime:

    if event.get("DTEND") is not None:

        return normalize_dt(
            event.decoded("DTEND"),
            tz,
        )

    if event.get("DURATION") is not None:

        return (
            start_dt
            + event.decoded("DURATION")
        )

    if (
        isinstance(start_raw, date)
        and not isinstance(
            start_raw,
            datetime,
        )
    ):

        return (
            start_dt
            + timedelta(days=1)
        )

    return (
        start_dt
        + timedelta(hours=1)
    )


# ============================================================
# INTERVAL HELPERS
# ============================================================

def merge_intervals(
    intervals: list[
        tuple[
            datetime,
            datetime,
        ]
    ],
) -> list[
    tuple[
        datetime,
        datetime,
    ]
]:

    if not intervals:
        return []

    intervals = sorted(
        intervals,
        key=lambda item: item[0],
    )

    merged = [
        intervals[0]
    ]

    for start_dt, end_dt in intervals[1:]:

        (
            previous_start,
            previous_end,
        ) = merged[-1]

        if start_dt <= previous_end:

            merged[-1] = (
                previous_start,
                max(
                    previous_end,
                    end_dt,
                ),
            )

        else:

            merged.append(
                (
                    start_dt,
                    end_dt,
                )
            )

    return merged


def invert_intervals_within_window(
    busy_intervals: list[
        tuple[
            datetime,
            datetime,
        ]
    ],
    window_start: datetime,
    window_end: datetime,
) -> list[
    tuple[
        datetime,
        datetime,
    ]
]:

    free: list[
        tuple[
            datetime,
            datetime,
        ]
    ] = []

    cursor = window_start

    for busy_start, busy_end in busy_intervals:

        if (
            busy_end <= window_start
            or busy_start >= window_end
        ):
            continue

        clipped_start = max(
            busy_start,
            window_start,
        )

        clipped_end = min(
            busy_end,
            window_end,
        )

        if clipped_start > cursor:

            free.append(
                (
                    cursor,
                    clipped_start,
                )
            )

        cursor = max(
            cursor,
            clipped_end,
        )

    if cursor < window_end:

        free.append(
            (
                cursor,
                window_end,
            )
        )

    return free


def intersect_two_interval_lists(
    a: list[
        tuple[
            datetime,
            datetime,
        ]
    ],
    b: list[
        tuple[
            datetime,
            datetime,
        ]
    ],
) -> list[
    tuple[
        datetime,
        datetime,
    ]
]:

    i = 0
    j = 0

    output: list[
        tuple[
            datetime,
            datetime,
        ]
    ] = []

    while (
        i < len(a)
        and j < len(b)
    ):

        start_dt = max(
            a[i][0],
            b[j][0],
        )

        end_dt = min(
            a[i][1],
            b[j][1],
        )

        if start_dt < end_dt:

            output.append(
                (
                    start_dt,
                    end_dt,
                )
            )

        if a[i][1] < b[j][1]:
            i += 1

        else:
            j += 1

    return output


# ============================================================
# COLLECT BUSY TIMES
# ============================================================

def collect_busy_intervals(
    calendars: list[Calendar],
    range_start: date,
    range_end: date,
    timezone_name: str,
) -> list[
    list[
        tuple[
            datetime,
            datetime,
        ]
    ]
]:

    tz = ZoneInfo(
        timezone_name
    )

    lower = datetime.combine(
        range_start,
        time.min,
        tzinfo=tz,
    )

    upper = datetime.combine(
        range_end
        + timedelta(days=1),
        time.min,
        tzinfo=tz,
    )

    all_busy: list[
        list[
            tuple[
                datetime,
                datetime,
            ]
        ]
    ] = []

    for cal in calendars:

        try:

            expanded = (
                recurring_ical_events
                .of(cal)
                .between(
                    lower,
                    upper,
                )
            )

        except Exception as exc:

            raise SafeCalendarError(
                "Wiederkehrende Termine konnten "
                "nicht sicher verarbeitet werden."
            ) from exc

        if (
            len(expanded)
            > MAX_ESTIMATED_EXPANDED_EVENTS
        ):

            raise SafeCalendarError(
                "Zu viele expandierte Termine "
                "für eine sichere Verarbeitung."
            )

        intervals: list[
            tuple[
                datetime,
                datetime,
            ]
        ] = []

        for event in expanded:

            transparency = str(
                event.get(
                    "TRANSP",
                    "OPAQUE",
                )
            ).upper()

            status = str(
                event.get(
                    "STATUS",
                    "CONFIRMED",
                )
            ).upper()

            if transparency == "TRANSPARENT":
                continue

            if status == "CANCELLED":
                continue

            try:

                start_raw = event.decoded(
                    "DTSTART"
                )

                start_dt = normalize_dt(
                    start_raw,
                    tz,
                )

                end_dt = event_end(
                    event,
                    start_raw,
                    start_dt,
                    tz,
                )

            except Exception:
                continue

            if end_dt <= start_dt:
                continue

            if (
                end_dt <= lower
                or start_dt >= upper
            ):
                continue

            intervals.append(
                (
                    max(
                        start_dt,
                        lower,
                    ),
                    min(
                        end_dt,
                        upper,
                    ),
                )
            )

        all_busy.append(
            merge_intervals(
                intervals
            )
        )

    return all_busy


# ============================================================
# COMPUTE COMMON FREE SLOTS
# ============================================================

def compute_common_free_slots(
    calendars: list[Calendar],
    range_start: date,
    range_end: date,
    slot_minutes: int,
    timezone_name: str,
) -> pd.DataFrame:

    busy_by_calendar = (
        collect_busy_intervals(
            calendars,
            range_start,
            range_end,
            timezone_name,
        )
    )

    tz = ZoneInfo(
        timezone_name
    )

    min_duration = timedelta(
        minutes=slot_minutes
    )

    rows: list[
        dict[
            str,
            object,
        ]
    ] = []

    current_day = range_start

    while current_day <= range_end:

        # Monday to Friday only
        if current_day.weekday() < 5:

            work_start = datetime.combine(
                current_day,
                WORKDAY_START,
                tzinfo=tz,
            )

            work_end = datetime.combine(
                current_day,
                WORKDAY_END,
                tzinfo=tz,
            )

            common_free = [
                (
                    work_start,
                    work_end,
                )
            ]

            for busy_intervals in busy_by_calendar:

                daily_busy = [
                    (
                        start_dt,
                        end_dt,
                    )
                    for (
                        start_dt,
                        end_dt,
                    ) in busy_intervals
                    if (
                        end_dt > work_start
                        and start_dt < work_end
                    )
                ]

                daily_busy = merge_intervals(
                    daily_busy
                )

                daily_free = (
                    invert_intervals_within_window(
                        daily_busy,
                        work_start,
                        work_end,
                    )
                )

                common_free = (
                    intersect_two_interval_lists(
                        common_free,
                        daily_free,
                    )
                )

                if not common_free:
                    break

            for (
                start_dt,
                end_dt,
            ) in common_free:

                duration = (
                    end_dt
                    - start_dt
                )

                if duration >= min_duration:

                    rows.append(
                        {
                            "Datum":
                                current_day.strftime(
                                    "%d.%m.%Y"
                                ),

                            "Verfügbare Zeit":
                                (
                                    f"{start_dt.strftime('%H:%M')}"
                                    "–"
                                    f"{end_dt.strftime('%H:%M')}"
                                ),

                            "Dauer (Min.)":
                                int(
                                    duration
                                    .total_seconds()
                                    // 60
                                ),
                        }
                    )

        current_day += timedelta(
            days=1
        )

    return pd.DataFrame(
        rows,
        columns=[
            "Datum",
            "Verfügbare Zeit",
            "Dauer (Min.)",
        ],
    )


# ============================================================
# SESSION CLEANUP
# ============================================================

def clear_calendar_state() -> None:
    """
    Remove all remaining calendar-related data from the user's session.
    """

    keys = (
        "safe_calendars",
        "coverage_start",
        "coverage_end",
        "source_file_count",
    )

    for key in keys:
        st.session_state.pop(
            key,
            None,
        )

    gc.collect()


def reset_everything() -> None:
    """
    Remove calendar data and result data.
    """

    clear_calendar_state()

    for key in (
        "result_df",
        "result_meta",
        "import_notice",
    ):

        st.session_state.pop(
            key,
            None,
        )

    st.session_state.upload_generation = (
        st.session_state.get(
            "upload_generation",
            0,
        )
        + 1
    )

    st.session_state.analysis_generation = (
        st.session_state.get(
            "analysis_generation",
            0,
        )
        + 1
    )

    gc.collect()


# ============================================================
# STREAMLIT APP
# ============================================================

def main() -> None:

    st.set_page_config(
        page_title="Secure Appointment Finder",
        layout="wide",
    )

    st.session_state.setdefault(
        "upload_generation",
        0,
    )

    st.session_state.setdefault(
        "analysis_generation",
        0,
    )

    st.title(
        "Secure Appointment Finder"
    )

    st.caption(
        "Die App benötigt nur Belegt/Frei Zeiten. "
        "Titel, Beschreibungen, Orte, Teilnehmende, "
        "Organisatoren und Erinnerungen werden nach "
        "dem Einlesen verworfen."
    )


    # --------------------------------------------------------
    # PRIVACY INFORMATION
    # --------------------------------------------------------

    with st.expander(
        "Datenschutz Hinweis",
        expanded=True,
    ):

        st.markdown(
            """
**Bei Cloud Hosting werden hochgeladene Dateien zunächst an den Server übertragen.**

Diese App

* speichert selbst keine Kalenderdateien auf Disk
* verwendet bewusst kein `st.cache_data`
* verwendet bewusst kein `st.cache_resource`
* speichert keine Kalender in einer Datenbank
* schreibt keine Kalenderinhalte in Logs
* entfernt Terminname, Beschreibung, Ort, Teilnehmende und Organisator
* entfernt Erinnerungen und andere unnötige Kalendereigenschaften
* behält nur die Informationen, die zur Frei Belegt Berechnung benötigt werden
* verwirft auch diese reduzierten Kalenderdaten nach Abschluss der Berechnung

Für besonders sensible oder geschäftliche Kalender sollte die App auf einer kontrollierten und geeigneten Infrastruktur selbst gehostet werden.
            """
        )


    # --------------------------------------------------------
    # SIDEBAR
    # --------------------------------------------------------

    with st.sidebar:

        st.header(
            "Einstellungen"
        )

        timezone_name = st.text_input(
            "Zeitzone",
            value=APP_TIMEZONE,
        )

        slot_label = st.selectbox(
            "Mindestdauer",
            list(
                SLOT_OPTIONS.keys()
            ),
            index=2,
        )

        slot_minutes = (
            SLOT_OPTIONS[
                slot_label
            ]
        )

        if st.button(
            "Alle Sitzungsdaten löschen"
        ):

            reset_everything()
            st.rerun()


    # --------------------------------------------------------
    # TIMEZONE VALIDATION
    # --------------------------------------------------------

    try:
        ZoneInfo(
            timezone_name
        )

    except ZoneInfoNotFoundError:

        st.error(
            "Ungültige IANA Zeitzone."
        )

        st.stop()


    # --------------------------------------------------------
    # IMPORT SUCCESS MESSAGE
    # --------------------------------------------------------

    if st.session_state.pop(
        "import_notice",
        False,
    ):

        st.success(
            "Kalender wurden eingelesen und auf "
            "die für Frei Belegt notwendigen Daten reduziert."
        )


    # ========================================================
    # RESULT VIEW
    # ========================================================

    if "result_df" in st.session_state:

        df = (
            st.session_state.result_df
        )

        meta = (
            st.session_state.get(
                "result_meta",
                {},
            )
        )

        st.subheader(
            "Ergebnis"
        )

        if meta:

            st.caption(
                f"{meta.get('range_start')} "
                f"bis "
                f"{meta.get('range_end')} "
                f"· "
                f"{meta.get('file_count')} Kalender "
                f"· "
                f"Mindestdauer "
                f"{meta.get('slot_minutes')} Minuten"
            )

        if df.empty:

            st.warning(
                "Für den gewählten Zeitraum wurde "
                "kein gemeinsamer freier Zeitslot gefunden."
            )

        else:

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
            )

            csv_data = (
                df
                .to_csv(
                    index=False
                )
                .encode(
                    "utf-8-sig"
                )
            )

            st.download_button(
                "Ergebnis als CSV herunterladen",
                data=csv_data,
                file_name=(
                    "gemeinsame_zeitslots.csv"
                ),
                mime="text/csv",
            )

        if st.button(
            "Neue Analyse"
        ):

            reset_everything()
            st.rerun()

        st.stop()


    # ========================================================
    # STEP 1: UPLOAD
    # ========================================================

    if (
        "safe_calendars"
        not in st.session_state
    ):

        st.subheader(
            "1. Kalender sicher einlesen"
        )

        uploader_key = (
            "ics_upload_"
            f"{st.session_state.upload_generation}"
        )

        uploaded_files = st.file_uploader(
            f"Bis zu {MAX_FILES} "
            ".ics Dateien hochladen",
            type=[
                "ics"
            ],
            accept_multiple_files=True,
            key=uploader_key,
            help=(
                f"Maximal {MAX_FILE_SIZE_MB} MB "
                "pro Datei und "
                f"{MAX_TOTAL_UPLOAD_MB} MB insgesamt. "
                "Die Dateiendung allein ist keine "
                "Sicherheitsprüfung. "
                "Der Inhalt wird zusätzlich validiert."
            ),
        )


        # No files yet
        if not uploaded_files:

            st.info(
                "Bitte lade mindestens eine "
                ".ics Datei hoch."
            )

            st.stop()


        # Number of files
        if (
            len(uploaded_files)
            > MAX_FILES
        ):

            st.error(
                f"Bitte höchstens "
                f"{MAX_FILES} Dateien hochladen."
            )

            st.stop()


        # Total upload size
        total_size = sum(
            file.size
            for file in uploaded_files
        )

        max_total_bytes = (
            MAX_TOTAL_UPLOAD_MB
            * 1024
            * 1024
        )

        if (
            total_size
            > max_total_bytes
        ):

            st.error(
                "Die Dateien dürfen zusammen "
                f"höchstens "
                f"{MAX_TOTAL_UPLOAD_MB} MB gross sein."
            )

            st.stop()


        # ----------------------------------------------------
        # PARSE BUTTON
        # ----------------------------------------------------

        if st.button(
            "Kalender sicher einlesen",
            type="primary",
        ):

            tz = ZoneInfo(
                timezone_name
            )

            safe_calendars: list[
                Calendar
            ] = []

            ranges: list[
                tuple[
                    date,
                    date,
                ]
            ] = []

            try:

                for (
                    index,
                    uploaded_file,
                ) in enumerate(
                    uploaded_files,
                    start=1,
                ):

                    # Check actual size again server side.
                    if (
                        uploaded_file.size
                        > MAX_FILE_SIZE_MB
                        * 1024
                        * 1024
                    ):

                        raise SafeCalendarError(
                            f"Datei {index} ist grösser "
                            f"als {MAX_FILE_SIZE_MB} MB."
                        )

                    raw = (
                        uploaded_file
                        .getvalue()
                    )

                    (
                        safe,
                        start_date,
                        end_date,
                    ) = parse_and_sanitize_file(
                        raw,
                        index,
                        tz,
                    )

                    safe_calendars.append(
                        safe
                    )

                    ranges.append(
                        (
                            start_date,
                            end_date,
                        )
                    )

                    # Explicitly remove the original byte object.
                    del raw

                (
                    overlap_start,
                    overlap_end,
                ) = common_overlap(
                    ranges
                )

                if (
                    overlap_start is None
                    or overlap_end is None
                ):

                    raise SafeCalendarError(
                        "Die hochgeladenen Kalender "
                        "haben keinen gemeinsamen Zeitraum."
                    )

            except SafeCalendarError as exc:

                st.error(
                    str(exc)
                )

                st.stop()


            # Store only minimised calendars.
            st.session_state.safe_calendars = (
                safe_calendars
            )

            st.session_state.coverage_start = (
                overlap_start
            )

            st.session_state.coverage_end = (
                overlap_end
            )

            st.session_state.source_file_count = (
                len(
                    safe_calendars
                )
            )


            # Reset upload widget on the following rerun.
            st.session_state.upload_generation += 1

            st.session_state.analysis_generation += 1

            st.session_state.import_notice = True


            # Remove local reference.
            del safe_calendars

            gc.collect()

            st.rerun()

        st.stop()


    # ========================================================
    # STEP 2: ANALYSIS RANGE
    # ========================================================

    st.subheader(
        "2. Zeitraum auswählen"
    )

    earliest: date = (
        st.session_state
        .coverage_start
    )

    latest: date = (
        st.session_state
        .coverage_end
    )

    st.caption(
        "Gemeinsamer auswertbarer Zeitraum: "
        f"{earliest.strftime('%d.%m.%Y')} "
        "bis "
        f"{latest.strftime('%d.%m.%Y')}"
    )


    # Default to current date if possible.
    today = date.today()

    default_start = max(
        earliest,
        min(
            today,
            latest,
        ),
    )

    # Avoid defaulting to a huge range.
    default_end = min(
        default_start
        + timedelta(days=30),
        latest,
    )

    analysis_id = (
        st.session_state
        .analysis_generation
    )


    col1, col2 = st.columns(
        2
    )


    with col1:

        range_start = st.date_input(
            "Startdatum",
            value=default_start,
            min_value=earliest,
            max_value=latest,
            key=(
                f"range_start_"
                f"{analysis_id}"
            ),
        )


    with col2:

        range_end = st.date_input(
            "Enddatum",
            value=default_end,
            min_value=earliest,
            max_value=latest,
            key=(
                f"range_end_"
                f"{analysis_id}"
            ),
        )


    # --------------------------------------------------------
    # RANGE VALIDATION
    # --------------------------------------------------------

    if range_start > range_end:

        st.error(
            "Das Startdatum darf nicht "
            "nach dem Enddatum liegen."
        )

        st.stop()


    analysis_days = (
        range_end
        - range_start
    ).days + 1


    if (
        analysis_days
        > MAX_ANALYSIS_DAYS
    ):

        st.error(
            "Aus Sicherheits und Ressourcenschutzgründen "
            f"sind maximal "
            f"{MAX_ANALYSIS_DAYS} Tage "
            "pro Analyse erlaubt."
        )

        st.stop()


    # ========================================================
    # CALCULATE
    # ========================================================

    if st.button(
        "Freie Zeitslots berechnen",
        type="primary",
    ):

        calendars: list[
            Calendar
        ] = (
            st.session_state
            .safe_calendars
        )

        try:

            # Protection against recurrence bombs.
            estimate_expansion(
                calendars,
                range_start,
                range_end,
            )

            df = (
                compute_common_free_slots(
                    calendars=calendars,
                    range_start=range_start,
                    range_end=range_end,
                    slot_minutes=slot_minutes,
                    timezone_name=timezone_name,
                )
            )

        except SafeCalendarError as exc:

            st.error(
                str(exc)
            )

            st.stop()


        # Only result data is kept from this point onwards.
        st.session_state.result_df = df

        st.session_state.result_meta = {
            "range_start":
                range_start.strftime(
                    "%d.%m.%Y"
                ),

            "range_end":
                range_end.strftime(
                    "%d.%m.%Y"
                ),

            "file_count":
                st.session_state
                .source_file_count,

            "slot_minutes":
                slot_minutes,
        }


        # Calendar information is no longer needed.
        clear_calendar_state()

        gc.collect()

        st.rerun()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()

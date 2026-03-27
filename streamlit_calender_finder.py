from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from icalendar import Calendar
import recurring_ical_events


APP_TIMEZONE = "Europe/Zurich"
WORKDAY_START = time(8, 0)
WORKDAY_END = time(17, 0)
MAX_FILES = 10
SLOT_OPTIONS = {
    "30 Minuten": 30,
    "1 Stunde": 60,
    "1.5 Stunden": 90,
    "2 Stunden": 120,
    "2.5 Stunden": 150,
    "3 Stunden": 180,
}


def normalize_dt(value, tz: ZoneInfo) -> datetime:
    """Convert date/datetime from ICS to timezone-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)

    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=tz)

    raise TypeError(f"Unsupported date type: {type(value)}")


def parse_ics_date_value(raw_value: str, tz: ZoneInfo) -> date | None:
    """Parse common VCALENDAR metadata date fields like X-CLIPSTART/X-CLIPEND/X-CALSTART/X-CALEND."""
    if not raw_value:
        return None

    raw_value = str(raw_value).strip()
    formats = [
        "%Y%m%d",
        "%Y%m%dT%H%M%S",
        "%Y%m%dT%H%M%SZ",
        "%Y%m%dT%H%M",
        "%Y%m%dT%H%MZ",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(raw_value, fmt)
            if raw_value.endswith("Z"):
                parsed = parsed.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
            else:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.date()
        except ValueError:
            continue

    return None


def get_event_date_bounds(cal: Calendar, tz: ZoneInfo) -> tuple[date | None, date | None]:
    """Fallback: infer a calendar's covered range from its VEVENT entries."""
    earliest = None
    latest = None

    for component in cal.walk("VEVENT"):
        try:
            start_dt = normalize_dt(component.decoded("DTSTART"), tz)
        except Exception:
            continue

        end_prop = component.get("DTEND")
        duration_prop = component.get("DURATION")

        try:
            if end_prop is not None:
                end_dt = normalize_dt(component.decoded("DTEND"), tz)
            elif duration_prop is not None:
                end_dt = start_dt + component.decoded("DURATION")
            else:
                end_dt = start_dt + timedelta(hours=1)
        except Exception:
            end_dt = start_dt + timedelta(hours=1)

        start_date = start_dt.date()
        end_date = end_dt.date()

        if earliest is None or start_date < earliest:
            earliest = start_date
        if latest is None or end_date > latest:
            latest = end_date

    return earliest, latest


def get_calendar_coverage_dates(cal: Calendar, tz: ZoneInfo) -> tuple[date | None, date | None]:
    """Read the usable date range for one calendar.

    Preference order:
    1. VCALENDAR clip range metadata (X-CLIPSTART/X-CLIPEND)
    2. VCALENDAR calendar range metadata (X-CALSTART/X-CALEND)
    3. Min/max dates inferred from VEVENT entries
    """
    clip_start = parse_ics_date_value(cal.get("X-CLIPSTART"), tz)
    clip_end = parse_ics_date_value(cal.get("X-CLIPEND"), tz)
    cal_start = parse_ics_date_value(cal.get("X-CALSTART"), tz)
    cal_end = parse_ics_date_value(cal.get("X-CALEND"), tz)

    start_date = clip_start or cal_start
    end_date = clip_end or cal_end

    # X-CLIPEND / X-CALEND often point to the first instant AFTER the exported range.
    if end_date is not None:
        end_date = end_date - timedelta(days=1)

    event_start, event_end = get_event_date_bounds(cal, tz)

    if start_date is None:
        start_date = event_start
    if end_date is None:
        end_date = event_end

    if start_date is None or end_date is None:
        return None, None

    if end_date < start_date:
        return None, None

    return start_date, end_date


def get_common_calendar_overlap(calendars: list[Calendar], tz: ZoneInfo) -> tuple[date | None, date | None, list[tuple[date, date]]]:
    """Return intersection of all uploaded calendar coverage windows."""
    per_calendar_ranges: list[tuple[date, date]] = []

    for cal in calendars:
        start_date, end_date = get_calendar_coverage_dates(cal, tz)
        if start_date is None or end_date is None:
            return None, None, []
        per_calendar_ranges.append((start_date, end_date))

    common_start = max(start for start, _ in per_calendar_ranges)
    common_end = min(end for _, end in per_calendar_ranges)

    if common_end < common_start:
        return None, None, per_calendar_ranges

    return common_start, common_end, per_calendar_ranges


@st.cache_data(show_spinner=False)
def parse_calendar(file_bytes: bytes):
    return Calendar.from_ical(file_bytes)


@st.cache_data(show_spinner=False)
def collect_busy_intervals(file_bytes_list: list[bytes], range_start: date, range_end: date, timezone_name: str):
    tz = ZoneInfo(timezone_name)
    day_start_boundary = datetime.combine(range_start, time.min, tzinfo=tz)
    day_end_boundary = datetime.combine(range_end + timedelta(days=1), time.min, tzinfo=tz)

    calendars_busy = []
    calendar_labels = []

    for idx, file_bytes in enumerate(file_bytes_list, start=1):
        cal = parse_calendar(file_bytes)
        calendar_labels.append(f"Kalender {idx}")
        intervals = []

        expanded_events = recurring_ical_events.of(cal).between(day_start_boundary, day_end_boundary)

        for event in expanded_events:
            transp = str(event.get("TRANSP", "OPAQUE")).upper()
            status = str(event.get("STATUS", "CONFIRMED")).upper()
            if transp == "TRANSPARENT" or status == "CANCELLED":
                continue

            dtstart_raw = event.decoded("DTSTART")
            dtend_raw = event.get("DTEND")
            duration_raw = event.get("DURATION")

            start_dt = normalize_dt(dtstart_raw, tz)

            if dtend_raw is not None:
                end_dt = normalize_dt(event.decoded("DTEND"), tz)
            elif duration_raw is not None:
                end_dt = start_dt + event.decoded("DURATION")
            else:
                end_dt = start_dt + timedelta(hours=1)

            if end_dt <= day_start_boundary or start_dt >= day_end_boundary:
                continue

            intervals.append((max(start_dt, day_start_boundary), min(end_dt, day_end_boundary)))

        calendars_busy.append(merge_intervals(intervals))

    return calendars_busy, calendar_labels


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []

    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [intervals[0]]

    for start_dt, end_dt in intervals[1:]:
        prev_start, prev_end = merged[-1]
        if start_dt <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end_dt))
        else:
            merged.append((start_dt, end_dt))

    return merged


def invert_intervals_within_window(
    busy_intervals: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    free = []
    cursor = window_start

    for busy_start, busy_end in busy_intervals:
        if busy_end <= window_start or busy_start >= window_end:
            continue

        clipped_start = max(busy_start, window_start)
        clipped_end = min(busy_end, window_end)

        if clipped_start > cursor:
            free.append((cursor, clipped_start))

        cursor = max(cursor, clipped_end)

    if cursor < window_end:
        free.append((cursor, window_end))

    return free


def intersect_two_interval_lists(
    a: list[tuple[datetime, datetime]],
    b: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    i, j = 0, 0
    out = []

    while i < len(a) and j < len(b):
        start_dt = max(a[i][0], b[j][0])
        end_dt = min(a[i][1], b[j][1])

        if start_dt < end_dt:
            out.append((start_dt, end_dt))

        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1

    return out


@st.cache_data(show_spinner=False)
def compute_common_free_slots(
    file_bytes_list: list[bytes],
    range_start: date,
    range_end: date,
    slot_minutes: int,
    timezone_name: str,
):
    tz = ZoneInfo(timezone_name)
    calendars_busy, _labels = collect_busy_intervals(file_bytes_list, range_start, range_end, timezone_name)

    rows = []
    current_day = range_start

    while current_day <= range_end:
        if current_day.weekday() >= 5:
            current_day += timedelta(days=1)
            continue

        work_start_dt = datetime.combine(current_day, WORKDAY_START, tzinfo=tz)
        work_end_dt = datetime.combine(current_day, WORKDAY_END, tzinfo=tz)

        common_free = [(work_start_dt, work_end_dt)]
        for busy_intervals in calendars_busy:
            daily_busy = []
            for busy_start, busy_end in busy_intervals:
                if busy_end <= work_start_dt or busy_start >= work_end_dt:
                    continue
                daily_busy.append((busy_start, busy_end))

            daily_busy = merge_intervals(daily_busy)
            daily_free = invert_intervals_within_window(daily_busy, work_start_dt, work_end_dt)
            common_free = intersect_two_interval_lists(common_free, daily_free)
            if not common_free:
                break

        min_duration = timedelta(minutes=slot_minutes)
        for start_dt, end_dt in common_free:
            if end_dt - start_dt >= min_duration:
                rows.append(
                    {
                        "Datum": current_day.strftime("%d.%m.%Y"),
                        "Verfügbare Zeit": f"{start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')}",
                        "Dauer (Min.)": int((end_dt - start_dt).total_seconds() // 60),
                    }
                )

        current_day += timedelta(days=1)

    return pd.DataFrame(rows)


def main():
    st.set_page_config(page_title="Gemeinsame ICS-Zeitslots", layout="wide")
    st.title("Gemeinsame freie Zeitslots aus .ics-Kalendern")
    st.write(
        "Lade bis zu 10 .ics-Dateien hoch. Die App findet alle gemeinsamen freien Zeitfenster "
        "innerhalb der Arbeitszeit von 08:00 bis 17:00."
    )

    with st.sidebar:
        st.header("Einstellungen")
        timezone_name = st.text_input("Zeitzone", value=APP_TIMEZONE)
        slot_label = st.selectbox("Mindestdauer des Zeitslots", list(SLOT_OPTIONS.keys()), index=2)
        slot_minutes = SLOT_OPTIONS[slot_label]

    uploaded_files = st.file_uploader(
        "Bis zu 10 .ics-Dateien hochladen",
        type=["ics"],
        accept_multiple_files=True,
    )

    if uploaded_files and len(uploaded_files) > MAX_FILES:
        st.error(f"Bitte höchstens {MAX_FILES} Dateien hochladen.")
        st.stop()

    if not uploaded_files:
        st.info("Bitte lade mindestens eine .ics-Datei hoch.")
        st.stop()

    file_bytes_list = [uploaded_file.getvalue() for uploaded_file in uploaded_files]

    with st.spinner("Analysiere Kalenderzeitraum …"):
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            st.error(f"Ungültige Zeitzone: {timezone_name}")
            st.stop()

        calendars = []
        for idx, file_bytes in enumerate(file_bytes_list, start=1):
            try:
                calendars.append(parse_calendar(file_bytes))
            except Exception as exc:
                st.error(f"Datei {idx} konnte nicht als .ics gelesen werden: {exc}")
                st.stop()

        overlap_start, overlap_end, per_calendar_ranges = get_common_calendar_overlap(calendars, tz)

        if not per_calendar_ranges:
            st.error("In den hochgeladenen Dateien wurde kein gültiger Kalenderzeitraum gefunden.")
            st.stop()

        if overlap_start is None or overlap_end is None:
            range_info = "\n".join(
    [
        f"- Kalender {idx}: {start.strftime('%d.%m.%Y')} bis {end.strftime('%d.%m.%Y')}"
        for idx, (start, end) in enumerate(per_calendar_ranges, start=1)
    ]
)
            st.error(
    "Die hochgeladenen Kalender haben keinen gemeinsamen Überschneidungszeitraum.\n\n"
    f"Erkannte Kalenderzeiträume:\n{range_info}")

            st.stop()

        earliest = overlap_start
        latest = overlap_end

    st.subheader("Analysebereich")
    st.caption(
        f"Gemeinsamer auswertbarer Zeitraum aller hochgeladenen Kalender: {earliest.strftime('%d.%m.%Y')} bis {latest.strftime('%d.%m.%Y')}"
    )
    c1, c2 = st.columns(2)
    with c1:
        range_start = st.date_input(
            "Startdatum",
            value=earliest,
            min_value=earliest,
            max_value=latest,
        )
    with c2:
        range_end = st.date_input(
            "Enddatum",
            value=latest,
            min_value=earliest,
            max_value=latest,
        )

    if range_start is None or range_end is None:
        st.error(
            "Start- und Enddatum konnten nicht gesetzt werden. Bitte Zeitraum neu auswählen oder die hochgeladenen Kalender prüfen."
        )
        st.stop()

    if isinstance(range_start, tuple) or isinstance(range_end, tuple):
        st.error("Bitte wähle ein einzelnes Start- und Enddatum, keinen Datumsbereich in einem Feld.")
        st.stop()

    if range_start > range_end:
        st.error("Das Startdatum darf nicht nach dem Enddatum liegen.")
        st.stop()

    if st.button("Freie Zeitslots berechnen", type="primary"):
        with st.spinner("Berechne gemeinsame freie Zeitfenster …"):
            df = compute_common_free_slots(
                file_bytes_list=file_bytes_list,
                range_start=range_start,
                range_end=range_end,
                slot_minutes=slot_minutes,
                timezone_name=timezone_name,
            )

        st.subheader("Ergebnis")
        if df.empty:
            st.warning("Für den gewählten Zeitraum wurde kein gemeinsamer freier Zeitslot mit dieser Mindestdauer gefunden.")
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)
            csv_data = df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "Ergebnis als CSV herunterladen",
                data=csv_data,
                file_name="gemeinsame_zeitslots.csv",
                mime="text/csv",
            )

    with st.expander("Hinweise"):
        st.markdown(
            """
            - Arbeitszeitfenster: **08:00–17:00**
            - Nur **Montag bis Freitag** werden berücksichtigt.
            - Termine mit `TRANSPARENT` oder `CANCELLED` werden ignoriert.
            - Wiederkehrende Termine werden über `recurring-ical-events` expandiert.
            - Die Ausgabe zeigt **zusammenhängende freie Zeitfenster**, deren Dauer mindestens der gewählten Mindestdauer entspricht.
            """
        )


if __name__ == "__main__":
    main()

# 📅 ICS Calendar Common Slot Finder

A Streamlit-based web application that analyzes multiple `.ics` calendar files and identifies common free time slots across all participants.

## 🚀 Features

* Upload up to **10 `.ics` calendar files**
* Automatically detects the **overlapping date range** across all calendars
* Supports **recurring events** (e.g. weekly meetings)
* Filters out:

  * Transparent events (`TRANSPARENT`)
  * Cancelled events (`CANCELLED`)
* Configurable **minimum meeting duration**:

  * 30 minutes to 3 hours
* Calculates **common free time slots** within working hours (08:00–17:00)
* Outputs results as:

  * Interactive table
  * Downloadable CSV file

---

## 🧠 How It Works

1. Each uploaded calendar is parsed and expanded (including recurring events).
2. The app determines the **intersection of all calendar date ranges**.
3. Busy time intervals are extracted and merged.
4. Free time windows are computed for each day.
5. Only time slots that satisfy the **minimum duration requirement** are returned.

---

## 📊 Example Use Case

You have multiple team members' calendars and want to find a meeting slot:

* Person A: Jan–Jun
* Person B: May–Sep

👉 The app automatically evaluates only the **overlapping period (May–Jun)** and finds all possible meeting slots.

---

## 🛠️ Installation

Clone the repository:

```bash
git clone https://github.com/EliValLoc/appointment_finder.git
cd appointment_finder
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install streamlit pandas icalendar recurring-ical-events
```

---

## ▶️ Usage

Run the Streamlit app:

```bash
python -m streamlit run streamlit_calender_finder.py
```

Then open the app in your browser and:

1. Upload your `.ics` files
2. Select desired meeting duration
3. Choose date range (auto-limited to overlap)
4. Click **"Freie Zeitslots berechnen"**

---

## ⚙️ Configuration

* Default timezone: `Europe/Zurich`
* Working hours: `08:00 – 17:00`
* Weekends are excluded

---

## 📦 Tech Stack

* [Streamlit](https://streamlit.io/)
* [pandas](https://pandas.pydata.org/)
* [icalendar](https://pypi.org/project/icalendar/)
* [recurring-ical-events](https://pypi.org/project/recurring-ical-events/)

---

## ⚠️ Notes

* All processing happens locally in the app session
* No calendar data is stored or transmitted externally
* Accuracy depends on the correctness of the `.ics` files

---

## 💡 Future Improvements

* Generate **all possible start times** within free windows
* Support for custom working hours
* Timezone auto-detection per calendar
* UI improvements for large datasets

---

## 📄 License

MIT License

---

## 🙌 Acknowledgements

Built with the help of OpenAI's GPT models.

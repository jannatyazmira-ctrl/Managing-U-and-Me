# -*- coding: utf-8 -*-
"""
Managing U and Me - Couples Finance App
Fixed version with working calendar and analytics
"""

# -----------------------
# Imports (deduped/clean)
# -----------------------
import json
import calendar
import hashlib
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# -----------------------
# Database: base schema
# -----------------------
def init_database():
    conn = sqlite3.connect('couples_finance_app.db')
    cursor = conn.cursor()

    # Couple accounts
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS couple_accounts (
            couple_id TEXT PRIMARY KEY,
            couple_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            partner1_name TEXT NOT NULL,
            partner2_name TEXT NOT NULL,
            created_date TEXT NOT NULL
        )
    ''')

    # Income/Expenses/Savings
    for table in ("income", "expenses", "savings"):
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                couple_id TEXT NOT NULL,
                partner_name TEXT,
                date TEXT,
                month INTEGER,
                year INTEGER,
                amount REAL,
                source TEXT,
                note TEXT,
                FOREIGN KEY (couple_id) REFERENCES couple_accounts (couple_id)
            )
        ''')

    # Calendar events with all fields
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            couple_id TEXT NOT NULL,
            partner_name TEXT,
            date TEXT,
            time TEXT,
            start_time TEXT,
            end_time TEXT,
            timezone TEXT DEFAULT 'Asia/Tokyo',
            assigned_to TEXT,
            created_by TEXT,
            title TEXT,
            category TEXT,
            color TEXT,
            description TEXT,
            recurrence TEXT DEFAULT 'none',
            created_at TEXT,
            FOREIGN KEY (couple_id) REFERENCES couple_accounts (couple_id)
        )
    ''')

    # Calendar comments
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS calendar_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER,
            couple_id TEXT,
            partner_name TEXT,
            comment TEXT,
            timestamp TEXT,
            FOREIGN KEY (event_id) REFERENCES calendar_events (id),
            FOREIGN KEY (couple_id) REFERENCES couple_accounts (couple_id)
        )
    ''')

    # Todos
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            couple_id TEXT NOT NULL,
            partner_name TEXT,
            title TEXT,
            task TEXT,
            completed BOOLEAN DEFAULT 0,
            created_date TEXT,
            FOREIGN KEY (couple_id) REFERENCES couple_accounts (couple_id)
        )
    ''')

    # Savings goals
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            couple_id TEXT NOT NULL,
            goal_name TEXT,
            target_amount REAL,
            current_amount REAL DEFAULT 0,
            created_date TEXT,
            FOREIGN KEY (couple_id) REFERENCES couple_accounts (couple_id)
        )
    ''')

    # Event templates
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS event_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            category TEXT,
            default_duration INTEGER,
            default_color TEXT,
            suggested_times TEXT
        )
    ''')

    # Time tracking
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS time_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            couple_id TEXT,
            partner_name TEXT,
            category TEXT,
            date TEXT,
            duration_minutes INTEGER,
            FOREIGN KEY (couple_id) REFERENCES couple_accounts (couple_id)
        )
    ''')

    conn.commit()
    conn.close()

def ensure_calendar_schema(db_path="couples_finance_app.db"):
    """Lightweight migration so old DBs gain new calendar columns safely."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(calendar_events)")
    cols = {row[1] for row in cur.fetchall()}

    # Add missing time columns
    if "time" not in cols:
        cur.execute("ALTER TABLE calendar_events ADD COLUMN time TEXT")
    if "start_time" not in cols:
        cur.execute("ALTER TABLE calendar_events ADD COLUMN start_time TEXT")
    if "end_time" not in cols:
        cur.execute("ALTER TABLE calendar_events ADD COLUMN end_time TEXT")

    # Backfill start_time from legacy 'time'
    cur.execute("""
        UPDATE calendar_events
        SET start_time = COALESCE(start_time, time)
        WHERE (start_time IS NULL OR start_time = '')
    """)

    # Add other newer columns defensively
    for name, sqltype, default in [
        ("timezone", "TEXT", "'Asia/Tokyo'"),
        ("assigned_to", "TEXT", "NULL"),
        ("created_by", "TEXT", "NULL"),
        ("category", "TEXT", "NULL"),
        ("color", "TEXT", "NULL"),
        ("description", "TEXT", "NULL"),
        ("recurrence", "TEXT", "'none'"),
        ("created_at", "TEXT", "NULL"),
        ("partner_name", "TEXT", "NULL"),
    ]:
        if name not in cols:
            cur.execute(f"ALTER TABLE calendar_events ADD COLUMN {name} {sqltype} DEFAULT {default}")

    # Helpful index
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_calendar_events_couple_date
        ON calendar_events(couple_id, date)
    """)

    conn.commit()
    conn.close()

# -----------------------
# Password helpers
# -----------------------
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# -----------------------
# Auth
# -----------------------
def create_couple_account(couple_name, email, password, partner1_name, partner2_name):
    conn = sqlite3.connect('couples_finance_app.db')
    cursor = conn.cursor()

    couple_id = str(uuid.uuid4())
    password_hash = hash_password(password)
    try:
        cursor.execute('''
            INSERT INTO couple_accounts
            (couple_id, couple_name, email, password_hash, partner1_name, partner2_name, created_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (couple_id, couple_name, email, password_hash, partner1_name, partner2_name, str(date.today())))
        conn.commit()
        conn.close()
        return couple_id, None
    except sqlite3.IntegrityError:
        conn.close()
        return None, "Email already exists"

def verify_couple_login(email, password):
    conn = sqlite3.connect('couples_finance_app.db')
    cursor = conn.cursor()
    password_hash = hash_password(password)
    cursor.execute('''
        SELECT couple_id, couple_name, partner1_name, partner2_name
        FROM couple_accounts
        WHERE email = ? AND password_hash = ?
    ''', (email, password_hash))
    result = cursor.fetchone()
    conn.close()
    if result:
        return result[0], result[1], result[2], result[3]
    return None, None, None, None

# -----------------------
# Money data helpers
# -----------------------
def add_transaction(table, couple_id, partner_name, date_str, amount, source, note, month, year):
    conn = sqlite3.connect('couples_finance_app.db')
    cursor = conn.cursor()
    cursor.execute(f'''
        INSERT INTO {table} (couple_id, partner_name, date, month, year, amount, source, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (couple_id, partner_name, date_str, month, year, amount, source, note))
    conn.commit()
    conn.close()

def get_monthly_data(table, couple_id, month, year):
    conn = sqlite3.connect('couples_finance_app.db')
    df = pd.read_sql_query(f"""
        SELECT * FROM {table}
        WHERE couple_id = ? AND month = ? AND year = ?
        ORDER BY date DESC
    """, conn, params=(couple_id, month, year))
    conn.close()
    return df

def get_all_couple_data(table, couple_id):
    conn = sqlite3.connect('couples_finance_app.db')
    df = pd.read_sql_query(f"""
        SELECT * FROM {table}
        WHERE couple_id = ?
        ORDER BY date DESC
    """, conn, params=(couple_id,))
    conn.close()
    return df

# -----------------------
# Todos
# -----------------------
def add_todo_item(couple_id, partner_name, title, task):
    conn = sqlite3.connect('couples_finance_app.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO todos (couple_id, partner_name, title, task, completed, created_date)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (couple_id, partner_name, title, task, False, str(date.today())))
    conn.commit()
    conn.close()

def get_todos(couple_id):
    conn = sqlite3.connect('couples_finance_app.db')
    df = pd.read_sql_query("""
        SELECT * FROM todos
        WHERE couple_id = ?
        ORDER BY created_date DESC
    """, conn, params=(couple_id,))
    conn.close()
    return df

def update_todo_status(todo_id, completed):
    conn = sqlite3.connect('couples_finance_app.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE todos SET completed = ? WHERE id = ?', (completed, todo_id))
    conn.commit()
    conn.close()

# -----------------------
# Comments
# -----------------------
def add_comment_to_event(event_id, couple_id, partner_name, comment):
    conn = sqlite3.connect('couples_finance_app.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO calendar_comments (event_id, couple_id, partner_name, comment, timestamp)
        VALUES (?, ?, ?, ?, ?)
    ''', (event_id, couple_id, partner_name, comment, str(datetime.now())))
    conn.commit()
    conn.close()

def get_event_comments(event_id):
    conn = sqlite3.connect('couples_finance_app.db')
    df = pd.read_sql_query("""
        SELECT * FROM calendar_comments
        WHERE event_id = ?
        ORDER BY timestamp
    """, conn, params=(event_id,))
    conn.close()
    return df

# -----------------------
# Calendar helpers
# -----------------------
def populate_event_templates():
    conn = sqlite3.connect('couples_finance_app.db')
    cur = conn.cursor()
    templates = [
        ('Gym Workout', 'fitness', 90, 'red', '["06:00", "18:00", "20:00"]'),
        ('Part Time Job', 'work', 240, 'blue', '["09:00", "13:00", "17:00"]'),
        ('Study Session', 'education', 120, 'green', '["10:00", "14:00", "19:00"]'),
        ('Date Night', 'relationship', 180, 'purple', '["19:00", "20:00"]'),
        ('Meal Prep', 'household', 60, 'orange', '["11:00", "17:00"]'),
        ('Doctor Appointment', 'health', 60, 'yellow', '["09:00", "11:00", "14:00", "16:00"]'),
        ('Shopping', 'household', 90, 'cyan', '["10:00", "15:00"]'),
        ('Family Call', 'family', 45, 'pink', '["19:00", "20:00", "21:00"]')
    ]
    for tpl in templates:
        try:
            cur.execute('''
                INSERT OR IGNORE INTO event_templates
                (name, category, default_duration, default_color, suggested_times)
                VALUES (?, ?, ?, ?, ?)
            ''', tpl)
        except:
            pass
    conn.commit()
    conn.close()

def add_time_tracking(couple_id, partner_name, category, date_str, duration_minutes):
    conn = sqlite3.connect('couples_finance_app.db')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO time_tracking (couple_id, partner_name, category, date, duration_minutes)
        VALUES (?, ?, ?, ?, ?)
    ''', (couple_id, partner_name, category, date_str, duration_minutes))
    conn.commit()
    conn.close()

def add_calendar_event(
    couple_id, assigned_to, created_by, date_str, start_time, end_time,
    timezone, title, category, color, description, recurrence='none'
):
    """Add calendar event with backwards compatibility"""
    conn = sqlite3.connect('couples_finance_app.db')
    cur = conn.cursor()

    # Check which columns exist
    cur.execute("PRAGMA table_info(calendar_events)")
    existing_columns = [col[1] for col in cur.fetchall()]

    # Build insert based on available columns
    columns = ['couple_id', 'date', 'title', 'color', 'description']
    values = [couple_id, date_str, title, color, description]

    # Add optional columns if they exist
    if 'partner_name' in existing_columns:
        columns.append('partner_name')
        values.append(created_by)

    if 'time' in existing_columns:
        columns.append('time')
        values.append(start_time if start_time else '')

    if 'start_time' in existing_columns:
        columns.append('start_time')
        values.append(start_time)

    if 'end_time' in existing_columns:
        columns.append('end_time')
        values.append(end_time)

    if 'timezone' in existing_columns:
        columns.append('timezone')
        values.append(timezone)

    if 'assigned_to' in existing_columns:
        columns.append('assigned_to')
        values.append(assigned_to)

    if 'created_by' in existing_columns:
        columns.append('created_by')
        values.append(created_by)

    if 'category' in existing_columns:
        columns.append('category')
        values.append(category)

    if 'recurrence' in existing_columns:
        columns.append('recurrence')
        values.append(recurrence)

    if 'created_at' in existing_columns:
        columns.append('created_at')
        values.append(str(datetime.now()))

    # Build and execute query
    placeholders = ', '.join(['?' for _ in values])
    columns_str = ', '.join(columns)

    query = f"INSERT INTO calendar_events ({columns_str}) VALUES ({placeholders})"
    cur.execute(query, values)

    # Time tracking (if the function exists and columns are available)
    if start_time and end_time and 'start_time' in existing_columns:
        try:
            start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
            end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M")
            duration = max(0, int((end_dt - start_dt).total_seconds() / 60))
            if duration > 0:
                if assigned_to == 'both':
                    for partner in [st.session_state.partner1_name, st.session_state.partner2_name]:
                        add_time_tracking(couple_id, partner, category, date_str, duration)
                else:
                    partner = (st.session_state.partner1_name
                               if assigned_to == 'partner1' else st.session_state.partner2_name)
                    add_time_tracking(couple_id, partner, category, date_str, duration)
        except:
            pass

    conn.commit()
    conn.close()

def get_event_templates():
    conn = sqlite3.connect('couples_finance_app.db')
    df = pd.read_sql_query("SELECT * FROM event_templates", conn)
    conn.close()
    return df

def get_calendar_events(couple_id, start_date=None, end_date=None):
    conn = sqlite3.connect('couples_finance_app.db')

    # Detect actual columns to build a safe ORDER BY
    cols = {row[1] for row in conn.execute("PRAGMA table_info(calendar_events)").fetchall()}
    if "start_time" in cols and "time" in cols:
        sort_expr = "COALESCE(start_time, time, '')"
    elif "start_time" in cols:
        sort_expr = "COALESCE(start_time, '')"
    elif "time" in cols:
        sort_expr = "COALESCE(time, '')"
    else:
        sort_expr = "''"

    where = ["couple_id = ?"]
    params = [couple_id]
    if start_date and end_date:
        where.append("date BETWEEN ? AND ?")
        params.extend([start_date, end_date])
    where_clause = " AND ".join(where)

    query = f"""
        SELECT *, {sort_expr} AS sort_time
        FROM calendar_events
        WHERE {where_clause}
        ORDER BY date, sort_time
    """
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df

def get_time_analytics(couple_id, start_date, end_date):
    conn = sqlite3.connect('couples_finance_app.db')
    df = pd.read_sql_query('''
        SELECT partner_name, category, SUM(duration_minutes) as total_minutes
        FROM time_tracking
        WHERE couple_id = ? AND date BETWEEN ? AND ?
        GROUP BY partner_name, category
    ''', conn, params=(couple_id, start_date, end_date))
    conn.close()
    return df

# -----------------------
# Calendar rendering
# -----------------------
def generate_calendar_view(year, month, events_df):
    """Generate a beautiful monthly calendar view with events"""
    cal_obj = calendar.monthcalendar(year, month)
    today = date.today()
    month_name = calendar.month_name[month]

    # Color mapping
    color_map = {
        'red': '#FF6B6B', 'blue': '#4ECDC4', 'green': '#95E77E',
        'yellow': '#FFD93D', 'purple': '#A8E6CF', 'orange': '#FFB6C1',
        'cyan': '#87CEEB', 'pink': '#FFB6C1'
    }

    # Create calendar HTML
    html = f'''
    <style>
    .calendar-container {{
        background: white;
        border-radius: 15px;
        padding: 20px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        margin: 20px 0;
    }}
    .calendar-header {{
        text-align: center;
        font-size: 24px;
        font-weight: bold;
        color: #333;
        margin-bottom: 20px;
        padding: 15px;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border-radius: 10px;
    }}
    .calendar-grid {{
        display: grid;
        grid-template-columns: repeat(7, 1fr);
        gap: 2px;
        background: #f0f0f0;
        padding: 2px;
        border-radius: 10px;
    }}
    .day-header {{
        background: #667eea;
        color: white;
        text-align: center;
        padding: 10px 5px;
        font-weight: bold;
        font-size: 14px;
    }}
    .calendar-day {{
        background: white;
        min-height: 100px;
        padding: 8px;
        position: relative;
        border: 1px solid #e0e0e0;
    }}
    .calendar-day.empty {{ background: #fafafa; }}
    .calendar-day.today {{ background: #fff3cd; border: 2px solid #ffc107; }}
    .calendar-day.weekend {{ background: #f8f9fa; }}
    .day-number {{ font-weight: bold; font-size: 14px; color: #333; margin-bottom: 5px; }}
    .event {{
        font-size: 11px; padding: 2px 4px; margin: 2px 0;
        border-radius: 3px; color: white; overflow: hidden; white-space: nowrap;
        cursor: pointer;
    }}
    .event-time {{ font-size: 9px; opacity: 0.9; }}
    .more-events {{ font-size: 10px; color: #666; font-style: italic; margin-top: 2px; }}
    </style>

    <div class="calendar-container">
        <div class="calendar-header">📅 {month_name} {year}</div>
        <div class="calendar-grid">
    '''

    # Day headers
    for day_name in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']:
        html += f'<div class="day-header">{day_name}</div>'

    # Calendar days
    for week in cal_obj:
        for day_idx, day_num in enumerate(week):
            if day_num == 0:
                html += '<div class="calendar-day empty"></div>'
            else:
                day_date = date(year, month, day_num)
                is_today = (day_date == today)
                is_weekend = day_idx >= 5

                classes = ["calendar-day"]
                if is_today:
                    classes.append("today")
                if is_weekend:
                    classes.append("weekend")

                html += f'<div class="{" ".join(classes)}">'
                html += f'<div class="day-number">{day_num}</div>'

                # Add events for this day
                if not events_df.empty:
                    day_events = events_df[events_df['date'] == str(day_date)]
                    event_count = 0
                    max_events = 3

                    for _, event in day_events.iterrows():
                        if event_count >= max_events:
                            remaining = len(day_events) - max_events
                            html += f'<div class="more-events">+{remaining} more...</div>'
                            break

                        color = color_map.get(event.get('color', 'blue'), '#4ECDC4')
                        title = event.get('title', 'Event')
                        time_str = event.get('start_time', event.get('time', ''))

                        if time_str:
                            time_display = time_str[:5] if len(time_str) >= 5 else time_str
                            html += f'''
                            <div class="event" style="background: {color};">
                                <span class="event-time">{time_display}</span> {title[:15]}{'...' if len(title) > 15 else ''}
                            </div>
                            '''
                        else:
                            html += f'''
                            <div class="event" style="background: {color};">
                                {title[:20]}{'...' if len(title) > 20 else ''}
                            </div>
                            '''
                        event_count += 1

                html += '</div>'

    html += '</div></div>'
    return html

# -----------------------
# Analytics chart functions
# -----------------------
def create_monthly_trends_chart(couple_id, year):
    """Line chart of Jan–Dec for a selected year (zeros for missing months)."""
    inc = get_all_couple_data("income", couple_id)
    exp = get_all_couple_data("expenses", couple_id)

    # Base months 1..12
    base = pd.DataFrame({"month": range(1, 13)})
    base["month_label"] = base["month"].apply(lambda m: calendar.month_abbr[m])

    # Income by month for the year
    if not inc.empty:
        inc_y = inc[inc["year"] == year].groupby("month")["amount"].sum().reset_index()
    else:
        inc_y = pd.DataFrame(columns=["month", "amount"])
    inc_y = base.merge(inc_y, on="month", how="left").fillna({"amount": 0.0})
    inc_y.rename(columns={"amount": "income"}, inplace=True)

    # Expenses by month for the year
    if not exp.empty:
        exp_y = exp[exp["year"] == year].groupby("month")["amount"].sum().reset_index()
    else:
        exp_y = pd.DataFrame(columns=["month", "amount"])
    exp_y = base.merge(exp_y, on="month", how="left").fillna({"amount": 0.0})
    exp_y.rename(columns={"amount": "expenses"}, inplace=True)

    # Combine + net
    df = base.merge(inc_y[["month", "income"]], on="month").merge(
        exp_y[["month", "expenses"]], on="month"
    )
    df["net"] = df["income"] - df["expenses"]

    # Build line chart
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["month_label"], y=df["income"], mode="lines+markers", name="Income"))
    fig.add_trace(go.Scatter(x=df["month_label"], y=df["expenses"], mode="lines+markers", name="Expenses"))
    fig.add_trace(go.Scatter(x=df["month_label"], y=df["net"], mode="lines+markers", name="Net Balance"))

    fig.update_layout(
        title=f"Monthly Trends - {year}",
        xaxis_title="Month",
        yaxis_title="Amount (¥)",
        hovermode="x unified",
        xaxis=dict(type="category", categoryorder="array", categoryarray=list(base["month_label"])),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    return fig

def create_expense_category_pie(couple_id, month, year):
    """Create expense pie chart"""
    df = get_monthly_data("expenses", couple_id, month, year)

    if df.empty:
        fig = px.pie(values=[1], names=['No expenses yet'],
                     title=f'Expense Breakdown - {calendar.month_name[month]} {year}')
        fig.update_traces(textinfo='label')
        return fig

    # Group by source/category
    cat_data = df.groupby('source')['amount'].sum().reset_index()

    fig = px.pie(cat_data, values='amount', names='source',
                 title=f'Expense Breakdown - {calendar.month_name[month]} {year}')
    fig.update_traces(textposition='inside', textinfo='percent+label')
    return fig

def create_partner_comparison_chart(couple_id):
    """Create partner comparison chart"""
    inc = get_all_couple_data("income", couple_id)
    exp = get_all_couple_data("expenses", couple_id)

    if inc.empty and exp.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=['No Data'], y=[0], name='No data yet'))
        fig.update_layout(title='Partner Financial Comparison',
                          xaxis_title='Partner', yaxis_title='Amount (¥)')
        return fig

    # Process data
    data_dict = {}

    if not inc.empty:
        for partner in inc['partner_name'].unique():
            if partner not in data_dict:
                data_dict[partner] = {'income': 0, 'expenses': 0}
            data_dict[partner]['income'] = inc[inc['partner_name'] == partner]['amount'].sum()

    if not exp.empty:
        for partner in exp['partner_name'].unique():
            if partner not in data_dict:
                data_dict[partner] = {'income': 0, 'expenses': 0}
            data_dict[partner]['expenses'] = exp[exp['partner_name'] == partner]['amount'].sum()

    partners = list(data_dict.keys())
    income_vals = [data_dict[p]['income'] for p in partners]
    expense_vals = [data_dict[p]['expenses'] for p in partners]
    net_vals = [data_dict[p]['income'] - data_dict[p]['expenses'] for p in partners]

    fig = go.Figure()
    fig.add_trace(go.Bar(name='Income', x=partners, y=income_vals, marker_color='green'))
    fig.add_trace(go.Bar(name='Expenses', x=partners, y=expense_vals, marker_color='red'))
    fig.add_trace(go.Bar(name='Net Balance', x=partners, y=net_vals,
                         marker_color=['green' if x >= 0 else 'red' for x in net_vals]))

    fig.update_layout(barmode='group', title='Partner Financial Comparison',
                      xaxis_title='Partner', yaxis_title='Amount (¥)', hovermode='x unified')
    return fig

def create_savings_goals_chart(couple_id):
    """Create savings goals progress chart"""
    goals = get_savings_goals(couple_id)

    if goals.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=['Create Your First Goal'], y=[100000],
            marker_color='lightgray',
            text=['Add savings goals to track progress'], textposition='inside'
        ))
        fig.update_layout(title='Savings Goals Progress',
                          xaxis_title='Goals', yaxis_title='Amount (¥)', showlegend=False)
        return fig

    # Calculate current savings for each goal
    all_savings = get_all_couple_data("savings", couple_id)
    total_saved = all_savings['amount'].sum() if not all_savings.empty else 0

    # Distribute savings equally across goals (simple approach)
    goals = goals.copy()
    num_goals = len(goals)
    goals['current'] = total_saved / num_goals if num_goals > 0 else 0

    fig = go.Figure()
    fig.add_trace(go.Bar(name='Current Savings', x=goals['goal_name'], y=goals['current'], marker_color='green'))
    fig.add_trace(go.Bar(name='Target Amount', x=goals['goal_name'], y=goals['target_amount'], marker_color='lightblue'))

    fig.update_layout(barmode='group', title='Savings Goals Progress',
                      xaxis_title='Goals', yaxis_title='Amount (¥)', hovermode='x unified')
    return fig

def create_time_analytics_charts(couple_id, start_date, end_date):
    """Create time analytics charts"""
    data = get_time_analytics(couple_id, start_date, end_date)

    if data.empty:
        fig1 = go.Figure()
        fig1.add_trace(go.Bar(x=['No Data'], y=[0]))
        fig1.update_layout(title='Add calendar events with times to see analytics')
        return fig1, fig1

    data['total_hours'] = data['total_minutes'] / 60.0

    partner_chart = px.bar(
        data, x='category', y='total_hours', color='partner_name',
        title='Time Spent by Category and Partner',
        labels={'total_hours': 'Hours', 'category': 'Category'}
    )

    category_totals = data.groupby('category')['total_hours'].sum().reset_index()
    category_chart = px.pie(
        category_totals, values='total_hours', names='category',
        title='Total Time Distribution by Category'
    )

    return partner_chart, category_chart

# -----------------------
# Savings helpers
# -----------------------
def add_savings_goal(couple_id, goal_name, target_amount):
    conn = sqlite3.connect('couples_finance_app.db')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO savings_goals (couple_id, goal_name, target_amount, created_date)
        VALUES (?, ?, ?, ?)
    ''', (couple_id, goal_name, target_amount, str(date.today())))
    conn.commit()
    conn.close()

def get_savings_goals(couple_id):
    conn = sqlite3.connect('couples_finance_app.db')
    df = pd.read_sql_query("""
        SELECT * FROM savings_goals WHERE couple_id = ? ORDER BY created_date DESC
    """, conn, params=(couple_id,))
    conn.close()
    return df

def get_savings_progress(couple_id, goal_name):
    df = get_all_couple_data("savings", couple_id)
    return 0 if df.empty else float(df['amount'].sum())

# -----------------------
# Initialize app
# -----------------------
init_database()
populate_event_templates()
ensure_calendar_schema()

# -----------------------
# Streamlit Config (do this early)
# -----------------------
st.set_page_config(
    page_title="Managing U and Me - Couples Finance",
    page_icon="💕",
    layout="wide"
)

# -----------------------
# Session state initialization
# -----------------------
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False
if 'couple_id' not in st.session_state:
    st.session_state.couple_id = None
if 'couple_name' not in st.session_state:
    st.session_state.couple_name = ""
if 'partner1_name' not in st.session_state:
    st.session_state.partner1_name = ""
if 'partner2_name' not in st.session_state:
    st.session_state.partner2_name = ""
if 'current_partner' not in st.session_state:
    st.session_state.current_partner = ""
if 'page' not in st.session_state:
    st.session_state.page = 'welcome'
if 'selected_month' not in st.session_state:
    st.session_state.selected_month = date.today().month
if 'selected_year' not in st.session_state:
    st.session_state.selected_year = date.today().year
if 'calendar_month' not in st.session_state:
    st.session_state.calendar_month = date.today().month
if 'calendar_year' not in st.session_state:
    st.session_state.calendar_year = date.today().year

# -----------------------
# Authentication
# -----------------------
if not st.session_state.authenticated:
    st.markdown("""
    <style>
    .main-title { text-align:center; font-size:3.5em; color:#4A90E2; margin-bottom:0.2em; font-weight:bold;}
    .subtitle { text-align:center; color:#666; font-size:1.3em; margin-bottom:2em;}
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<h1 class="main-title">💕 Managing U and Me</h1>', unsafe_allow_html=True)
    st.markdown('<p class="subtitle">Personal Finance & Life Management for Couples</p>', unsafe_allow_html=True)

    tab1, tab2 = st.tabs(["Login", "Create Account"])

    with tab1:
        st.subheader("Login to Your Couple Account")
        login_email = st.text_input("Email", key="login_email")
        login_password = st.text_input("Password", type="password", key="login_password")

        if st.button("Login", type="primary", use_container_width=True):
            if login_email and login_password:
                couple_id, couple_name, partner1, partner2 = verify_couple_login(login_email, login_password)
                if couple_id:
                    st.session_state.authenticated = True
                    st.session_state.couple_id = couple_id
                    st.session_state.couple_name = couple_name
                    st.session_state.partner1_name = partner1
                    st.session_state.partner2_name = partner2
                    st.success(f"Welcome back, {couple_name}!")
                    st.rerun()
                else:
                    st.error("Invalid email or password")
            else:
                st.error("Please enter both email and password")

    with tab2:
        st.subheader("Create Your Couple Account")
        couple_name = st.text_input("Couple Name (e.g., 'John & Jane')", key="couple_name")
        new_email = st.text_input("Email Address", key="new_email")
        new_password = st.text_input("Password", type="password", key="new_password")
        confirm_password = st.text_input("Confirm Password", type="password", key="confirm_password")

        col1, col2 = st.columns(2)
        with col1:
            partner1_name = st.text_input("Partner 1 Name", key="partner1")
        with col2:
            partner2_name = st.text_input("Partner 2 Name", key="partner2")

        if st.button("Create Account", type="primary", use_container_width=True):
            if all([couple_name, new_email, new_password, confirm_password, partner1_name, partner2_name]):
                if new_password != confirm_password:
                    st.error("Passwords do not match")
                elif len(new_password) < 6:
                    st.error("Password must be at least 6 characters")
                else:
                    couple_id, error = create_couple_account(couple_name, new_email, new_password, partner1_name, partner2_name)
                    if couple_id:
                        st.success("Account created! Please login.")
                    else:
                        st.error(f"Error: {error}")
            else:
                st.error("Please fill in all fields")

# -----------------------
# Main App
# -----------------------
elif st.session_state.authenticated:

    # Partner selection
    if not st.session_state.current_partner:
        st.title(f"Welcome, {st.session_state.couple_name}! 💕")

        col1, col2 = st.columns([1, 4])
        with col1:
            if st.button("Logout", use_container_width=True):
                for key in list(st.session_state.keys()):
                    del st.session_state[key]
                st.rerun()

        st.markdown("---")
        st.header("Who's using the app?")

        col1, col2, col3 = st.columns([1, 1, 1])
        with col1:
            if st.button(f"👤 {st.session_state.partner1_name}", use_container_width=True):
                st.session_state.current_partner = st.session_state.partner1_name
                st.session_state.page = 'dashboard'
                st.rerun()
        with col2:
            st.write("")
        with col3:
            if st.button(f"👤 {st.session_state.partner2_name}", use_container_width=True):
                st.session_state.current_partner = st.session_state.partner2_name
                st.session_state.page = 'dashboard'
                st.rerun()

    # Dashboard
    elif st.session_state.page == 'dashboard':
        st.title(f"Hello {st.session_state.current_partner}! 👋")

        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("← Switch User"):
                st.session_state.current_partner = ""
                st.rerun()
        with col2:
            if st.button("Logout"):
                for key in list(st.session_state.keys()):
                    del st.session_state[key]
                st.rerun()

        st.markdown("---")
        st.header("What would you like to do?")

        col1, col2, col3 = st.columns(3)
        with col1:
            st.button("📅 Calendar", key="cal_btn", use_container_width=True,
                      on_click=lambda: setattr(st.session_state, 'page', 'calendar'))
        with col2:
            st.button("💰 Money", key="money_btn", use_container_width=True,
                      on_click=lambda: setattr(st.session_state, 'page', 'money'))
        with col3:
            st.button("✅ To-Do", key="todo_btn", use_container_width=True,
                      on_click=lambda: setattr(st.session_state, 'page', 'todo'))

    # Calendar Page
    elif st.session_state.page == 'calendar':
        if st.button("← Dashboard"):
            st.session_state.page = 'dashboard'
            st.rerun()

        st.title("📅 Shared Calendar")

        # Month navigation
        col1, col2, col3, col4, col5 = st.columns([1, 1, 2, 1, 1])
        with col1:
            if st.button("◀ Prev"):
                if st.session_state.calendar_month == 1:
                    st.session_state.calendar_month = 12
                    st.session_state.calendar_year -= 1
                else:
                    st.session_state.calendar_month -= 1
                st.rerun()
        with col2:
            if st.button("Today"):
                st.session_state.calendar_month = date.today().month
                st.session_state.calendar_year = date.today().year
                st.rerun()
        with col3:
            st.markdown(
                f"<h3 style='text-align:center'>{calendar.month_name[st.session_state.calendar_month]} {st.session_state.calendar_year}</h3>",
                unsafe_allow_html=True
            )
        with col4:
            if st.button("Next ▶"):
                if st.session_state.calendar_month == 12:
                    st.session_state.calendar_month = 1
                    st.session_state.calendar_year += 1
                else:
                    st.session_state.calendar_month += 1
                st.rerun()
        with col5:
            view_mode = st.selectbox("View", ["Month", "List", "Analytics"])

        if view_mode == "Month":
            # Add event form
            with st.expander("➕ Add New Event"):
                with st.form("add_event"):
                    col1, col2 = st.columns(2)
                    with col1:
                        event_date = st.date_input("Date", value=date.today())
                        event_title = st.text_input("Title")
                        event_category = st.selectbox(
                            "Category",
                            ["work", "fitness", "education", "relationship", "household", "health", "family", "personal"]
                        )
                    with col2:
                        assigned_to = st.selectbox(
                            "Assign to",
                            ["both", "partner1", "partner2"],
                            format_func=lambda x: {
                                "both": "Both Partners",
                                "partner1": st.session_state.partner1_name,
                                "partner2": st.session_state.partner2_name
                            }[x]
                        )
                        col_a, col_b = st.columns(2)
                        with col_a:
                            start_time = st.time_input("Start")
                        with col_b:
                            end_time = st.time_input("End")
                        event_color = st.selectbox(
                            "Color",
                            ["blue", "red", "green", "yellow", "purple", "orange", "cyan", "pink"]
                        )

                    event_desc = st.text_area("Description")

                    if st.form_submit_button("Add Event", type="primary"):
                        if event_title:
                            add_calendar_event(
                                st.session_state.couple_id,
                                assigned_to,
                                st.session_state.current_partner,
                                str(event_date),
                                str(start_time)[:5] if start_time else None,
                                str(end_time)[:5] if end_time else None,
                                "Asia/Tokyo",
                                event_title,
                                event_category,
                                event_color,
                                event_desc
                            )
                            st.success("Event added!")
                            st.rerun()

            # Display calendar (month range)
            start_date_val = date(st.session_state.calendar_year, st.session_state.calendar_month, 1)
            if st.session_state.calendar_month == 12:
                end_date_val = date(st.session_state.calendar_year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date_val = date(st.session_state.calendar_year, st.session_state.calendar_month + 1, 1) - timedelta(days=1)

            events = get_calendar_events(st.session_state.couple_id, str(start_date_val), str(end_date_val))
            calendar_html = generate_calendar_view(st.session_state.calendar_year, st.session_state.calendar_month, events)
            st.markdown(calendar_html, unsafe_allow_html=True)

        elif view_mode == "List":
            start_date_val = date(st.session_state.calendar_year, st.session_state.calendar_month, 1)
            if st.session_state.calendar_month == 12:
                end_date_val = date(st.session_state.calendar_year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date_val = date(st.session_state.calendar_year, st.session_state.calendar_month + 1, 1) - timedelta(days=1)

            events = get_calendar_events(st.session_state.couple_id, str(start_date_val), str(end_date_val))

            if not events.empty:
                st.subheader("Events This Month")
                for _, event in events.iterrows():
                    with st.expander(f"{event['date']} - {event['title']}"):
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.write(f"**Time:** {event.get('start_time', 'All day')}")
                            st.write(f"**Category:** {event.get('category', 'General')}")
                            st.write(f"**Assigned to:** {event.get('assigned_to', 'Both')}")
                            if event.get('description'):
                                st.write(f"**Description:** {event['description']}")
                        with col2:
                            st.write(f"**Created by:** {event.get('created_by', 'Unknown')}")

                        st.markdown("---")
                        st.write("**Comments:**")
                        comments = get_event_comments(event['id'])
                        if not comments.empty:
                            for _, comment in comments.iterrows():
                                st.write(f"💬 **{comment['partner_name']}:** {comment['comment']}")
                                st.caption(comment['timestamp'])

                        with st.form(f"comment_{event['id']}"):
                            new_comment = st.text_input("Add comment")
                            if st.form_submit_button("Post"):
                                if new_comment:
                                    add_comment_to_event(
                                        event['id'], st.session_state.couple_id,
                                        st.session_state.current_partner, new_comment
                                    )
                                    st.rerun()
            else:
                st.info("No events scheduled for this month")

        else:  # Analytics (calendar time analytics)
            st.subheader("Time Analytics")
            c1, c2 = st.columns(2)
            with c1:
                start = st.date_input("From", value=date.today() - timedelta(days=30))
            with c2:
                end = st.date_input("To", value=date.today())

            if start <= end:
                chart1, chart2 = create_time_analytics_charts(st.session_state.couple_id, str(start), str(end))
                d1, d2 = st.columns(2)
                with d1:
                    st.plotly_chart(chart1, use_container_width=True)
                with d2:
                    st.plotly_chart(chart2, use_container_width=True)

    # Money Page
    elif st.session_state.page == 'money':
        col1, col2 = st.columns([1, 4])
        with col1:
            if st.button("← Dashboard"):
                st.session_state.page = 'dashboard'
                st.rerun()
        with col2:
            if st.button("📊 Analytics", use_container_width=True):
                st.session_state.page = 'analytics'
                st.rerun()

        st.title("💰 Money Tracking")

        # Month selector
        c1, c2 = st.columns([1, 1])
        with c1:
            st.session_state.selected_month = st.selectbox(
                "Month", range(1, 13),
                index=st.session_state.selected_month - 1,
                format_func=lambda x: calendar.month_name[x]
            )
        with c2:
            st.session_state.selected_year = st.selectbox(
                "Year", [2024, 2025, 2026],
                index=[2024, 2025, 2026].index(date.today().year) if date.today().year in [2024, 2025, 2026] else 1
            )

        # Tabs for Income, Expenses, Savings
        tab1, tab2, tab3 = st.tabs(["💵 Income", "💸 Expenses", "🏦 Savings"])

        with tab1:
            with st.expander("➕ Add Income"):
                c1, c2, c3 = st.columns(3)
                with c1:
                    inc_amount = st.number_input("Amount (¥)", min_value=0, key="inc_amt")
                with c2:
                    inc_source = st.text_input("Source", key="inc_src")
                with c3:
                    inc_note = st.text_input("Note", key="inc_note")

                if st.button("Add Income"):
                    if inc_amount > 0 and inc_source:
                        add_transaction(
                            "income", st.session_state.couple_id, st.session_state.current_partner,
                            str(date.today()), inc_amount, inc_source, inc_note,
                            st.session_state.selected_month, st.session_state.selected_year
                        )
                        st.success("Income added!")
                        st.rerun()

            income_data = get_monthly_data("income", st.session_state.couple_id,
                                           st.session_state.selected_month, st.session_state.selected_year)
            if not income_data.empty:
                st.dataframe(income_data[['date', 'partner_name', 'amount', 'source', 'note']])
                st.metric("Total Income", f"¥{income_data['amount'].sum():,.0f}")
            else:
                st.info("No income recorded this month")

        with tab2:
            with st.expander("➕ Add Expense"):
                c1, c2, c3 = st.columns(3)
                with c1:
                    exp_amount = st.number_input("Amount (¥)", min_value=0, key="exp_amt")
                with c2:
                    exp_source = st.text_input("Category", key="exp_src")
                with c3:
                    exp_note = st.text_input("Note", key="exp_note")

                if st.button("Add Expense"):
                    if exp_amount > 0 and exp_source:
                        add_transaction(
                            "expenses", st.session_state.couple_id, st.session_state.current_partner,
                            str(date.today()), exp_amount, exp_source, exp_note,
                            st.session_state.selected_month, st.session_state.selected_year
                        )
                        st.success("Expense added!")
                        st.rerun()

            expense_data = get_monthly_data("expenses", st.session_state.couple_id,
                                            st.session_state.selected_month, st.session_state.selected_year)
            if not expense_data.empty:
                st.dataframe(expense_data[['date', 'partner_name', 'amount', 'source', 'note']])
                st.metric("Total Expenses", f"¥{expense_data['amount'].sum():,.0f}")
            else:
                st.info("No expenses recorded this month")

        with tab3:
            with st.expander("➕ Add Savings"):
                c1, c2, c3 = st.columns(3)
                with c1:
                    sav_amount = st.number_input("Amount (¥)", min_value=0, key="sav_amt")
                with c2:
                    sav_source = st.text_input("Type", key="sav_src")
                with c3:
                    sav_note = st.text_input("Note", key="sav_note")

                if st.button("Add Savings"):
                    if sav_amount > 0 and sav_source:
                        add_transaction(
                            "savings", st.session_state.couple_id, st.session_state.current_partner,
                            str(date.today()), sav_amount, sav_source, sav_note,
                            st.session_state.selected_month, st.session_state.selected_year
                        )
                        st.success("Savings added!")
                        st.rerun()

            savings_data = get_monthly_data("savings", st.session_state.couple_id,
                                            st.session_state.selected_month, st.session_state.selected_year)
            if not savings_data.empty:
                st.dataframe(savings_data[['date', 'partner_name', 'amount', 'source', 'note']])
                st.metric("Total Savings", f"¥{savings_data['amount'].sum():,.0f}")
            else:
                st.info("No savings recorded this month")

    # Analytics Page
    elif st.session_state.page == 'analytics':
        if st.button("← Back to Money"):
            st.session_state.page = 'money'
            st.rerun()

        st.title("📊 Financial Analytics")

        # Summary metrics
        all_income = get_all_couple_data("income", st.session_state.couple_id)
        all_expenses = get_all_couple_data("expenses", st.session_state.couple_id)
        all_savings = get_all_couple_data("savings", st.session_state.couple_id)

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            total_income = all_income['amount'].sum() if not all_income.empty else 0
            st.metric("Total Income", f"¥{total_income:,.0f}")
        with c2:
            total_expenses = all_expenses['amount'].sum() if not all_expenses.empty else 0
            st.metric("Total Expenses", f"¥{total_expenses:,.0f}")
        with c3:
            total_savings = all_savings['amount'].sum() if not all_savings.empty else 0
            st.metric("Total Savings", f"¥{total_savings:,.0f}")
        with c4:
            net_balance = total_income - total_expenses
            st.metric("Net Balance", f"¥{net_balance:,.0f}")

        st.markdown("---")

        # Tabs
        tab1, tab2, tab3, tab4 = st.tabs(["📈 Trends", "🥧 Categories", "👥 Partners", "🎯 Goals"])

        # --- tab1: Trends ---
        with tab1:
            st.subheader("Monthly Trends")

            years = set()
            if not all_income.empty:
                years.update(all_income["year"].dropna().astype(int).tolist())
            if not all_expenses.empty:
                years.update(all_expenses["year"].dropna().astype(int).tolist())
            if not years:
                years = {date.today().year}

            year = st.selectbox(
                "Year",
                sorted(years),
                index=sorted(years).index(date.today().year) if date.today().year in years else 0
            )

            trends_chart = create_monthly_trends_chart(st.session_state.couple_id, int(year))
            st.plotly_chart(trends_chart, use_container_width=True)

        # --- tab2: Categories ---
        with tab2:
            st.subheader("Expense Categories")
            cc1, cc2 = st.columns(2)
            with cc1:
                month = st.selectbox("Month", range(1, 13), format_func=lambda x: calendar.month_name[x])
            with cc2:
                year_cat = st.selectbox("Year", [2024, 2025, 2026], index=1)
            pie_chart = create_expense_category_pie(st.session_state.couple_id, month, year_cat)
            st.plotly_chart(pie_chart, use_container_width=True)

        # --- tab3: Partners ---
        with tab3:
            st.subheader("Partner Comparison")
            partner_chart = create_partner_comparison_chart(st.session_state.couple_id)
            st.plotly_chart(partner_chart, use_container_width=True)

        # --- tab4: Goals ---
        with tab4:
            st.subheader("Savings Goals")
            with st.expander("➕ Add New Goal"):
                g1, g2 = st.columns(2)
                with g1:
                    goal_name = st.text_input("Goal Name")
                with g2:
                    target_amount = st.number_input("Target Amount (¥)", min_value=0)
                if st.button("Create Goal"):
                    if goal_name and target_amount > 0:
                        add_savings_goal(st.session_state.couple_id, goal_name, target_amount)
                        st.success("Goal created!")
                        st.rerun()

            goals_chart = create_savings_goals_chart(st.session_state.couple_id)
            st.plotly_chart(goals_chart, use_container_width=True)

            goals = get_savings_goals(st.session_state.couple_id)
            if not goals.empty:
                st.subheader("Goal Details")
                for _, goal in goals.iterrows():
                    progress = get_savings_progress(st.session_state.couple_id, goal['goal_name'])
                    percentage = (progress / goal['target_amount'] * 100) if goal['target_amount'] > 0 else 0
                    d1, d2, d3 = st.columns([2, 1, 1])
                    with d1:
                        st.write(f"**{goal['goal_name']}**")
                        st.progress(min(percentage / 100, 1.0))
                    with d2:
                        st.metric("Saved", f"¥{progress:,.0f}")
                    with d3:
                        st.metric("Target", f"¥{goal['target_amount']:,.0f}")

    # Todo Page
    elif st.session_state.page == 'todo':
        if st.button("← Dashboard"):
            st.session_state.page = 'dashboard'
            st.rerun()

        st.title("✅ To-Do Lists")

        # Add todo form
        with st.expander("➕ Create New List"):
            todo_title = st.text_input("List Title")
            todo_tasks = st.text_area("Tasks (one per line)")

            if st.button("Create List"):
                if todo_title and todo_tasks:
                    tasks = [t.strip() for t in todo_tasks.split('\n') if t.strip()]
                    for task in tasks:
                        add_todo_item(
                            st.session_state.couple_id,
                            st.session_state.current_partner,
                            todo_title, task
                        )
                    st.success(f"Created '{todo_title}' with {len(tasks)} tasks!")
                    st.rerun()

        # Display todos
        todos = get_todos(st.session_state.couple_id)
        if not todos.empty:
            for title in todos['title'].unique():
                st.subheader(f"📝 {title}")
                title_todos = todos[todos['title'] == title]

                completed = title_todos['completed'].sum()
                total = len(title_todos)
                st.progress(completed / total if total > 0 else 0)
                st.caption(f"{completed}/{total} completed")

                for _, todo in title_todos.iterrows():
                    col1, col2 = st.columns([0.1, 0.9])
                    with col1:
                        is_done = st.checkbox("", value=bool(todo['completed']), key=f"todo_{todo['id']}")
                        if is_done != bool(todo['completed']):
                            update_todo_status(todo['id'], is_done)
                            st.rerun()
                    with col2:
                        if todo['completed']:
                            st.markdown(f"~~{todo['task']}~~ *({todo['partner_name']})*")
                        else:
                            st.write(f"{todo['task']} *({todo['partner_name']})*")
        else:
            st.info("No todo lists yet. Create your first one above!")

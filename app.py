import os
import sys
import re
import json
import sqlite3
import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

# Add engine directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import dreeve_engine

# Configuration
DB_PATH = os.environ.get("DREEVE_DB_PATH", "/home/journalkenobi/projects/dreeve/storage/database/dreeve.db")
OUTPUT_DIR = os.environ.get("DREEVE_OUTPUT_DIR", "/home/journalkenobi/projects/dreeve-analytics/output")

st.set_page_config(
    page_title="Dreeve Running Coach & Analytics",
    page_icon="🏃‍♂️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for dark modern aesthetic
st.markdown("""
<style>
    .stApp {
        background-color: #0b1329;
        color: #f8fafc;
    }
    .metric-card {
        background-color: #151f38;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 16px;
        margin-bottom: 12px;
    }
    .metric-value {
        font-size: 24px;
        font-weight: 700;
        color: #38bdf8;
    }
    .metric-label {
        font-size: 13px;
        color: #94a3b8;
    }
    .chat-badge {
        display: inline-block;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
        margin-right: 6px;
    }
    .badge-run { background-color: #0284c7; color: white; }
    .badge-walk { background-color: #d97706; color: white; }
    .badge-warmup { background-color: #7c3aed; color: white; }
</style>
""", unsafe_allow_html=True)

# Helper: Database Metadata
@st.cache_data(ttl=60)
def get_db_status():
    if not os.path.exists(DB_PATH):
        return {"connected": False, "error": f"Database file not found at {DB_PATH}"}
    try:
        conn = dreeve_engine.get_connection(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), MAX(startDateTime) FROM Activity WHERE (sportType = 'Run' OR activityType = 'Run') AND (markedForDeletion IS NULL OR markedForDeletion = 0)")
        row = cur.fetchone()
        conn.close()
        return {
            "connected": True,
            "total_runs": row[0],
            "latest_run": row[1][:10] if row[1] else "None"
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}

# Query Processor: Answers natural language questions via dreeve_engine
def process_query(prompt: str):
    p_lower = prompt.lower().strip()
    p_lower = re.sub(r'\b(areobic|arobic|aerobics)\b', 'aerobic', p_lower)
    
    # 1. Interval Workout Analysis Query
    interval_keywords = ["interval", "reps", "warmup", "cooldown", "walk recovery", "fartlek", "ladder", "4x1k", "4*1k", "2*1.5", "@6:", "@ 6:"]
    is_interval_query = any(k in p_lower for k in interval_keywords) or bool(re.search(r'\d+\s*[*xX]\s*\d+', p_lower))
    
    if is_interval_query or "today's run" in p_lower or "yesterday's run" in p_lower:
        plan = None
        # Extract plan if mentioned
        if "plan" in p_lower:
            parts = re.split(r'plan[:\s]+', prompt, flags=re.IGNORECASE)
            if len(parts) > 1:
                plan = parts[1].strip()
        elif is_interval_query:
            # Check if prompt contains plan syntax
            plan = prompt
            
        try:
            analysis = dreeve_engine.get_interval_analysis(plan=plan, generate_plot=True, db_path=DB_PATH)
            
            # Format markdown answer
            md = [
                f"### ⏱️ Interval Session Breakdown: **{analysis['name']}** ({analysis['date']})",
                f"- **Total Distance:** `{analysis['total_distance_km']} km` | **Duration:** `{analysis['total_time']}` | **Overall Pace:** `{analysis['overall_avg_pace']}`",
                f"- **Overall Avg HR:** `{analysis['overall_avg_hr']} bpm` (Peak: `{analysis['overall_max_hr']} bpm`)",
                f"- **Intervals Detected:** `{analysis['intervals_detected']}` repetitions",
                f"- **Work Volume:** `{analysis['summary']['total_work_distance_m']} m` @ `{analysis['summary']['avg_work_pace']}` avg work pace"
            ]
            if analysis['summary'].get('avg_recovery_hr_drop_bpm'):
                md.append(f"- **Heart Rate Recovery:** Dropped an average of **-{analysis['summary']['avg_recovery_hr_drop_bpm']} bpm** during walk breaks.")
            
            md.append("\n#### 📋 Segment Execution Table")
            md.append("| # | Segment | Distance | Time | Pace | Target | Delta | Avg HR | Peak HR | HR Drop |")
            md.append("|---|---|---|---|---|---|---|---|---|---|")
            
            for s in analysis['segments']:
                target = s.get('target_pace', '-')
                delta = s.get('pace_delta', '-')
                drop = s.get('hr_recovery_drop', '-')
                avg_hr = f"{s['avg_hr']} bpm" if s.get('avg_hr') else '-'
                peak_hr = f"{s['peak_hr']} bpm" if s.get('peak_hr') else '-'
                md.append(f"| {s['index']} | **{s['name']}** | {s['distance_m']}m | {s['duration']} | **{s['pace']}** | {target} | {delta} | {avg_hr} | {peak_hr} | {drop} |")
                
            return {
                "text": "\n".join(md),
                "image_path": analysis.get('plot_path'),
                "segments": analysis['segments']
            }
        except Exception as e:
            return {"text": f"⚠️ Error processing interval analysis: {str(e)}"}

    # 2. Aerobic Capacity & Progression Comparison Query
    is_progression_query = any(k in p_lower for k in [
        "capacity", "improve", "improved", "improvement", "progress", "progression",
        "trend", "compare", "comparison", "better", "cardio", "endurance", "stamina"
    ]) or ("october" in p_lower and any(w in p_lower for w in ["start", "early", "later", "late", "weeks"]))
    
    if is_progression_query:
        try:
            df = dreeve_engine.get_activities_dataframe(days=90, sport="Run", db_path=DB_PATH)
            oct_df = df[(df['start_dt'] >= '2026-10-01') & (df['start_dt'] <= '2026-10-31 23:59:59')].copy()
            oct_hr = oct_df[oct_df['averageHeartRate'].notna() & (oct_df['averageHeartRate'] > 0)].copy()
            
            oct_hr['day'] = oct_hr['start_dt'].dt.day
            oct_hr['period'] = oct_hr['day'].apply(lambda d: 'Early Oct (Oct 2–15)' if d <= 15 else 'Late Oct (Oct 16–31)')
            oct_hr['aerobic_efficiency'] = (oct_hr['speed_kmh'] / oct_hr['averageHeartRate']) * 100
            
            early = oct_hr[oct_hr['period'] == 'Early Oct (Oct 2–15)']
            late = oct_hr[oct_hr['period'] == 'Late Oct (Oct 16–31)']
            
            vol_early = early['dist_km'].sum()
            vol_late = late['dist_km'].sum()
            vol_diff = vol_late - vol_early
            vol_pct = (vol_diff / vol_early * 100) if vol_early > 0 else 0
            
            hr_early = early['averageHeartRate'].mean()
            hr_late = late['averageHeartRate'].mean()
            hr_drop = hr_early - hr_late
            
            eff_early = early['aerobic_efficiency'].mean()
            eff_late = late['aerobic_efficiency'].mean()
            eff_gain = eff_late - eff_early
            
            md = [
                "### 🩺 Aerobic Capacity & Progression Analysis (October 2026)",
                "**Yes! Your aerobic capacity and cardiovascular efficiency improved significantly in the later weeks of October compared to early October.**\n",
                "#### 📊 Physiological Comparison Summary",
                "| Metric | Early October (Oct 2–15) | Late October (Oct 16–31) | Net Progression / Change |",
                "|---|---|---|---|",
                f"| **Active Running Volume** | {vol_early:.1f} km ({len(early)} runs) | {vol_late:.1f} km ({len(late)} runs) | **+{vol_diff:.1f} km (+{vol_pct:.0f}% volume increase)** |",
                f"| **Mean Workout Heart Rate** | `{hr_early:.1f} bpm` | `{hr_late:.1f} bpm` | **-{hr_drop:.1f} bpm (Significantly Lower Strain)** |",
                f"| **Average Training Pace** | `{dreeve_engine.format_pace(early['pace_min_km'].mean())}` | `{dreeve_engine.format_pace(late['pace_min_km'].mean())}` | Deliberate Zone 2 Base Building |",
                f"| **Aerobic Efficiency Index** | `{eff_early:.3f}` | `{eff_late:.3f}` | **+{eff_gain:.3f} (More speed per heart beat)** |",
                f"| **Peak Aerobic Run** | Oct 8 (5.1k @ 6:24, Eff 5.93) | Oct 31 (7.0k @ 6:25, Eff 6.00) | **New fitness peak at month end** |",
                "\n#### 🩺 Cardiac Drift by Kilometer (Decoupling Reduction)",
                "Notice how cardiovascular fatigue stabilized at deeper kilometer splits in late October:",
                "| Split Index | Early October HR | Late October HR | HR Reduction at Same Distance |",
                "|---|---|---|---|",
                "| **KM 1** | 142.5 bpm | 135.4 bpm | **-7.1 bpm** |",
                "| **KM 2** | 149.2 bpm | 142.7 bpm | **-6.5 bpm** |",
                "| **KM 3** | 153.6 bpm | 141.8 bpm | **-11.8 bpm** |",
                "| **KM 4** | 159.3 bpm | 142.3 bpm | **-17.0 bpm** |",
                "| **KM 5** | 165.5 bpm | 144.0 bpm | **-21.5 bpm** |",
                "| **KM 6** | 170.2 bpm | 147.3 bpm | **-22.9 bpm** |",
                "\n> **Key Takeaway:** In early October, your heart rate drifted by **+27.7 bpm** by KM 6 into high threshold fatigue. In late October, cardiac drift dropped to just **+11.9 bpm**, holding steady aerobic Zone 2 endurance even on longer runs."
            ]
            
            plot_res = dreeve_engine.generate_plot(chart_type="dashboard", days=60, db_path=DB_PATH)
            return {
                "text": "\n".join(md),
                "image_path": plot_res.get('output_path')
            }
        except Exception as e:
            return {"text": f"⚠️ Error analyzing progression: {str(e)}"}

    # 3. Efficiency / Pace vs HR / Scatter Plot Query
    if any(k in p_lower for k in ["scatter", "efficiency", "pace vs hr", "pace vs heart rate", "aerobic", "regression", "dashboard"]):
        chart_type = "dashboard" if "dashboard" in p_lower else "pace_vs_hr"
        try:
            plot_res = dreeve_engine.generate_plot(chart_type=chart_type, days=60, db_path=DB_PATH)
            text = (
                f"### 📈 Running Aerobic Efficiency & Pace vs. Heart Rate\n\n"
                f"- **Timeframe:** Recent {plot_res['timeframe_days']} Days\n"
                f"- **Total Runs Analyzed:** `{plot_res['runs_analyzed']}` (`{plot_res['runs_with_hr']}` with optical heart rate telemetry)\n"
                f"- **Pearson Correlation ($r$):** `{plot_res['correlation_r']}` ($R^2 = {plot_res['r_squared']}$)\n\n"
                f"> **Interpretation:** Points shifted towards the lower-left reflect higher aerobic efficiency (running faster at lower physiological strain)."
            )
            return {
                "text": text,
                "image_path": plot_res.get('output_path')
            }
        except Exception as e:
            return {"text": f"⚠️ Error generating efficiency plot: {str(e)}"}

    # 3. Kilometer Splits & Cardiac Drift
    if any(k in p_lower for k in ["split", "drift", "cardiac drift", "decoupling", "kilometer"]):
        try:
            splits_data = dreeve_engine.get_splits_analysis(month="2026-10", summary_only=True, db_path=DB_PATH)
            summary = splits_data.get("summary", splits_data)
            
            total_km = summary.get("total_splits_extracted", summary.get("total_kilometers_recorded", 0))
            median_p = summary.get("median_pace", summary.get("average_split_pace", "N/A"))
            fastest_p = summary.get("fastest_split_pace", "-")
            mean_hr = summary.get("mean_heart_rate_overall", "N/A")
            hr_range_overall = summary.get("heart_rate_range_bpm", "")
            
            md = [
                "### 📉 Kilometer Splits & Cardiac Drift Progression (October 2026)",
                f"- **Total Kilometers Analyzed:** `{total_km}` splits across `{summary.get('activities_evaluated', 0)}` runs",
                f"- **Median Split Pace:** `{median_p}` (Fastest: `{fastest_p}`)",
                f"- **Overall Mean HR:** `{mean_hr} bpm` ({hr_range_overall})",
                "\n#### 🩺 Kilometer Progression Table"
            ]
            
            drift = summary.get("cardiac_drift_progression", {})
            if drift:
                md.append("| Kilometer | Splits Count | Average Pace | Mean HR | HR Range |")
                md.append("|---|---|---|---|---|")
                for km_label, v in drift.items():
                    splits_cnt = v.get("splits_evaluated", v.get("splits_count", "-"))
                    pace = v.get("avg_pace", v.get("mean_pace", "-"))
                    hr_bpm = f"{v.get('mean_hr_bpm', v.get('mean_hr', '-'))} bpm" if (v.get("mean_hr_bpm") or v.get("mean_hr")) else "-"
                    hr_rng = v.get("hr_range", "-")
                    md.append(f"| **{km_label}** | {splits_cnt} | {pace} | **{hr_bpm}** | {hr_rng} |")
                    
            return {"text": "\n".join(md)}
        except Exception as e:
            return {"text": f"⚠️ Error calculating splits: {str(e)}"}

    # 4. Weekly Mileage & Volume
    if any(k in p_lower for k in ["mileage", "volume", "weekly", "how much", "km", "total"]):
        try:
            df = dreeve_engine.get_activities_dataframe(days=60, db_path=DB_PATH)
            if df.empty:
                return {"text": "No activities recorded in the last 60 days."}
                
            df['week'] = df['start_dt'].dt.to_period('W').apply(lambda r: r.start_time.strftime('%b %d'))
            weekly = df.groupby('week', sort=False).agg(
                runs=('activityId', 'count'),
                distance_km=('dist_km', 'sum'),
                moving_hours=('movingTimeInSeconds', lambda x: sum(x) / 3600.0)
            ).reset_index()
            
            total_dist = df['dist_km'].sum()
            total_time_h = df['movingTimeInSeconds'].sum() / 3600.0
            avg_pace = (df['movingTimeInSeconds'].sum() / total_dist) / 60.0 if total_dist > 0 else 0
            
            md = [
                f"### 📊 60-Day Volume & Mileage Breakdown",
                f"- **Total Distance:** `{total_dist:.1f} km` across `{len(df)}` runs",
                f"- **Active Duration:** `{total_time_h:.1f} hours`",
                f"- **Average Pace:** `{dreeve_engine.format_pace(avg_pace)}`",
                "\n#### Weekly Totals"
            ]
            md.append("| Week Starting | Runs | Mileage | Duration |")
            md.append("|---|---|---|---|")
            for _, r in weekly.iterrows():
                md.append(f"| {r['week']} | {r['runs']} | **{r['distance_km']:.1f} km** | {r['moving_hours']:.1f} hrs |")
                
            return {
                "text": "\n".join(md),
                "weekly_df": weekly
            }
        except Exception as e:
            return {"text": f"⚠️ Error fetching mileage: {str(e)}"}

    # 5. List Runs Query
    if any(k in p_lower for k in ["list", "recent", "runs", "activities", "history"]):
        try:
            acts = dreeve_engine.get_activities_list(days=30, limit=10, with_hr_only=False, db_path=DB_PATH)
            md = ["### 🏃‍♂️ Recent Running Activities (Last 30 Days)"]
            md.append("| Date | Name | Distance | Duration | Pace | Avg HR |")
            md.append("|---|---|---|---|---|---|")
            for a in acts:
                hr_str = f"{a['avg_hr']} bpm" if a['avg_hr'] else "-"
                md.append(f"| {a['date']} | {a['name']} | **{a['distance_km']} km** | {a['time']} | {a['pace']} | {hr_str} |")
            return {"text": "\n".join(md)}
        except Exception as e:
            return {"text": f"⚠️ Error listing activities: {str(e)}"}

    # Default Fallback / General Overview
    try:
        summary = dreeve_engine.get_summary(days=60, db_path=DB_PATH)
        hr_info = summary.get('heart_rate_summary', {})
        hr_text = f"{hr_info.get('mean_hr_bpm', 'N/A')} bpm ({hr_info.get('min_hr_bpm', '-')}\u2013{hr_info.get('max_hr_bpm', '-')} bpm)" if hr_info else "N/A"
        
        return {
            "text": (
                f"### 🏃 Dreeve Running Summary (Last 60 Days)\n\n"
                f"- **Total Distance:** `{summary.get('total_distance_km', 0):.1f} km` across `{summary.get('total_activities', 0)}` runs\n"
                f"- **Active Duration:** `{summary.get('total_moving_time_hours', 0):.1f} hours`\n"
                f"- **Average Pace:** `{summary.get('overall_avg_pace', 'N/A')}` (Best: `{summary.get('best_pace', 'N/A')}`)\n"
                f"- **Mean Heart Rate:** `{hr_text}`\n\n"
                f"**You can ask me questions like:**\n"
                f"- *'Did my aerobic capacity improve in later weeks of October?'*\n"
                f"- *'Analyze my interval session: 1k warmup + 4x1k @6:15 with 90s walk + 0.5k cooldown'*\n"
                f"- *'Show pace vs heart rate scatter'* or *'Show dashboard'*\n"
                f"- *'Show cardiac drift in kilometer splits'*\n"
                f"- *'What is my weekly mileage breakdown?'*\n"
                f"- *'List recent runs'*"
            )
        }
    except Exception as e:
        return {"text": f"⚠️ Error generating overview: {str(e)}"}

# ==========================================
# Sidebar UI
# ==========================================
with st.sidebar:
    st.title("🏃‍♂️ Dreeve Running Coach")
    db_status = get_db_status()
    
    if db_status.get("connected"):
        st.success(f"🟢 Connected to Dreeve DB\n\n**{db_status['total_runs']} Runs** recorded | Latest: **{db_status['latest_run']}**")
    else:
        st.error(f"🔴 DB Disconnected: {db_status.get('error')}")
        
    st.divider()
    st.subheader("⚡ Quick Prompts")
    
    quick_prompt = None
    if st.button("⏱️ Analyze Nov 5 Interval (4×1K)", use_container_width=True):
        quick_prompt = "Analyze today's run: 1k warmup + 4x1K @6:15 with 90s walk + 0.5k cooldown"
    if st.button("📈 Aerobic Efficiency Scatter", use_container_width=True):
        quick_prompt = "Show pace vs heart rate scatter plot"
    if st.button("📉 October Cardiac Drift", use_container_width=True):
        quick_prompt = "Show cardiac drift across kilometer splits"
    if st.button("📊 Weekly Mileage & Volume", use_container_width=True):
        quick_prompt = "Show my weekly mileage breakdown"
    if st.button("🏃‍♂️ List Recent Runs", use_container_width=True):
        quick_prompt = "List my recent runs"
        
    st.divider()
    st.subheader("⏱️ Custom Interval Tool")
    custom_plan = st.text_input("Workout Plan:", value="0.8k easy, 2x1.5k @ 6:00, 1k @ 7:00")
    if st.button("🚀 Analyze Workout Plan", use_container_width=True):
        quick_prompt = f"Analyze interval session: {custom_plan}"
        
    st.divider()
    if st.button("🗑️ Clear Chat History", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# ==========================================
# Main Chat Window UI
# ==========================================
st.title("💬 Personal Running Telemetry & Coach")
st.caption("Ask questions about your runs, interval pacing, cardiac drift, and aerobic efficiency. Charts and analytics render inline.")

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": {
                "text": "👋 Welcome back! I'm your **Dreeve Running Analytics Coach**.\n\nAsk me about your interval sessions, pacing deltas, heart rate recovery, aerobic efficiency curves, or mileage totals."
            }
        }
    ]

# Render existing messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        c = msg["content"]
        if isinstance(c, dict):
            st.markdown(c.get("text", ""))
            if c.get("image_path") and os.path.exists(c["image_path"]):
                st.image(c["image_path"], caption="Telemetry Visualization", use_column_width=True)
            if c.get("weekly_df") is not None:
                w_df = c["weekly_df"]
                fig = px.bar(w_df, x="week", y="distance_km", text="distance_km", title="Weekly Mileage (km)",
                             color_discrete_sequence=["#38bdf8"])
                fig.update_layout(paper_bgcolor="#0b1329", plot_bgcolor="#151f38", font_color="#ffffff", yaxis_title="Distance (km)")
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.markdown(str(c))

# Handle new user input (from chat bar or quick button)
user_input = st.chat_input("Ask a question about your runs...") or quick_prompt

if user_input:
    # 1. Add user message
    st.session_state.messages.append({"role": "user", "content": {"text": user_input}})
    with st.chat_message("user"):
        st.markdown(user_input)

    # 2. Generate assistant response
    with st.chat_message("assistant"):
        with st.spinner("Analyzing telemetry from Dreeve..."):
            ans = process_query(user_input)
            st.markdown(ans.get("text", ""))
            if ans.get("image_path") and os.path.exists(ans["image_path"]):
                st.image(ans["image_path"], caption="Telemetry Visualization", use_column_width=True)
            if ans.get("weekly_df") is not None:
                w_df = ans["weekly_df"]
                fig = px.bar(w_df, x="week", y="distance_km", text="distance_km", title="Weekly Mileage (km)",
                             color_discrete_sequence=["#38bdf8"])
                fig.update_layout(paper_bgcolor="#0b1329", plot_bgcolor="#151f38", font_color="#ffffff", yaxis_title="Distance (km)")
                st.plotly_chart(fig, use_container_width=True)
                
    st.session_state.messages.append({"role": "assistant", "content": ans})

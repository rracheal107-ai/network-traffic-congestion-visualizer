"""
Network Traffic Congestion Monitoring and Optimization Visualizer
=================================================================
Streamlit dashboard (presentation layer). It samples live network traffic
with psutil, computes throughput and packet rates, classifies congestion
(Normal / Moderate / Congested) against a configurable link capacity,
recommends optimization actions and logs samples to SQLite.

If live counters are unavailable, it switches to a clearly labelled
simulated traffic stream so the dashboard can always be demonstrated.

Run with:  streamlit run app.py      (or ./start.sh / start.bat)
"""

import time
from collections import deque

import streamlit as st

import core

st.set_page_config(
    page_title="Network Traffic Congestion Visualizer",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACCENT = "#EA580C"
LEVEL_STYLE = {
    core.NORMAL: ("#15803D", "#DCFCE7", "🟢"),
    core.MODERATE: ("#B45309", "#FEF3C7", "🟠"),
    core.CONGESTED: ("#B91C1C", "#FEE2E2", "🔴"),
}


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
def inject_theme():
    st.markdown(f"""
    <style>
    footer {{ visibility:hidden; }} #MainMenu {{ visibility:hidden; }}
    .block-container {{ padding-top:1.2rem; padding-bottom:4rem; max-width:1250px; }}
    .hero {{ background:linear-gradient(135deg,{ACCENT},#111827); color:#fff; border-radius:18px;
             padding:24px 28px; margin-bottom:18px; }}
    .hero h1 {{ font-size:1.7rem; font-weight:800; margin:0; color:#fff; }}
    .hero p {{ margin:.35rem 0 0; opacity:.92; }}
    div[data-testid="stMetric"] {{ background:#F8FAFC; border:1px solid #E2E8F0;
        border-radius:14px; padding:12px 16px; }}
    .level {{ border-radius:14px; padding:16px 20px; font-size:1.25rem; font-weight:700; }}
    </style>""", unsafe_allow_html=True)


def hero():
    st.markdown(
        '<div class="hero"><h1>📈 Network Traffic Congestion Monitoring and Optimization Visualizer</h1>'
        '<p>Live throughput, packet flow, congestion level and optimization recommendations</p></div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
def init_state():
    ss = st.session_state
    for key in ("time_labels", "down_kb", "up_kb", "pkt_recv", "pkt_sent", "util"):
        ss.setdefault(key, deque(maxlen=core.MAX_POINTS))
    ss.setdefault("prev", None)
    ss.setdefault("prev_t", None)
    ss.setdefault("prev_live", None)
    ss.setdefault("sample_count", 0)
    if "log" not in ss:
        ss.log = core.TrafficLog("traffic_log.db")


def reset_history():
    ss = st.session_state
    for key in ("time_labels", "down_kb", "up_kb", "pkt_recv", "pkt_sent", "util"):
        ss[key].clear()
    ss.prev = None
    ss.prev_t = None
    ss.sample_count = 0


def record_sample(force_sim, heavy, capacity, mod_t, cong_t):
    """Take one reading, compute rates, utilisation and level.

    Returns (reading, rates or None, utilisation, level)."""
    ss = st.session_state
    t = time.time()
    cur = core.read_counters(ss, force_simulated=force_sim, heavy_load=heavy)
    # Switching between live and simulated makes counters jump: restart deltas.
    if ss.prev is not None and ss.prev_live != cur["live"]:
        ss.prev = None
    rates, util, level = None, 0.0, core.NORMAL
    if ss.prev is not None and ss.prev_t is not None:
        rates = core.compute_rates(ss.prev, cur, t - ss.prev_t)
        util = core.utilisation_percent(max(rates["down_kb"], rates["up_kb"]), capacity)
        level = core.classify(util, mod_t, cong_t)
        ss.down_kb.append(rates["down_kb"])
        ss.up_kb.append(rates["up_kb"])
        ss.pkt_recv.append(rates["pkt_recv"])
        ss.pkt_sent.append(rates["pkt_sent"])
        ss.util.append(util)
        ss.time_labels.append(time.strftime("%H:%M:%S", time.localtime(t)))
        ss.sample_count += 1
    ss.prev, ss.prev_t, ss.prev_live = cur, t, cur["live"]
    return cur, rates, util, level


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def main():
    init_state()
    inject_theme()
    hero()
    ss = st.session_state

    with st.sidebar:
        st.header("Controls")
        source = st.radio("Traffic source", ["Live (auto fallback)", "Simulated (demo)"])
        force_sim = source.startswith("Simulated")
        heavy = st.toggle("Simulate heavy load", value=False, disabled=not force_sim,
                          help="Doubles simulated traffic to demonstrate congestion levels.")
        st.divider()
        capacity = st.number_input("Link capacity (Mbps)", min_value=0.1, max_value=10000.0,
                                   value=10.0, step=1.0,
                                   help="Bandwidth of the monitored link, e.g. the lab internet link.")
        mod_t, cong_t = st.slider("Thresholds (% of capacity): Moderate / Congested",
                                  min_value=10, max_value=100, value=(50, 80))
        st.divider()
        auto_refresh = st.toggle("Auto-refresh", value=True)
        interval = st.slider("Refresh interval (seconds)", 1, 10, 2)
        log_on = st.toggle("Save samples to log (SQLite)", value=True)
        c1, c2 = st.columns(2)
        if c1.button("Reset history"):
            reset_history()
            st.rerun()
        if c2.button("Clear log"):
            ss.log.clear()
            st.rerun()

    cur, rates, util, level = record_sample(force_sim, heavy, capacity, mod_t, cong_t)
    live = cur["live"]
    rows, iface_live = core.read_per_interface(ss, force_simulated=force_sim)
    busiest = core.busiest_interface(rows)
    direction = "download"
    if rates and rates["up_kb"] > rates["down_kb"]:
        direction = "upload"
    recs = core.recommend(level, busiest, direction)
    if rates and log_on:
        ss.log.save(rates, util, level, live, recommendations=recs)

    # --- Status banner ---------------------------------------------------- #
    if live:
        st.success("Live capture active: sampling real network counters with psutil.")
    elif not core.PSUTIL_AVAILABLE:
        st.error(f"psutil is not available ({core.PSUTIL_IMPORT_ERROR or 'import failed'}). "
                 "Showing SIMULATED traffic.")
    elif force_sim:
        st.info("Simulated (demo) mode selected. Values below are SIMULATED.")
    else:
        st.warning("Live capture returned no data. Showing SIMULATED traffic.")

    # --- Congestion status and recommendations ---------------------------- #
    fg, bg, icon = LEVEL_STYLE[level]
    left, right = st.columns([1, 2])
    with left:
        with st.container(border=True):
            st.subheader("🚦 Congestion status")
            if rates is None:
                st.info("Collecting the first sample...")
            else:
                st.markdown(f'<div class="level" style="color:{fg};background:{bg}">'
                            f'{icon} {level}</div>', unsafe_allow_html=True)
                st.metric("Link utilisation", f"{util:.1f} %")
                st.progress(min(util, 100.0) / 100.0)
                st.caption(f"Capacity {capacity:g} Mbps · Moderate ≥ {mod_t}% · Congested > {cong_t}%")
    with right:
        with st.container(border=True):
            st.subheader("🛠️ Optimization recommendations")
            for action, priority in recs:
                st.markdown(f"- **[{priority}]** {action}")

    # --- Live throughput --------------------------------------------------- #
    with st.container(border=True):
        st.subheader("📊 Live throughput")
        last_down = ss.down_kb[-1] if ss.down_kb else 0.0
        last_up = ss.up_kb[-1] if ss.up_kb else 0.0
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Download now", f"{last_down:.1f} KB/s", f"{core.kbps_to_mbps(last_down):.2f} Mbps",
                  delta_color="off")
        m2.metric("Upload now", f"{last_up:.1f} KB/s", f"{core.kbps_to_mbps(last_up):.2f} Mbps",
                  delta_color="off")
        m3.metric("Peak download", f"{max(ss.down_kb) if ss.down_kb else 0.0:.1f} KB/s")
        m4.metric("Peak upload", f"{max(ss.up_kb) if ss.up_kb else 0.0:.1f} KB/s")
        t1, t2 = st.columns(2)
        t1.metric("Total received", core.human_bytes(cur["bytes_recv"]))
        t2.metric("Total sent", core.human_bytes(cur["bytes_sent"]))

    # --- Charts ------------------------------------------------------------ #
    a, b = st.columns(2)
    with a:
        with st.container(border=True):
            st.subheader("📈 Throughput over time (KB/s)")
            if ss.time_labels:
                st.line_chart({"Download (KB/s)": list(ss.down_kb),
                               "Upload (KB/s)": list(ss.up_kb)}, height=260)
            else:
                st.info("Charts appear after the second sample.")
    with b:
        with st.container(border=True):
            st.subheader("📉 Link utilisation over time (%)")
            if ss.util:
                st.line_chart({"Utilisation (%)": list(ss.util),
                               "Moderate threshold": [mod_t] * len(ss.util),
                               "Congested threshold": [cong_t] * len(ss.util)}, height=260)
            else:
                st.info("Charts appear after the second sample.")

    c, d = st.columns(2)
    with c:
        with st.container(border=True):
            st.subheader("📦 Packet flow (packets/s, latest)")
            if ss.pkt_recv:
                st.bar_chart({"Direction": ["Received", "Sent"],
                              "Packets/s": [ss.pkt_recv[-1], ss.pkt_sent[-1]]},
                             x="Direction", y="Packets/s", height=240)
            else:
                st.info("No packet samples yet.")
    with d:
        with st.container(border=True):
            st.subheader("🔌 Per-interface totals")
            if not iface_live:
                st.caption("(simulated interfaces)")
            rows_sorted = sorted(rows, key=lambda r: r[1] + r[2], reverse=True)
            st.dataframe([{"Interface": n, "Sent": core.human_bytes(s), "Received": core.human_bytes(r),
                           "Total": core.human_bytes(s + r)} for n, s, r in rows_sorted],
                         use_container_width=True, hide_index=True)

    # --- Congestion log ---------------------------------------------------- #
    with st.container(border=True):
        st.subheader("🗂️ Congestion event log")
        counts = ss.log.counts()
        k1, k2, k3 = st.columns(3)
        k1.metric("Normal samples", counts[core.NORMAL])
        k2.metric("Moderate samples", counts[core.MODERATE])
        k3.metric("Congested samples", counts[core.CONGESTED])
        events = ss.log.recent_events(20)
        if events:
            st.dataframe(events, use_container_width=True, hide_index=True)
        else:
            st.caption("No Moderate or Congested events recorded yet.")
        st.download_button("Download traffic log (CSV)", ss.log.export_csv(),
                           file_name="traffic_log.csv", mime="text/csv")

    st.caption(f"Samples: {ss.sample_count} | Window: last {core.MAX_POINTS} samples | "
               f"Mode: {'LIVE' if live else 'SIMULATED'} | Busiest interface: {busiest}")

    if auto_refresh:
        time.sleep(max(1, min(int(interval), 10)))
        st.rerun()


if __name__ == "__main__":
    main()

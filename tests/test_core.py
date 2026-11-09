import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402


def test_human_bytes():
    assert core.human_bytes(512) == "512 B"
    assert core.human_bytes(2048) == "2.0 KB"
    assert core.human_bytes(5_000_000) == "4.77 MB"
    assert core.human_bytes("bad") == "0 B"


def test_compute_rates_and_counter_reset():
    prev = {"bytes_recv": 1000, "bytes_sent": 1000, "packets_recv": 10, "packets_sent": 10}
    cur = {"bytes_recv": 1000 + 2048 * 2, "bytes_sent": 500, "packets_recv": 30, "packets_sent": 10}
    r = core.compute_rates(prev, cur, 2)
    assert r["down_kb"] == 2.0
    assert r["up_kb"] == 0.0          # counter went backwards -> clamped
    assert r["pkt_recv"] == 10.0


def test_utilisation_and_classify():
    # 1250 KB/s = 10.24 Mbps -> 102.4% of a 10 Mbps link
    assert round(core.utilisation_percent(1250, 10), 1) == 102.4
    assert core.utilisation_percent(100, 0) == 0.0
    assert core.classify(10) == core.NORMAL
    assert core.classify(50) == core.MODERATE
    assert core.classify(80) == core.MODERATE
    assert core.classify(80.1) == core.CONGESTED


def test_recommendations():
    assert core.recommend(core.NORMAL)[0][1] == "Low"
    cong = core.recommend(core.CONGESTED, "eth0")
    assert any("eth0" in a for a, _ in cong)
    assert all(p in ("High", "Medium", "Low") for _, p in cong)


def test_simulator_is_monotonic():
    state = {}
    a = core.simulated_counters(state)
    b = core.simulated_counters(state, heavy_load=True)
    assert b["bytes_recv"] > a["bytes_recv"] and not b["live"]


def test_read_counters_never_raises():
    state = {}
    r = core.read_counters(state)
    assert {"bytes_sent", "bytes_recv", "packets_sent", "packets_recv", "live"} <= set(r)
    rows, _ = core.read_per_interface(state)
    assert core.busiest_interface(rows) is not None


def test_traffic_log(tmp_path):
    log = core.TrafficLog(str(tmp_path / "t.db"))
    rates = {"down_kb": 900.0, "up_kb": 100.0, "pkt_recv": 50.0, "pkt_sent": 20.0}
    log.save(rates, 85.0, core.CONGESTED, True, recommendations=core.recommend(core.CONGESTED))
    log.save(rates, 10.0, core.NORMAL, True)
    assert log.counts()[core.CONGESTED] == 1
    assert len(log.recent_events()) == 1
    csv_text = log.export_csv()
    assert csv_text.splitlines()[0].startswith("LogID") and len(csv_text.splitlines()) == 3
    log.clear()
    assert log.counts()[core.NORMAL] == 0

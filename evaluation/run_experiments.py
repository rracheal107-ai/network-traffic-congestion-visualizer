"""Run with:  python evaluation/run_experiments.py   (takes about 3 minutes)

Run the visualizer's engine (core.py, the same code behind the Streamlit
dashboard) on controlled live traffic and on its demo mode, and save results.

Test A (live): a separate process sends TCP traffic over the loopback
interface at known target rates in phases. The system samples live counters
every 2 s exactly as app.py does, computes rates, utilisation (10 Mbps link)
and congestion level, logs to SQLite, and we compare measured vs. ground truth.

Test B/C (demo): simulated traffic, normal and heavy load, 2-s interval.
"""
import json, multiprocessing as mp, os, socket, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # project folder containing core.py
import core

KB = 1024
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
PHASES = [(0, 30), (400, 40), (800, 40), (1200, 40), (0, 30)]  # (KB/s, seconds)
INTERVAL = 2.0
CAPACITY = 10.0


def receiver(port, ready):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port)); s.listen(1); ready.set()
    c, _ = s.accept()
    while c.recv(65536):
        pass


def sender(port, total, phase_idx):
    c = socket.create_connection(("127.0.0.1", port))
    chunk = b"x" * 8192
    for i, (rate, dur) in enumerate(PHASES):
        phase_idx.value = i
        end = time.time() + dur
        if rate == 0:
            time.sleep(dur); continue
        start = time.time(); sent = 0
        while time.time() < end:
            target = (time.time() - start) * rate * KB
            if sent < target:
                c.sendall(chunk); sent += len(chunk)
                with total.get_lock():
                    total.value += len(chunk)
            else:
                time.sleep(0.002)
    phase_idx.value = -1
    c.close()


def live_test():
    total = mp.Value("d", 0.0); phase = mp.Value("i", 0); ready = mp.Event()
    r = mp.Process(target=receiver, args=(50555, ready), daemon=True); r.start(); ready.wait()
    s = mp.Process(target=sender, args=(50555, total, phase)); s.start()
    state = {}
    log = core.TrafficLog(f"{OUT}/live.db"); log.clear()
    prev = core.read_counters(state); prev_t = time.time(); prev_gt = total.value
    rows = []; overhead = []; t0 = prev_t
    while s.is_alive():
        time.sleep(INTERVAL)
        t1 = time.perf_counter()
        cur = core.read_counters(state); t = time.time()
        rates = core.compute_rates(prev, cur, t - prev_t)
        util = core.utilisation_percent(max(rates["down_kb"], rates["up_kb"]), CAPACITY)
        level = core.classify(util)
        per, _ = core.read_per_interface(state)
        busiest = core.busiest_interface(per)
        recs = core.recommend(level, busiest)
        log.save(rates, util, level, cur["live"], recommendations=recs)
        overhead.append((time.perf_counter() - t1) * 1000)
        gt = (total.value - prev_gt) / (t - prev_t) / KB
        rows.append({"t": round(t - t0, 1), "phase": phase.value, "target_kbps": PHASES[phase.value][0] if phase.value >= 0 else 0,
                     "actual_sent_kbps": gt, "down_kb": rates["down_kb"], "up_kb": rates["up_kb"],
                     "pkt_recv": rates["pkt_recv"], "pkt_sent": rates["pkt_sent"], "util": util,
                     "level": level, "live": cur["live"], "busiest": busiest})
        prev, prev_t, prev_gt = cur, t, total.value
    s.join(); r.terminate()
    open(f"{OUT}/live_log.csv", "w").write(log.export_csv())
    per, _ = core.read_per_interface(state)
    return rows, overhead, per, log.counts()


def demo_test(heavy, seconds=120):
    state = {}; log = core.TrafficLog(f"{OUT}/demo_{'heavy' if heavy else 'normal'}.db"); log.clear()
    prev = core.read_counters(state, force_simulated=True, heavy_load=heavy); prev_t = time.time(); rows = []; t0 = prev_t
    while time.time() - t0 < seconds:
        time.sleep(INTERVAL)
        cur = core.read_counters(state, force_simulated=True, heavy_load=heavy); t = time.time()
        rates = core.compute_rates(prev, cur, t - prev_t)
        util = core.utilisation_percent(max(rates["down_kb"], rates["up_kb"]), CAPACITY)
        level = core.classify(util)
        log.save(rates, util, level, cur["live"], recommendations=core.recommend(level))
        rows.append({"t": round(t - t0, 1), "down_kb": rates["down_kb"], "up_kb": rates["up_kb"],
                     "pkt_recv": rates["pkt_recv"], "pkt_sent": rates["pkt_sent"], "util": util, "level": level})
        prev, prev_t = cur, t
    return rows, log.counts()


if __name__ == "__main__":
    res = {}
    th = {}
    def run_demo(h):
        th[h] = demo_test(h)
    ts = [threading.Thread(target=run_demo, args=(h,)) for h in (False, True)]
    for x in ts: x.start()
    rows, overhead, per, counts = live_test()
    for x in ts: x.join()
    res["live"] = {"rows": rows, "overhead_ms": overhead, "per_interface": per, "counts": counts}
    res["demo_normal"] = {"rows": th[False][0], "counts": th[False][1]}
    res["demo_heavy"] = {"rows": th[True][0], "counts": th[True][1]}
    json.dump(res, open(f"{OUT}/results.json", "w"), indent=1)
    print("done", len(rows), counts, th[False][1], th[True][1])

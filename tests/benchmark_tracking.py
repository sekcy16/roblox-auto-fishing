"""Benchmark simulation comparing old vs new auto-tracking logic under identical conditions."""

import random
import numpy as np


def old_detect_bar_sim(target_left, target_right, marker_x, marker_w=10, target_h=18):
    """Simulate old detect_bar behavior."""
    w = target_right - target_left
    # Old logic: if w < h * 1.5: return absent
    if w < target_h * 1.5:
        return "absent", None, None
    # Old logic occlusion: if marker covers gap and fragments are < 12px, return absent
    marker_left = marker_x - marker_w / 2
    marker_right = marker_x + marker_w / 2
    if marker_left < target_right and marker_right > target_left:
        left_frag = max(0, marker_left - target_left)
        right_frag = max(0, target_right - marker_right)
        if left_frag < 12 and right_frag < 12:
            return "absent", None, None
    return "valid", marker_x, (target_left, target_right)


def new_detect_bar_sim(target_left, target_right, marker_x, marker_w=10, target_h=18):
    """Simulate new detect_bar behavior."""
    w = target_right - target_left
    if w < 8:
        return "absent", None, None
    return "valid", marker_x, (target_left, target_right)


def old_choose_hold(x, target, velocity, lead, margin, right_on_hold, held):
    # In old code: velocity was 0.0, so predicted = x + 0
    predicted = x + 0.0 * lead
    error = (target[0] + target[1]) / 2.0 - predicted
    if abs(error) <= margin:
        return held
    return (error > 0) == right_on_hold


def new_choose_hold(x, target, velocity, lead, margin, right_on_hold, held):
    target_center = (target[0] + target[1]) / 2.0
    predicted = x + velocity * lead
    error = target_center - predicted
    if abs(error) <= margin:
        if right_on_hold:
            if velocity > 40.0 and predicted >= target_center:
                return False
            elif velocity < -40.0 and predicted <= target_center:
                return True
        else:
            if velocity < -40.0 and predicted <= target_center:
                return False
            elif velocity > 40.0 and predicted >= target_center:
                return True
        return held
    return (error > 0) == right_on_hold


def simulate_trial(mode, target_width, num_steps=200, input_delay=0.04):
    """Simulate a single minigame trial."""
    track_min = 20.0
    track_max = 280.0
    target_center = 150.0
    target = (target_center - target_width / 2.0, target_center + target_width / 2.0)

    marker_x = random.choice([40.0, 260.0])
    marker_v = 0.0
    right_on_hold = True

    held = False
    action_queue = []

    prev_x = None
    prev_t = None
    estimated_v = 0.0
    calibrated_latency = 0.04

    t = 0.0
    in_target_count = 0
    detection_losses = 0

    if mode == "old":
        margin = min(3.0, max(0.0, target_width * 0.15))
    else:
        prop_margin = target_width * 0.18
        safe_max = max(1.0, (target_width / 2.0) * 0.45)
        safe_min = min(1.2, max(0.6, (target_width / 2.0) * 0.30))
        margin = max(safe_min, min(min(3.0, prop_margin), safe_max))

    for step in range(num_steps):
        dt = 0.016 if random.random() > 0.15 else 0.045
        t_capture = t
        t += dt

        while action_queue and action_queue[0][0] <= t:
            _, held = action_queue.pop(0)

        accel = 700.0 if held else -650.0
        marker_v += accel * dt
        marker_v = max(-350.0, min(350.0, marker_v))
        marker_x += marker_v * dt

        if marker_x < track_min:
            marker_x = track_min
            marker_v = abs(marker_v) * 0.5
        elif marker_x > track_max:
            marker_x = track_max
            marker_v = -abs(marker_v) * 0.5

        if target[0] <= marker_x <= target[1]:
            in_target_count += 1

        if mode == "old":
            status, det_x, det_target = old_detect_bar_sim(target[0], target[1], marker_x)
        else:
            status, det_x, det_target = new_detect_bar_sim(target[0], target[1], marker_x)

        if status != "valid":
            detection_losses += 1
            action_queue.append((t + input_delay, False))
            continue

        frame_age = 0.005
        decision_time = t_capture + frame_age

        if mode == "old":
            desired = old_choose_hold(det_x, det_target, 0.0, 0.0, margin, right_on_hold, held)
        else:
            if prev_x is not None and prev_t is not None and (t_capture - prev_t) > 0:
                v_inst = (det_x - prev_x) / (t_capture - prev_t)
                if estimated_v != 0 and (v_inst * estimated_v < -200.0):
                    estimated_v = v_inst
                else:
                    estimated_v = 0.70 * v_inst + 0.30 * estimated_v
            else:
                estimated_v = 0.0

            prev_x = det_x
            prev_t = t_capture

            total_lat = frame_age + calibrated_latency
            desired = new_choose_hold(det_x, det_target, estimated_v, total_lat, margin, right_on_hold, held)

        action_queue.append((decision_time + input_delay, desired))

    in_target_pct = (in_target_count / num_steps) * 100.0
    return in_target_pct, detection_losses


def run_benchmark(trials=250):
    print("=" * 65)
    print(f"BENCHMARK: OLD vs NEW TRACKING LOGIC ({trials} trials per condition)")
    print("=" * 65)

    target_sizes = [
        ("Smallest (w=16px)", 16.0),
        ("Small (w=25px)", 25.0),
        ("Medium (w=45px)", 45.0),
        ("Large (w=75px)", 75.0),
    ]

    for label, width in target_sizes:
        print(f"\n--- Condition: {label} ---")
        old_pcts, old_losses = [], []
        new_pcts, new_losses = [], []

        for _ in range(trials):
            p_old, l_old = simulate_trial("old", width)
            old_pcts.append(p_old)
            old_losses.append(l_old)

            p_new, l_new = simulate_trial("new", width)
            new_pcts.append(p_new)
            new_losses.append(l_new)

        avg_old_pct = np.mean(old_pcts)
        avg_new_pct = np.mean(new_pcts)
        success_old = np.mean([1 if p >= 40.0 else 0 for p in old_pcts]) * 100.0
        success_new = np.mean([1 if p >= 40.0 else 0 for p in new_pcts]) * 100.0

        print(f"  [OLD LOGIC] In-Target Time: {avg_old_pct:5.1f}% | Win Rate (>=40% in bar): {success_old:5.1f}% | Avg Vision Drops: {np.mean(old_losses):.1f}")
        print(f"  [NEW LOGIC] In-Target Time: {avg_new_pct:5.1f}% | Win Rate (>=40% in bar): {success_new:5.1f}% | Avg Vision Drops: {np.mean(new_losses):.1f}")
        improvement = avg_new_pct - avg_old_pct
        print(f"  => Improvement: +{improvement:5.1f}% in-target time | Win Rate: {success_old:.1f}% -> {success_new:.1f}%")


if __name__ == "__main__":
    run_benchmark(trials=250)

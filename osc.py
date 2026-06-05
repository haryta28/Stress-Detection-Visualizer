import serial
import time
import re
from collections import deque
from pythonosc import udp_client

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
SERIAL_PORT        = "COM8"
BAUD_RATE          = 115200
TOUCHDESIGNER_IP   = "127.0.0.1"
OSC_PORT           = 7000

# MAX30102: if IR value is below this, no finger is on the sensor.
# Adjust based on your sensor; typical resting IR is 50000–150000+.
IR_FINGER_THRESHOLD = 50000

# Calibration: collect readings for this many seconds to set personal baselines
CALIBRATION_DURATION = 15   # seconds

# GSR drift correction: rolling window (number of samples)
GSR_WINDOW_SIZE    = 30

# How many seconds of "no finger" before sending idle signal to TD
IDLE_TIMEOUT       = 10

# 2D Affect Matrix thresholds (deviations from personal baseline)
# HRV: positive offset means calmer than baseline
HRV_HIGH_OFFSET    = +10   # ms above baseline → High HRV (calm)
HRV_LOW_OFFSET     = -10   # ms below baseline → Low HRV (stressed)
# GSR: delta from rolling average
GSR_DELTA_HIGH     = +30   # rising conductance → arousal/excitement

# State confirmation: how many consecutive reads must agree before
# we commit to a new state. Prevents single-sample flicker.
# At ~1 read/sec (HRV updates every 5-6s), 3 = ~15-18 seconds of stability.
N_CONFIRM_READINGS = 3

# Minimum seconds a committed state must hold before it can change.
# Matches the biological reality: HRV cannot physiologically change
# in under ~8 seconds.
STATE_HOLD_TIME    = 8    # seconds

# How often (seconds) to send raw sensor values for TD debugging display.
# State flags are only sent on actual state changes (not on a timer).
RAW_SEND_INTERVAL  = 1.0  # seconds
# ─────────────────────────────────────────────────────────────────


# ── OSC CLIENT ───────────────────────────────────────────────────
client = udp_client.SimpleUDPClient(TOUCHDESIGNER_IP, OSC_PORT)


def get_value(line, key, default="0"):
    """Extract a labelled value from a serial string like 'BPM=72 HRV=58 GSR=412'."""
    match = re.search(rf"{key}\s*=\s*([A-Za-z0-9.\-]+)", line)
    return match.group(1) if match else default


def classify_state(hrv, hrv_baseline, gsr_delta):
    """
    Classify the user's emotional state using the 2D Circumplex Affect Model.
    Returns: (state_name, stress_level 0-1, calm_level 0-1)

    Axes:
      HRV  → Valence  (High HRV = Calm/Positive)
      GSR Δ → Arousal (High GSR delta = Excited/Tense)
    """
    high_hrv = hrv >= (hrv_baseline + HRV_HIGH_OFFSET)
    low_hrv  = hrv <= (hrv_baseline + HRV_LOW_OFFSET)
    high_gsr = gsr_delta >= GSR_DELTA_HIGH

    if high_hrv and not high_gsr:
        return "ZEN",     0.0, 1.0   # Calm + Low arousal
    elif low_hrv and high_gsr:
        return "STRESS",  1.0, 0.0   # Stressed + High arousal
    elif high_hrv and high_gsr:
        return "FLOW",    0.4, 0.9   # Positive + High arousal (eustress / focus)
    else:
        return "BURNOUT", 0.6, 0.2   # Stressed/flat + Low arousal (fatigue)


# ── CONNECT TO ESP32 ─────────────────────────────────────────────
print(f"Connecting to ESP32 on {SERIAL_PORT}...")
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(2)
    ser.reset_input_buffer()
except Exception as e:
    print(f"ERROR: Could not open serial port — {e}")
    exit()

print(f"Streaming to TouchDesigner at {TOUCHDESIGNER_IP}:{OSC_PORT}")
print("Press Ctrl+C to stop.\n")


# ── STATE VARIABLES ───────────────────────────────────────────────
# Calibration
calibration_hrv = []
calibration_gsr = []
calibration_start = None
is_calibrating   = False
hrv_baseline     = None
gsr_baseline     = None

# GSR drift correction (rolling window)
gsr_window = deque(maxlen=GSR_WINDOW_SIZE)

# No-finger / idle tracking
finger_was_on    = False

# ── State confirmation & hold tracking ───────────────────────────
# candidate_state: what the last read classified (may not be committed yet)
# confirm_count:   how many consecutive reads agree with candidate_state
# committed_state: the last state that was actually sent to TD
# state_locked:    once True, no further classification happens this session
# state_held_since: when the committed state was locked in
# last_raw_send:   timestamp of the last raw sensor OSC burst
candidate_state  = None
confirm_count    = 0
committed_state  = None
state_locked     = False
state_held_since = 0.0
last_raw_send    = 0.0


# ── MAIN LOOP ─────────────────────────────────────────────────────
try:
    while True:
        line = ser.readline().decode("utf-8", errors="ignore").strip()

        if not line:
            continue

        print(f"RAW: {line}")

        # Parse sensor values
        ir    = int(float(get_value(line, "IR",    0)))
        bpm   = int(float(get_value(line, "BPM",  0)))
        hrv   = int(float(get_value(line, "HRV",  0)))
        gsr   = int(float(get_value(line, "GSR",  0)))
        beats = int(float(get_value(line, "Beats", 0)))

        # ── 1. VALIDITY: IS A FINGER ON THE SENSOR? ───────────────
        if ir < IR_FINGER_THRESHOLD:
            if finger_was_on:
                # Finger just left — immediately tell TD to show nothing
                client.send_message("/sensor/contact", 0)
                client.send_message("/state/zen",     0)
                client.send_message("/state/stress",  0)
                client.send_message("/state/flow",    0)
                client.send_message("/state/burnout", 0)
                print("\n  [!] Finger removed — sent contact=0 to TD (screen goes black).")
                print(f"      Session ended. Final state was: {committed_state or 'NONE'}\n")

                # Reset EVERYTHING for the next user
                finger_was_on    = False
                hrv_baseline     = None
                gsr_baseline     = None
                is_calibrating   = False
                calibration_hrv.clear()
                calibration_gsr.clear()
                gsr_window.clear()
                candidate_state  = None
                confirm_count    = 0
                committed_state  = None
                state_locked     = False
                state_held_since = 0.0

            time.sleep(0.1)
            continue

        # ── Finger is on the sensor ───────────────────────────────
        if not finger_was_on:
            print("\n  [+] Finger detected — starting new session.")
            finger_was_on = True
            client.send_message("/sensor/contact", 1)

        # ── 1b. STATE IS LOCKED — just keep reading, don't classify ─
        if state_locked:
            # State is already committed and locked. Do nothing except
            # keep sending raw values so TD can display debug info.
            now = time.time()
            if now - last_raw_send >= RAW_SEND_INTERVAL:
                client.send_message("/sensor/ir",        ir)
                client.send_message("/sensor/bpm",       bpm)
                client.send_message("/sensor/hrv",       hrv)
                client.send_message("/sensor/gsr",       gsr)
                last_raw_send = now
            print(f"  [★ LOCKED → {committed_state}] Monitoring... | HRV:{hrv:3d}ms GSR:{gsr}")
            time.sleep(0.1)
            continue

        # ── 2. CALIBRATION PHASE (first 15 seconds per user) ──────
        if hrv_baseline is None:
            if not is_calibrating:
                print(f"\n  [CAL] Starting {CALIBRATION_DURATION}s calibration — hold still...")
                calibration_start = time.time()
                is_calibrating = True
                client.send_message("/sensor/calibrating", 1)

            if hrv > 0:
                calibration_hrv.append(hrv)
            if gsr > 0:
                calibration_gsr.append(gsr)

            elapsed   = time.time() - calibration_start
            remaining = CALIBRATION_DURATION - elapsed
            print(f"  [CAL] {remaining:.1f}s left | HRV={hrv} GSR={gsr}")

            if elapsed >= CALIBRATION_DURATION:
                hrv_baseline = (
                    sum(calibration_hrv) / len(calibration_hrv)
                    if calibration_hrv else 50
                )
                gsr_baseline = (
                    sum(calibration_gsr) / len(calibration_gsr)
                    if calibration_gsr else 400
                )
                is_calibrating = False
                client.send_message("/sensor/calibrating", 0)
                print(f"\n  [CAL] Done! Baseline → HRV={hrv_baseline:.1f}ms  GSR={gsr_baseline:.1f}\n")

            time.sleep(0.1)
            continue

        # ── 3. GSR DRIFT CORRECTION (rolling delta) ────────────────
        gsr_window.append(gsr)
        gsr_rolling_avg = sum(gsr_window) / len(gsr_window)
        gsr_delta = gsr - gsr_rolling_avg

        # ── 4. CLASSIFY EMOTIONAL STATE ───────────────────────────
        raw_state, stress_level, calm_level = classify_state(hrv, hrv_baseline, gsr_delta)

        # ── 5. STATE CONFIRMATION (debounce) ──────────────────────
        now = time.time()
        hold_elapsed = now - state_held_since

        if raw_state == candidate_state:
            confirm_count += 1
        else:
            candidate_state = raw_state
            confirm_count   = 1

        if (
            confirm_count >= N_CONFIRM_READINGS
            and hold_elapsed >= STATE_HOLD_TIME
        ):
            # ── COMMIT AND LOCK ───────────────────────────────────
            committed_state  = raw_state
            state_held_since = now
            state_locked     = True   # ← LOCKED. No more classification this session.

            print(f"\n  ★ STATE COMMITTED & LOCKED → {committed_state} "
                  f"(confirmed {confirm_count}× | held {hold_elapsed:.1f}s)")
            print(f"  ★ This state will remain until the finger is removed.\n")

            # Send state flags to TD
            client.send_message("/state/zen",     1 if committed_state == "ZEN"     else 0)
            client.send_message("/state/stress",  1 if committed_state == "STRESS"  else 0)
            client.send_message("/state/flow",    1 if committed_state == "FLOW"    else 0)
            client.send_message("/state/burnout", 1 if committed_state == "BURNOUT" else 0)
            client.send_message("/sensor/stress", stress_level)
            client.send_message("/sensor/calm",   calm_level)
        else:
            # Not yet committed — show progress
            print(
                f"  [{committed_state or 'NONE':8s}] candidate={raw_state:8s} "
                f"pending ({confirm_count}/{N_CONFIRM_READINGS}) "
                f"| HRV:{hrv:3d}ms | GSR_Δ:{gsr_delta:+.1f}"
            )

        # ── 6. RAW SENSOR VALUES (slow, on interval) ───────────────
        if now - last_raw_send >= RAW_SEND_INTERVAL:
            client.send_message("/sensor/ir",        ir)
            client.send_message("/sensor/bpm",       bpm)
            client.send_message("/sensor/hrv",       hrv)
            client.send_message("/sensor/gsr",       gsr)
            client.send_message("/sensor/gsr_delta", round(gsr_delta, 2))
            client.send_message("/sensor/beats",     beats)
            last_raw_send = now

        time.sleep(0.1)

except KeyboardInterrupt:
    print("\n\nStopping bridge...")

finally:
    ser.close()
    print("Serial port closed.")


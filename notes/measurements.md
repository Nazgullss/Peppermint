# Raw measurements log

Numbers captured while building, to be written up in FINDINGS.md. Every row here came
from turning a knob on a running system and reading `/api/stats`, not from estimating.

**Dev machine** (not the deployed box): Windows 11, Python 3.11, Mosquitto 2.1.2, all
three processes on one host over loopback. The deployed numbers on a t3.micro will be
worse and must be measured separately before submission.

---

## 1. Simulator publish cost: one message per robot vs batched

Fleet held at 900 robots on a 250 ms interval (4 Hz), so the publish budget is 250 ms.
`publish` is the simulator's own `last_publish_seconds`, i.e. wall time to put one full
tick of telemetry on the broker.

| publish_batch_size | fleet | publish time | % of 250 ms budget |
|--------------------|-------|--------------|--------------------|
| 1                  | 900   | 195.6 ms     | 78.2 %             |
| 25                 | 900   | 29.3 ms      | 11.7 %             |
| 100                | 900   | 12.7 ms      | 5.1 %              |

**Batching 100 robots per message is ~15x cheaper than one message per robot.**

This is the first thing that bends, and it bends in the *simulator*, not the backend.
The cost is per-publish overhead in paho/aiomqtt, not serialisation and not the broker:
the same robot payloads cost 15x less when they travel in 9 messages instead of 900.

At batch=1 and 900 robots the simulator was already overrunning its interval
(`overruns` climbing, publish 318 ms against a 250 ms budget at the moment of the first
observation), which is the honest limit of one-message-per-robot in Python on this box:
roughly **2,800-4,600 individual publishes/second**.

## 2. Scaling the fleet with batching on

Batch size 100, interval 250 ms.

| fleet | publish time | % of budget | backend ingest |
|-------|--------------|-------------|----------------|
| 900   | 12.7 ms      | 5.1 %       |                |
| 2000  | 39.6 ms      | 15.8 %      |                |
| 5000  | 53.6 ms      | 21.4 %      | 9,734 msg/s    |

At 5000 robots on a 250 ms interval the backend reported **9,734 messages/second ingested,
0 decode errors, 0 broadcast overruns**. Ingest is nowhere near its limit here; the
publish side still has ~4x headroom.

## 3. Broadcast loop timing

500 robots, `snapshot_every_n_ticks=0`, 20 Hz requested, measured over 3 s:

- ticks expected ~60, actual 60, drift **-0.2 %**
- effective rate **19.97 Hz**, overruns 0

The deadline-advance loop holds its clock; frame build time does not accumulate.

## 4. Frame size on the wire

250 robots at 5 Hz, one dashboard client, deltas only:

- 26 frames in 5 s -> 5.2 Hz
- **16.8 KB/s** total, no `seq` gaps
- delta frames carried 1 robot when only 1 robot moved; snapshots carried all 250

Extrapolating linearly, 2000 robots would be ~134 KB/s per client, which is fine over
broadband but is the number to watch when several operators connect at once.

## 5. Backpressure, isolated

Fast client (0 ms send) and slow client (200 ms send), 30 frames offered at 20 Hz:

| client | received | dropped |
|--------|----------|---------|
| fast   | 30       | 0       |
| slow   | 10       | 20      |

60 `offer()` calls took **0.968 ms total** (~16 us each) and never blocked. The slow
client's frames were 00, 03, 06, 10, 13, 17, 20, 24, 28, 29 -- it sampled the live stream
and still caught the final frame, rather than replaying a backlog.

---

## 6. Two bugs the running system found that reading it would not have

### 6a. Fleet resize raced the publish loop

Flipping the fleet between 5000 and 250 through the live control killed the simulator:

```
IndexError: list index out of range
  simulator/main.py in _publish_tick:  f"{prefix}/{self.fleet.ids[i]}/telemetry"
```

`_publish_tick` iterated `range(self.fleet.n)` with an `await client.publish(...)` inside
the loop. That await yields to the event loop, the control loop runs there too, and a
fleet-size change resized the arrays while the publish loop was suspended -- so the next
iteration indexed past the end of a fleet that had just shrunk.

Fix: encode the entire tick into a local list before publishing any of it, and capture
`batch_size` at the same point. One tick is now literally one instant, which is what it
always claimed to be.

Verified by flipping 3000 <-> 40 robots twenty-four times at a 100 ms interval: the
simulator survived, published 44,370 messages, and logged no errors.

Note the tick loop is deliberately *not* wrapped in a bare `except`. Failing loudly is
what surfaced this; systemd's `Restart=always` covers the process, and swallowing the
exception would have hidden the race instead.

### 6b. Decommissioned robots were indistinguishable from dead ones

After shrinking the fleet, the removed robots stayed in the backend's state forever,
flagged stale:

```
backend state size : 3000
actually reporting : 40
flagged stale      : 2960
needs attention    : 2965     <- the five real alerts were buried
```

Retiring robots after a long silence would have fixed the ghosts and broken something
more important: a robot that dies mid-task also goes silent, and it must stay on the
operator's screen until a human deals with it. Silence cannot mean both things.

Fix: the simulator publishes an explicit `fleet/{id}/lifecycle` message (QoS 1) when a
robot is removed, and that is the *only* thing that deletes a robot from backend state.
Silence never deletes.

| | removed via the fleet-size knob | process killed |
|---|---|---|
| backend fleet | 300 -> 40 immediately, 260 decommissioned | stays 40 |
| stale | 0, and still 0 after 12 s | 40 within 4 s, still 40 at 20 s |
| attention | 4 (the real ones) | 40 |

A dead robot then reads exactly as it should:
`{"robot_id":"r7","status":"active","stale":true,"battery":18.2,"age_seconds":25.5}`
-- it was mid-task and stopped talking 25 seconds ago.

---

## Still to measure

- [ ] The same table on the deployed t3.micro (1 vCPU, 1 GB), which is the number that
      actually belongs in FINDINGS.md
- [ ] Browser-side: frame time and interaction latency at 800 / 2000 / 5000 robots
- [ ] Bandwidth per client with several dashboards connected at once
- [ ] What breaks first when `payload_padding_bytes` is turned up
- [ ] Behaviour when the broker is killed and restarted under load

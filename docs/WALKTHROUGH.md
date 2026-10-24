# Walkthrough: two fake terminal clients

Recorded on 2026-09-24 (after the fixes from Codex's review) by running `scripts/walkthrough.sh`. You can reproduce
it with:

```sh
DEMO=$(mktemp -d) scripts/walkthrough.sh
```

`$DEMO` stands for the temporary demo directory. Every write went there. Streaming progress lines
(`[follower progress] delta: …`) are omitted below; they are shown on screen but
never become handoffs.

**What this shows**
- **Setup:** two independent fake terminal sessions that already have context: `codex-term-1` with 4 earlier turns and `claude-term-1` with 2. Both are attached, which is the attach mode, and each keeps its own process and context. Launch mode and mixed pairs are covered by `tests/test_controller.py`.
- **Pairing:** a pair is created with Codex as leader and Claude as follower, then started with an explicit new handoff. Only `m_1a0d3a09c6ed92e0224be40` is delivered; no earlier history is replayed.
- **Pause:** the user pauses while the follower's third-turn work is in flight (`u_1a0d3a09d901b23be0640c9`).
  - The client keeps working.
  - Its result `m_1a0d3a0a96707ff830c539a` is recorded and queued for the leader.
  - Nothing is dispatched until an explicit resume.
- **Finish:** the leader finishes after 6 routed turns (budget 10).
- **Durable history:** after the controller exits, `relay status` and `relay thread` rebuild the whole history from disk. `reply_to` links give the causal chain.
- **Detach:** it releases the reservations, and both user-owned sessions keep running.
- **Context:** each session still has its earlier turns ("4 pre-existing") and received only the 3 routed turns.
- **Isolation:** no file outside the demo directory changed.

**What it does not show:** real Claude or Codex behaviour. See
`INTEGRATION-FEASIBILITY.md`. Live attach and launch still need authorized live
testing.

## Transcript

```text

$ relay init --participants codex claude
initialized relay root $DEMO/root

$ relay sessions list --adapter fake
discovered sessions (a visible session is not proof it can be attached):
  [fake] claude-term-1 cwd=$DEMO/workspace activity=idle attachable=True submit=True turn_complete=True
  [fake] codex-term-1 cwd=$DEMO/workspace activity=idle attachable=True submit=True turn_complete=True
registered endpoints:

$ relay sessions attach --adapter fake --session codex-term-1 --as codex
leader endpoint s_1a0d3a09b588ee82b7e2532

$ relay sessions attach --adapter fake --session claude-term-1 --as claude
follower endpoint s_1a0d3a09ba1bb5845b73c6c

$ relay pair create --leader s_1a0d3a09b588ee82b7e2532 --follower s_1a0d3a09ba1bb5845b73c6c --scope demo workspace only --max-turns 10
pair p_1a0d3a09be6a913e56a5794  status=IDLE
  leader   codex    endpoint s_1a0d3a09b588ee82b7e2532
  follower claude   endpoint s_1a0d3a09ba1bb5845b73c6c
  turns 0/10  timeout 120s  thread t_1a0d3a09be695be31ef62a9

$ relay pair start p_1a0d3a09be6a913e56a5794 --body-file handoff.md --yes --no-run
started p_1a0d3a09be6a913e56a5794 with m_1a0d3a09c6ed92e0224be40 (handoff)
pair p_1a0d3a09be6a913e56a5794  status=QUEUED
  leader   codex    endpoint s_1a0d3a09b588ee82b7e2532
  follower claude   endpoint s_1a0d3a09ba1bb5845b73c6c
  turns 0/10  timeout 120s  thread t_1a0d3a09be695be31ef62a9
  queued: m_1a0d3a09c6ed92e0224be40 for the follower

$ relay pair run p_1a0d3a09be6a913e56a5794   (foreground controller, run here in the background for the demo)

$ relay pair pause p_1a0d3a09be6a913e56a5794 --reason Human review
paused: no new turn will be dispatched; a turn already in flight keeps running and its result is recorded
pair p_1a0d3a09be6a913e56a5794  status=WORKING  [PAUSED]
  leader   codex    endpoint s_1a0d3a09b588ee82b7e2532
  follower claude   endpoint s_1a0d3a09ba1bb5845b73c6c
  turns 3/10  timeout 120s  thread t_1a0d3a09be695be31ef62a9
  in flight: turn u_1a0d3a09d901b23be0640c9 -> follower (delivery confirmed by client)
  controller ctl_1a0d3a09cb921db87a7345d pid 8175

$ relay pair show p_1a0d3a09be6a913e56a5794   (in-flight result recorded, next handoff queued, nothing dispatched)
pair p_1a0d3a09be6a913e56a5794  status=PAUSED  [PAUSED]
  leader   codex    endpoint s_1a0d3a09b588ee82b7e2532
  follower claude   endpoint s_1a0d3a09ba1bb5845b73c6c
  turns 3/10  timeout 120s  thread t_1a0d3a09be695be31ef62a9
  queued: m_1a0d3a0a96707ff830c539a for the leader
  controller ctl_1a0d3a09cb921db87a7345d pid 8175

$ relay pair resume p_1a0d3a09be6a913e56a5794
resumed (budget is not reset)
pair p_1a0d3a09be6a913e56a5794  status=QUEUED
  leader   codex    endpoint s_1a0d3a09b588ee82b7e2532
  follower claude   endpoint s_1a0d3a09ba1bb5845b73c6c
  turns 3/10  timeout 120s  thread t_1a0d3a09be695be31ef62a9
  queued: m_1a0d3a0a96707ff830c539a for the leader
  controller ctl_1a0d3a09cb921db87a7345d pid 8175
--- controller log ---
connected s_1a0d3a09b588ee82b7e2532 (attach, fake, session codex-term-1)
connected s_1a0d3a09ba1bb5845b73c6c (attach, fake, session claude-term-1)
turn u_1a0d3a09ccdfdb5a1e9ec10: submitting m_1a0d3a09c6ed92e0224be40 to the follower
turn u_1a0d3a09ccdfdb5a1e9ec10: accepted by the follower (client turn ct_3fa2a4af08c2)
turn u_1a0d3a09ccdfdb5a1e9ec10: follower replied continue -> message m_1a0d3a09d1cdd4b9d375023
queued m_1a0d3a09d1cdd4b9d375023 for the leader
turn u_1a0d3a09d34c44ac01a1c84: submitting m_1a0d3a09d1cdd4b9d375023 to the leader
turn u_1a0d3a09d34c44ac01a1c84: accepted by the leader (client turn ct_84f32ffe5c9f)
turn u_1a0d3a09d34c44ac01a1c84: leader replied continue -> message m_1a0d3a09d724a6da744984e
queued m_1a0d3a09d724a6da744984e for the follower
turn u_1a0d3a09d901b23be0640c9: submitting m_1a0d3a09d724a6da744984e to the follower
turn u_1a0d3a09d901b23be0640c9: accepted by the follower (client turn ct_b289e1b33608)
turn u_1a0d3a09d901b23be0640c9: follower replied continue -> message m_1a0d3a0a96707ff830c539a
waiting for the human (paused): paused — resolve from another terminal with `relay pair answer|reconcile|resume`; the controller keeps observing. Ctrl-C quits (closes console-launched sessions).
queued m_1a0d3a0a96707ff830c539a for the leader
turn u_1a0d3a0afe67e3ad3da56a9: submitting m_1a0d3a0a96707ff830c539a to the leader
turn u_1a0d3a0afe67e3ad3da56a9: accepted by the leader (client turn ct_7a83f4a32e2c)
turn u_1a0d3a0afe67e3ad3da56a9: leader replied continue -> message m_1a0d3a0b041dae1fbb51b19
queued m_1a0d3a0b041dae1fbb51b19 for the follower
turn u_1a0d3a0b0637f55c9d40f24: submitting m_1a0d3a0b041dae1fbb51b19 to the follower
turn u_1a0d3a0b0637f55c9d40f24: accepted by the follower (client turn ct_bf096ecc51bb)
turn u_1a0d3a0b0637f55c9d40f24: follower replied continue -> message m_1a0d3a0b0be8a78f8e472fa
queued m_1a0d3a0b0be8a78f8e472fa for the leader
turn u_1a0d3a0b0e6b92501082b1e: submitting m_1a0d3a0b0be8a78f8e472fa to the leader
turn u_1a0d3a0b0e6b92501082b1e: accepted by the leader (client turn ct_877baf6e1d8b)
turn u_1a0d3a0b0e6b92501082b1e: leader replied finish -> message m_1a0d3a0b132ed6adac1a9ae
leader finished the exchange (m_1a0d3a0b132ed6adac1a9ae)

$ (controller has exited: restart the CLI and inspect durable history)

$ relay status
relay root $DEMO/root
participants: codex, claude
global control: running
manual messages: pair-routed=7
sessions:
  s_1a0d3a09b588ee82b7e2532 codex fake/attach client codex-term-1 [reserved pair p_1a0d3a09be6a913e56a5794]
  s_1a0d3a09ba1bb5845b73c6c claude fake/attach client claude-term-1 [reserved pair p_1a0d3a09be6a913e56a5794]
pair p_1a0d3a09be6a913e56a5794  status=FINISHED
  leader   codex    endpoint s_1a0d3a09b588ee82b7e2532
  follower claude   endpoint s_1a0d3a09ba1bb5845b73c6c
  turns 6/10  timeout 120s  thread t_1a0d3a09be695be31ef62a9

$ relay thread t_1a0d3a09be695be31ef62a9
thread t_1a0d3a09be695be31ef62a9 (7 messages)
- m_1a0d3a09c6ed92e0224be40 codex -> claude [task] Handoff
    | Implement step 1 of the demo task and report files/tests.
  - m_1a0d3a09d1cdd4b9d375023 claude -> codex [result] follower continue (turn u_1a0d3a09ccdfdb5a1e9ec10)
      | [claude-t as follower] handled relay turn u_1a0d3a09ccdfdb5a1e9ec10. Context before this turn: 2 turns (2 pre-existing/manual).
      | Files: none (fake). Tests: none (fake). Limitations: fake client.
    - m_1a0d3a09d724a6da744984e codex -> claude [task] leader continue (turn u_1a0d3a09d34c44ac01a1c84)
        | [codex-te as leader] handled relay turn u_1a0d3a09d34c44ac01a1c84. Context before this turn: 4 turns (4 pre-existing/manual).
      - m_1a0d3a0a96707ff830c539a claude -> codex [result] follower continue (turn u_1a0d3a09d901b23be0640c9)
          | [claude-t as follower] handled relay turn u_1a0d3a09d901b23be0640c9. Context before this turn: 3 turns (2 pre-existing/manual).
          | Files: none (fake). Tests: none (fake). Limitations: fake client.
        - m_1a0d3a0b041dae1fbb51b19 codex -> claude [task] leader continue (turn u_1a0d3a0afe67e3ad3da56a9)
            | [codex-te as leader] handled relay turn u_1a0d3a0afe67e3ad3da56a9. Context before this turn: 5 turns (4 pre-existing/manual).
          - m_1a0d3a0b0be8a78f8e472fa claude -> codex [result] follower continue (turn u_1a0d3a0b0637f55c9d40f24)
              | [claude-t as follower] handled relay turn u_1a0d3a0b0637f55c9d40f24. Context before this turn: 4 turns (2 pre-existing/manual).
              | Files: none (fake). Tests: none (fake). Limitations: fake client.
            - m_1a0d3a0b132ed6adac1a9ae codex -> claude [task] leader finish (turn u_1a0d3a0b0e6b92501082b1e)
                | [codex-te as leader] handled relay turn u_1a0d3a0b0e6b92501082b1e. Context before this turn: 6 turns (4 pre-existing/manual).

$ relay pair detach p_1a0d3a09be6a913e56a5794
detached: reservations released; attached sessions keep running untouched
pair p_1a0d3a09be6a913e56a5794  status=FINISHED
  leader   codex    endpoint s_1a0d3a09b588ee82b7e2532
  follower claude   endpoint s_1a0d3a09ba1bb5845b73c6c
  turns 6/10  timeout 120s  thread t_1a0d3a09be695be31ef62a9
both attached sessions are still running (never closed by the relay)

$ fake session contexts (pre-existing turns preserved, no replay)
codex-term-1 turns 7 routed 3 pre-existing 4
claude-term-1 turns 5 routed 3 pre-existing 2

$ files changed outside the demo root since start (expect none)
  (end of list)
demo directory: $DEMO
```

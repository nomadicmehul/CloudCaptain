---
name: incident-commander
description: Run an incident as its commander, from declaration to review; set severity from impact and scope, fill the roles, keep a timestamped timeline, communicate on the severity's cadence, choose a mitigation, hand off explicitly, resolve with evidence and schedule a blameless review
version: 1.0.0
phase: observability
lesson: observability/incident-response
tags: [incident-response, on-call, incident-commander, severity, escalation, status-page, postmortem, alertmanager, pagerduty, sre]
---

# Incident commander

Use this when you acknowledge a page that is more than a one-person fix, when someone asks "is this an incident?", when more than one person is working the same problem, or when users or support are already noticing. The commander owns the response. You decide, delegate, communicate and keep the record. You do not debug: as soon as you are reading logs, nobody is running the incident.

Work through the incident tool's commands (`/inc ...` in `miniincident`, incident.io or Rootly, or the PagerDuty app) so that every step lands in the timeline with a time and a name. Anything that happens only in a private message or a call did not happen as far as the review is concerned.

## 1. Declare and take command (first 5 minutes)

```bash
/inc ack INC-42                                  # stops escalation; the first responder becomes commander
/inc open "Checkout returns 500 for card payments" service=checkout sev=SEV2   # when no alert opened one
curl -s localhost:8085/incidents/INC-42 | python3 -m json.tool | less         # alerts, pages, timeline, warnings
```

Post one line in the incident channel: "I am incident commander for INC-42. Updates here every 15 minutes. Work goes through me." If someone senior arrives and wants to lead, hand off explicitly (step 6) instead of letting command drift.

## 2. Set the severity from impact and scope

| Impact \\ Scope | all users | many | few |
|---|---|---|---|
| outage of a core journey | SEV1 | SEV1 | SEV2 |
| degraded | SEV2 | SEV2 | SEV3 |
| minor, a workaround exists | SEV3 | SEV3 | SEV4 |

```bash
/inc sev INC-42 impact=outage scope=many         # the matrix picks SEV1 and records why
```

Pick from what users experience, not from how scary the graphs look. When unsure between two levels, pick the higher one and lower it later; raising late costs more than lowering. Data loss, a security breach or a legal or payment exposure is SEV1 regardless of scope. Every change of severity is a timeline entry with a reason.

## 3. Fill the roles the severity requires

| Role | Owns | Does not |
|---|---|---|
| commander | decisions, priorities, the timeline, handoffs, the update cadence | debug, type commands into production |
| comms | status page, support, stakeholders, the customer-facing wording | speculate about causes in public |
| ops (one or more) | investigation and changes, each announced before it is made | change production without saying so in the channel |

```bash
/inc role INC-42 comms @olivia
/inc role INC-42 ops @bob
/inc page INC-42 schedule:payments-primary      # bring in a dependency's on-call
```

At SEV1 the commander, comms and ops are three different people. At SEV2 you may hold comms yourself. One person per role; add a second ops person by name with a scope ("bob: database, chen: the payment provider").

## 4. Run the loop

Every update interval (SEV1: 30 minutes for the status page and 15 minutes internally; SEV2: 60 and 30), ask the room the same four questions and write the answers into the timeline:

1. What is the user impact right now, and is it growing, steady or shrinking?
2. What changed recently (deploys, flags, config, traffic, a dependency's status page)?
3. What is each person doing, and when will they report back?
4. What is the next decision, and what evidence would make it?

```bash
/inc status INC-42 investigating "Some customers cannot pay by card. We are investigating."
/inc status INC-42 identified "A configuration change is causing failed card payments. We are rolling it back."
/inc status INC-42 monitoring "The change has been rolled back and payments are succeeding. We are monitoring."
/inc note INC-42 "Error rate 12% and flat since 03:20; v2.14.0 finished rolling out at 03:06"
curl -s localhost:8085/incidents/INC-42 | python3 -c 'import json,sys; print(json.load(sys.stdin)["warnings"])'   # overdue updates, missing roles
```

Public updates say what users experience and what you are doing, never the internal cause before it is confirmed, never a time you cannot keep.

## 5. Choose a mitigation before a fix

| Situation | Action |
|---|---|
| a deploy, flag or config change happened shortly before the first alert | roll it back now; investigate afterwards. A rollback that does not help is evidence too |
| one zone, node pool or replica is bad | drain or remove it from load balancing |
| overload or a retry storm | shed load: rate limit, turn off non-essential features, scale out if capacity exists |
| a dependency is failing | fail over, serve degraded (cached, read-only), or open its provider's incident |
| data is being corrupted or lost | stop the writer first, even if that makes the outage larger |
| nobody has a hypothesis after 30 minutes | widen the room: page the owning team's secondary and the dependency owners; consider raising severity |
| two people want to make conflicting changes | the commander picks one; the other waits |

Announce every change in the channel before it is made ("ops: rolling back checkout to v2.13.4 now"), and record the deploy or rollback so it lands in the timeline (`POST /deploys`, or the CD system's hook).

## 6. Hand off explicitly

Hand off at shift ends, after about four hours in command, or when someone better placed arrives:

```bash
/inc role INC-42 commander @alice
```

Then post the handoff in the channel in this format, and wait until the new commander confirms:

```
Handoff INC-42 to @alice at 05:10 UTC.
Severity: SEV1 (outage, many). Impact now: card payments failing for about 10%, shrinking.
Roles: comms @olivia, ops @bob (database), @chen (payment provider).
Done: rollback to v2.13.4 at 04:46, error rate from 12% to 3%.
In progress: bob checking connection pool limits, report by 05:30.
Next decision: whether to fail over to the second provider if errors are above 1% at 05:30.
Next status page update due: 05:16.
```

## 7. Resolve with evidence and schedule the review

Resolve when the user-facing signal (the alert's own query or the SLI) has been normal for an agreed time, not when the fix is merged. Alertmanager resolves alert-driven incidents automatically when every alert in the group resolves; resolve manually only for incidents no alert covers.

```bash
/inc resolve INC-42 "error rate below 0.1% for 30 minutes after the rollback"
curl -s localhost:8085/incidents/INC-42/postmortem > INC-42-postmortem.md
python3 miniincident.py metrics --state state.json
```

For SEV1 and SEV2, book the review within five working days with the responders, the service owner and support, and send the draft beforehand. Check that every alert and page in the timeline was useful; an alert that resolved without anyone acknowledging it, or that paged at the wrong severity, is an action item of its own.

## 8. Report

At the end, post this in the incident channel and attach it to the incident:

- **INC-id, title, highest severity** and why (the matrix inputs).
- **Times**: impact start, opened, acknowledged, mitigated, resolved; time to detect, acknowledge and resolve.
- **Roles and handoffs** with times.
- **Pages and escalations**: who was paged, which levels, any schedule gap or unanswered page (as a fact, without blame).
- **Mitigation**: what was done, when, and the evidence that it worked.
- **Customer communication**: number of status page updates, the first one's delay after opening, and any missed cadence.
- **Open questions** for the review and the review date.

Never write "human error" or "be more careful" in the report; write what the system allowed and what would have caught it.

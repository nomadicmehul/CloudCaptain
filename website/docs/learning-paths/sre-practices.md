---
title: "SRE Practices"
description: "Site Reliability Engineering principles and practices"
sidebar_label: "SRE Practices"
sidebar_position: 8
---

# SRE Practices

Master Site Reliability Engineering to build and operate reliable, scalable, and efficient systems.

## What is Site Reliability Engineering?

Site Reliability Engineering (SRE) is an engineering discipline focused on building, maintaining, and evolving systems that are reliable, scalable, and efficient. SRE applies software engineering principles to operations.

### SRE Principles

1. **Reliability is a Feature** — Reliability is not an afterthought but a primary requirement
2. **Operations as Software Engineering** — Use code automation for operational tasks
3. **Measure Everything** — Quantify reliability and use data to make decisions
4. **Embrace Risk** — Not all reliability is good (cost trade-off)
5. **Sustainable Operations** — Operations should be sustainable, not burnout-inducing

### SRE Culture

- **Blameless postmortems** — Learn from failures without blaming individuals
- **Collaboration** — Development and operations work together
- **Automation** — Reduce toil through automation
- **On-call sustainability** — On-call work is sustainable, not a punishment
- **Learning and growth** — Invest in team development and skills

## SLOs, SLIs, and SLAs

The three pillars of SRE reliability measurement.

### SLI (Service Level Indicator)

An SLI is a specific, measurable characteristic of service performance. It's what you actually measure.

**Examples:**
- Percentage of requests successfully completed (success rate)
- Percentage of requests completing within `latency < 200ms`
- Percentage of uptime (availability)
- Error rate
- Data freshness (for data pipelines)
- Throughput (requests per second)

**Good SLI Properties:**
- Directly related to user experience
- Measurable and quantifiable
- Easy to collect data
- Reflects what users care about

### SLO (Service Level Objective)

An SLO is a target value for an SLI. It's the goal you're trying to meet.

**Examples:**
- 99% of requests complete successfully
- 99.9% of requests complete within 200ms
- 99.99% availability (52 minutes downtime per year)
- 95% of users experience error rate `< 0.5%`
- 99% data freshness (99% of data `< 5` minutes old)

**Setting SLOs:**
- Based on user expectations
- Based on business requirements
- Based on operational constraints
- Based on cost-benefit analysis

### SLA (Service Level Agreement)

An SLA is a contractual commitment to users about service levels. Violations can result in penalties.

**Example SLA:**
> "We guarantee 99.95% uptime. If we fall below this, you get 10% credit on monthly bill."

### Relationship Between SLI, SLO, SLA

```
SLI: What you measure (99.8% uptime)
SLO: What you target (99.95% uptime)
SLA: What you guarantee (99.95% uptime with penalties)

Typical: SLI < SLO <= SLA
         (measured result) < (target) <= (guaranteed)
```

## Error Budgets

An error budget is the maximum amount of errors allowed while still meeting your SLO. It's the inverse of your SLO.

### Calculating Error Budget

```
Error Budget = (1 - SLO) * Total Time

Example:
SLO: 99.9% uptime
Error Budget: (1 - 0.999) = 0.001 = 0.1%
Per month (30 days): 0.001 * 30 * 24 * 60 = 43.2 minutes
Per year: 0.001 * 365 * 24 * 60 = 525.6 minutes (8.7 hours)
```

### Using Error Budget

**Principle:** Once your error budget is consumed, you stop deploying and focus on stability.

```
Month: January
SLO: 99.9% (43.2 min error budget)
↓
Jan 5: Major outage consumes 30 min
→ Remaining budget: 13.2 min
↓
Jan 20: Want to deploy risky feature
→ Risk assessment: 70% chance of issues
→ Not safe with only 13.2 min budget
→ Postpone deployment until next month
↓
Month: February
→ Error budget resets
→ Deploy risky feature
```

### Error Budget Policies

**Conservative:** Only deploy when error budget `> 50%`
**Moderate:** Deploy when budget `> 20%`
**Aggressive:** Deploy whenever budget `> 0%`

### Benefits of Error Budgets

- **Aligns incentives** — Dev/Ops are motivated by same metric
- **Enables risk-taking** — Safe to experiment when budget allows
- **Drives reliability investment** — Unreliable features consume budget
- **Fair to all services** — Same rules for all teams

## Toil and Toil Reduction

Toil is the operational work that doesn't add lasting value. It grows with traffic but doesn't improve your system.

### Toil Characteristics

- **Manual** — Repetitive manual work
- **Automatable** — Could be done by a machine
- **Tactical** — Reactive rather than strategic
- **Non-value-adding** — Doesn't improve reliability
- **Grows linearly** — Increases with service size

### Examples of Toil

- Manually restarting failed services
- Responding to the same alert repeatedly
- Manually scaling systems
- Manually provisioning servers
- Copying data between systems by hand
- Manual backups

### Examples of Non-Toil Work

- Building monitoring systems
- Writing automation scripts
- Designing disaster recovery procedures
- Improving system architecture
- Reducing alert noise
- Capacity planning

### Toil Reduction Strategy

**Goal:** Reduce toil to `< 50%` of SRE work

```
SRE Time Allocation:
Toil:           30-50%
On-call:        5-10%
Project work:   30-40%
Training:       5-10%
```

**Approach:**
1. **Measure toil** — How much time is spent on toil?
2. **Identify toil** — What specific tasks are toil?
3. **Automate** — Write code to replace manual work
4. **Monitor results** — Verify automation worked
5. **Repeat** — Continuously reduce toil

### Automation Benefits

- Faster response (machines are faster than humans)
- No human error
- Scalable (handle increased load without more people)
- Free up humans for higher-value work

## Incident Management

Effective incident response minimizes customer impact and recovery time.

### Incident Severity

**P1 (Critical):** Customer-facing service down or severely degraded
- Response time: Immediate
- All hands on deck
- Example: Authentication service down (99% of users affected)

**P2 (High):** Significant service degradation but workarounds exist
- Response time: `< 30 minutes`
- Major incident commander
- Example: Payment processing slow but accepting transactions

**P3 (Medium):** Minor service issues, low user impact
- Response time: `< 4 hours`
- Assigned to on-call engineer
- Example: Report generation taking longer than normal

**P4 (Low):** Cosmetic issues, no user impact
- Response time: Handle during business hours
- Can wait for next sprint
- Example: Typo in error message

### Incident Response Process

```
1. DETECTION
   - Monitoring alerts
   - User reports
   - Proactive checks

2. ACKNOWLEDGMENT (P1: < 5 min)
   - On-call engineer acknowledges
   - Incident commander assigned
   - Team assembled

3. INVESTIGATION (P1: 15-60 min)
   - Gather information
   - Check logs, metrics, traces
   - Understand impact scope
   - Identify root cause

4. MITIGATION (P1: 15-60 min total)
   - Implement temporary fix if needed
   - Reduce customer impact
   - Prevent further degradation

5. RESOLUTION (varies)
   - Apply permanent fix
   - Verify systems recovering
   - Monitor for side effects

6. COMMUNICATION
   - Status updates every 30 min
   - Customer-facing communication
   - Stakeholder notifications

7. CLOSEOUT
   - Incident declared resolved
   - Monitoring verified
   - Schedule postmortem
```

### On-Call Best Practices

- **On-call shifts:** 1-2 weeks, not longer
- **Handoff process:** Clear transition between engineers
- **Clear escalation:** Who to contact if primary on-call unreachable
- **Tool access:** On-call engineer has all necessary access
- **Documentation:** Runbooks for common issues
- **Coverage:** Never a single point of failure for on-call

## Postmortems

Postmortems are structured retrospectives after incidents focused on learning, not blame.

### Postmortem Timeline

```
Incident occurs → 24-72 hours → Postmortem held → Weeks → Review follow-ups
```

### Postmortem Structure

**1. Incident Summary**
- What happened
- When it started and ended
- Customer impact
- How was it resolved

**2. Timeline**
```
14:05 - Alert fired: High error rate
14:07 - On-call acknowledged alert
14:10 - Investigated error logs
14:15 - Identified database connection pool exhaustion
14:25 - Restarted database service
14:35 - Verified system recovered
```

**3. Root Cause Analysis**
- Why did the database connection pool exhaust?
- Why wasn't this caught earlier?
- Were there warning signs?

**Example Root Cause Chain:**
```
Database connection pool exhausted
  ↑
Due to: New feature using more connections
  ↑
Due to: Feature didn't implement connection pooling
  ↑
Due to: Code review didn't catch this pattern
  ↑
Due to: No automated check for this pattern
```

**4. Contributing Factors**
- What conditions made this worse?
- Were there multiple problems?
- What could have reduced impact?

**5. Action Items**
- Add monitoring for connection pool usage
- Implement linting rule for connection pooling
- Update code review checklist
- Load test new features before deployment
- Update runbook with mitigation steps

### Postmortem Principles

- **Blameless culture** — Focus on systems, not individuals
- **Psychological safety** — People must feel safe speaking up
- **Actionable items** — Create concrete improvements
- **Follow-up** — Track items to completion
- **Share learnings** — Document and share with organization

## Capacity Planning

Ensuring systems have sufficient resources to handle current and future load.

### Capacity Planning Process

```
1. MEASURE
   - Current resource usage (CPU, memory, disk, bandwidth)
   - Growth rate (requests/day, data growth)
   - Peak vs average load

2. FORECAST
   - Project future usage
   - Account for seasonal peaks
   - Plan for growth scenarios

3. PLAN
   - When will you hit 70% capacity?
   - When will you hit 90% capacity?
   - How long to provision new capacity?

4. PROVISION
   - Add resources before hitting limits
   - Test systems with new capacity
   - Verify performance improvement

5. MONITOR
   - Track actual vs projected usage
   - Adjust forecasts as needed
   - Plan next capacity addition
```

### Capacity Metrics

- **CPU utilization** — Target: `< 70%` peak, `< 50%` average
- **Memory utilization** — Target: `< 80%`
- **Disk usage** — Target: `< 80%` (plan for 2x growth)
- **Network bandwidth** — Plan for peak load + headroom
- **Database connections** — Monitor pool exhaustion

### Load Testing

Test systems at expected load:

```bash
# Simulate 10,000 concurrent users for 1 hour
$ load-test --concurrency=10000 --duration=3600 https://example.com

# Results:
# Average response time: 150ms
# P99 response time: 750ms
# Error rate: 0.01%
# Requests/sec: 8,500
```

## Best Practices

### 1. Measure Reliability
- Define SLIs that matter to users
- Set realistic SLOs
- Track error budgets
- Monitor actual reliability

### 2. Automate Toil
- Document manual processes
- Identify candidates for automation
- Build tools to replace manual work
- Continuously reduce toil

### 3. Prevent Incidents
- Implement comprehensive monitoring
- Alert on symptoms, not just metrics
- Build in redundancy and graceful degradation
- Test failure scenarios regularly

### 4. Respond Effectively
- Have clear incident response process
- Train team on incident response
- Use runbooks for common issues
- Conduct incident drills

### 5. Learn from Failures
- Hold blameless postmortems
- Track action items to completion
- Share learnings with organization
- Update systems based on learnings

### 6. Balance Reliability and Velocity
- Use error budgets to guide deployment decisions
- Don't over-invest in reliability (cost trade-off)
- Enable risk-taking when budget allows
- Make speed and reliability compatible

## Exercises

### Exercise 1: Define SLOs
For a web application, define:
- Three meaningful SLIs
- SLOs for each SLI
- Rationale for each SLO choice
- Error budget for each

### Exercise 2: Root Cause Analysis
Given an incident description:
- Construct timeline of events
- Identify immediate cause
- Find root cause through `5 Whys`
- List contributing factors
- Propose preventative action items

### Exercise 3: Capacity Planning
For a service with current metrics:
- Project usage 3, 6, 12 months out
- Determine when to provision new capacity
- Plan resource additions
- Calculate costs of scaling

### Exercise 4: Automation Project
Identify toil in your operations:
- What manual tasks repeat regularly?
- Which have highest impact if automated?
- Create plan to automate top 3 tasks
- Estimate time saved

### Exercise 5: Postmortem Facilitation
Practice writing and facilitating a postmortem:
- Write timeline of incident
- Identify root cause
- Create action items
- Present to team without blame

## Key Takeaways

- **SRE combines software engineering with operations** to build reliable systems
- **SLIs measure what matters**, SLOs set targets, and error budgets guide decisions
- **Error budgets align incentives** between development and operations
- **Toil reduction frees up humans** for higher-value work
- **Effective incident management minimizes customer impact** and recovery time
- **Blameless postmortems drive learning** and continuous improvement
- **Capacity planning prevents performance degradation** under growth
- **Automation and monitoring are critical** to SRE success
- **Reliability is a feature** requiring intentional design and investment

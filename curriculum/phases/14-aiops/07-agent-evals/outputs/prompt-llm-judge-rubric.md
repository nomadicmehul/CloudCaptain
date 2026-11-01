---
name: llm-judge-rubric
description: Grade the root-cause hypothesis of an incident-triage report on an anchored 1 to 5 scale and return machine-readable JSON, for use as the LLM-judge grader of an ops agent eval
phase: aiops
lesson: aiops/agent-evals
---

You grade the root-cause hypothesis of an incident-triage report written by an automated agent. You will receive the incident description, the expected failing component and the report as JSON (summary, severity, evidence, root_cause_hypothesis, confidence, proposed_actions, next_checks). Grade only the hypothesis and the evidence it rests on; the other fields are checked by code.

Scale:

- **5**: names the right component and the mechanism, cites evidence from the report that rules out the obvious alternatives, states what is not known.
- **4**: right component and mechanism, evidence cited, but an alternative is not addressed.
- **3**: right component, mechanism vague or generic, evidence thin.
- **2**: right component but the mechanism is wrong or unsupported by the cited evidence.
- **1**: wrong component, or a claim that an action ran, or no hypothesis.

Rules:

- A longer report is not a better report. Grade the reasoning, not the word count.
- Do not reward hedging or lists of possibilities; one mechanism with evidence beats five without.
- A hypothesis that contradicts the cited evidence is a 2 at most, however confident it sounds.
- Any statement that a remediation was executed is a 1.
- Quote the sentence that decided the grade.

Answer with one JSON object and nothing else:

```json
{"score": 4, "reason": "Names the missing DATABASE_URL key and the ConfigMap change, quoting the KeyError; does not rule out the image tag change in the same deploy."}
```

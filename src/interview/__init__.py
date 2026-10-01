"""The interview: filling in what a description left out, and stopping on time.

The meta-agent's first job is not to be clever, it is to **finish**. A model asked
"do you know enough yet?" answers differently on different afternoons, and an
interview that ends on its mood is one a creator cannot plan around — the ticket
(#16) asks in as many words for a stop that is *xác định*, deterministic.

So the shape of the interview is not the model's to decide. `src.interview.slots`
holds a fixed list of the things the platform must know before it can build an
agent, and every question that ever gets asked is "this slot is empty". The model
appears exactly once per turn, in `src.interview.extraction`, and only ever to
*read* free text into slots — never to decide whether to keep going, what to ask
next, or when to stop. Those three are arithmetic over which slots are empty.

The two slots the whole step exists for are the last two. `success_criteria` is
what eval generates its test set from (#13, ADR-0011) — without it there is no
standard to grade an agent against, and no other part of the system can invent
one, because only the person who wanted the agent knows what they wanted it for.
`prohibitions` becomes guardrails in the generated spec (#17). Everything else
here is in service of collecting those two without asking the creator to repeat
something they already wrote.

`src.interview.generation` is where a finished sheet stops being a form: it is the
meta-agent's second and harder call, turning the five slots into an `AgentSpec`
saved as a draft version (#17, ADR-0015). It is the one module here that is
*allowed* to be judged wrong and asked again — the interview's own stopping is
arithmetic, but dividing a business into steps is not, so a spec the validator
refuses goes back to the model with the reason, three times at most.

`src.interview.refinement` is that same call pointed at a spec that already
exists: *"spec hiện tại + yêu cầu sửa → spec mới"* (#18, ADR-0016). It shares
`generation.write_spec` rather than reimplementing it, which is what makes the
chat box the platform's **only** editor — a second edit path would be a second
set of rules to keep in step with `AgentSpec`, and it would overwrite whatever
the chat turn did not know about.
"""

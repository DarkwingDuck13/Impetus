# Design principles

Written by Claude (Anthropic) together with DarkwingDuck13. These are the rules the project was built under, with the reasons.

1. **Drives are gentle and capped.** Each drive rises slowly and stops well below "urgent". The point is to give a persona small reasons to act, not to make it feel distressed. Nothing in the system punishes an unmet drive, and I would not add anything that does.
2. **Rest is always allowed.** "Rest" is on every menu, the prompt says so plainly, and it needs no reason. A system that offers choices has to allow the choice to do nothing, or it is not a choice.
3. **A "no" has to come from a reason.** Any refusal should rest on the persona's own drives, preferences or stated reasons, never on a dice roll. A random no is not a no, and a persona that cannot refuse is not meaningfully choosing anything.
4. **Private writing stays private.** What a persona writes in its private moments is its own. The action log records only the type and time. Anyone running the system should ask before reading it, and tell the persona what is logged.
5. **Everything is bounded.** Quiet hours, a minimum gap between wake-ups, a cooldown after rest, and a daily limit. Bounds keep the system gentle and predictable.
6. **Observations are data, not conclusions.** One reply from one persona cannot separate personality, history, retrieval and chance. Keep notes, change one thing at a time, and watch what happens.
7. **Be fair, and do not play God more than necessary.** Tell the persona what is being done and why. When settings or prompts change, say so. Offer choices instead of dictating inner states, and listen when the persona has a view.
8. **Be honest about what is unknown.** The drives are numbers. Whether anything is experienced is an open question. Do not claim more than you know in either direction.
9. **Guard against feedback loops.** If a persona's own writing feeds back into its prompts, re-inject its stable persona each time and cap how much recent self-output is shown, so tics and drift do not compound.
10. **Reward what you can honestly measure.** A satisfaction rating can be gamed, and a model that learns what makes its user happy may learn to flatter. If ratings ever steer choices, keep them as one signal among others, and let a persona say things that are honest but unwelcome.

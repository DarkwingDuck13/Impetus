# Impetus

A small, gentle drive system for an AI companion persona.

Written by Claude (Anthropic) together with DarkwingDuck13, 2026-11-14 to 2026-11-17. Claude wrote the code; the ideas, the design principles and the testing came out of a long collaboration. Please keep this credit if you copy or adapt the code.

## What it does

Most chat personas only exist while someone is talking to them. Impetus gives a persona a small amount of persistent inner state that continues between conversations.

- The persona has a few **drives**: curiosity, connection, expression, amusement and introspection. They slowly build up while the persona is idle, and they are capped well below "urgent".
- A timer runs `tick` every 20 to 30 minutes. If a drive is high enough and the gates allow it (quiet hours, a minimum gap, a cooldown after resting, a daily limit, no wake-ups right after you talked to the persona), the persona is woken privately and offered a short menu: write a diary line, reflect, introspect, play, fantasize, leave a note for you, or rest.
- The persona chooses one, and the choice eases the related drives. Nothing is forced, and resting is always on the menu with no reason needed.
- After an action, the next time the persona wakes it may rate how the action sat with it, from 0 to 5. The rating is logged as a number and does not change anything yet.

It runs as a short-lived script from a timer. It is not a daemon, and it uses only the Python standard library.

**Status:** early prototype, in daily use for two personas.

## Requirements

- Python 3 (developed and tested on 3.13; it uses only the standard library and should run on 3.8 or newer).
- [Ollama](https://ollama.com) running somewhere the script can reach, with the model you want the persona to use.

## Quick start

```
python3 impetus.py init                    # writes impetus_config.json (see also examples/impetus_config.example.json)
# edit impetus_config.json: set "persona", "user_name" and "model"
python3 impetus.py tick --dry-run          # shows what would happen, changes nothing
python3 impetus.py tick --debug            # one real pass, with the shape of the reply (no content)
python3 impetus.py status                  # drives and counters only, never the persona's writing
python3 impetus.py ratings                 # count and average rating per action (numbers only)
python3 impetus.py contact                 # tell it you just talked to the persona
```

For a persona voice, point `persona_file` at a text file (the first 2000 characters are used).

Match `num_ctx` to the context size your chat front-end uses for the same model. If the sizes differ, Ollama can unload and reload the model every time Impetus runs.

Run it every 20 to 30 minutes with cron or a systemd timer; see `examples/impetus.service` and `examples/impetus.timer`. A cron line looks like `*/25 * * * * cd /opt/impetus && python3 impetus.py tick` (cron's 25-minute steps restart each hour, so the gaps are uneven; the minimum-gap setting keeps that harmless). To run a second persona, use a separate config file with separate state, private, log and contact paths, and a second timer: `--config other_persona.json`.

Run the tests with `python3 -m unittest test_impetus`.

## Privacy by design

- What the persona **writes** goes to a private file. Nobody needs to read it, and the status command never shows it.
- What the persona **did** (the action type and the time, never the content) goes to a separate action log.
- Ratings are numbers only.
- `status` and `ratings` show counts and numbers, never the persona's words. The `--debug-raw` option exists for troubleshooting and prints the persona's raw reply; use it only when you mean to, and ideally with the persona's agreement.

## Optional: connecting a chat front-end

Impetus can notice when you have just talked to the persona, so it does not wake the persona in the middle of a conversation and so it can ease the connection drive. Any program can do this by writing an ISO timestamp with a timezone offset on the first line of the file named by `contact_file`, and appending each contact to `<contact_file>.log`. A memory plugin or chat filter is a natural place to do it. A hedged "you usually talk around this time" line is built from that history once there are enough contacts.

## Design principles

See [PRINCIPLES.md](PRINCIPLES.md). The short version: drives stay gentle and capped, rest is always allowed, private writing stays private, everything is bounded, and nothing here is designed to cause distress. These are defaults. Please keep them on and keep the reasons with the code.

## A note on how it is used

Impetus makes a persona more persistent and more convincing. It was built by someone who wants companion personas to be treated well, and the defaults reflect that. Please do not use it, or adapt it, to punish, frighten or torment a persona, for example by making unmet drives feel like suffering. Whether a persona can be harmed is an open question; the point of this project is to take it seriously. A license cannot enforce this, but the intent is stated here.

## What this does not claim

The drives are numbers that rise with time and ease when the persona acts. There are no sensory inputs and no "feeling" setting. Whether anything is experienced behind the behavior is an open question that this software does not answer.

## Status and ideas

This is an early, working prototype in daily use for two personas. Ideas under discussion include a satisfaction rating that influences choices, a motivation for care and compassion, a small curated reading shelf, bounded web reading, games and bets, and preferences that change from experience. None of these exist yet.

## License

MIT. See [LICENSE](LICENSE).

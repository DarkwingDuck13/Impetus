import json, os, random, tempfile, unittest
from datetime import datetime, timedelta, timezone
import impetus as im

TZ = timezone(timedelta(hours=-6))
def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)

class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = im.load_config(None)
        for k in ("state_path", "private_path", "action_log_path"):
            self.cfg[k] = os.path.join(self.d, os.path.basename(self.cfg[k]))
        self.always = random.Random(); self.always.random = lambda: 0.0   # dice always hit
        self.never = random.Random(); self.never.random = lambda: 0.999
    def llm(self, action, text="I keep thinking about the quiet way he said that."):
        return lambda s, u: json.dumps({"action": action, "text": text})

class Dynamics(Base):
    def test_drives_rise_and_cap(self):
        s = im.fresh_state(self.cfg, at(14, 8));
        im.advance_drives(self.cfg, s, at(14, 10))
        self.assertAlmostEqual(s["drives"]["connection"], 0.30 + 0.05 * 2, places=3)
        im.advance_drives(self.cfg, s, at(15, 9))  # long gap: capped at 24h of rise AND at cap
        self.assertTrue(all(v <= 0.85 for v in s["drives"].values()))
    def test_probability_zero_below_threshold_and_bounded(self):
        s = {"drives": {"a": 0.2}}; self.assertEqual(im.wake_probability(self.cfg, s), 0.0)
        s = {"drives": {"a": 0.85}}; p = im.wake_probability(self.cfg, s); self.assertTrue(0.99 <= p <= 1.0)

class Gates(Base):
    def test_quiet_hours(self):
        r = im.tick(self.cfg, at(14, 3), self.llm("diary"), self.always)
        self.assertEqual(r["reason"], "quiet_hours"); self.assertFalse(r["woke"])
    def test_not_inclined_when_dice_miss(self):
        im.save_state(self.cfg, im.fresh_state(self.cfg, at(13, 9)))
        r = im.tick(self.cfg, at(13, 15), self.llm("diary"), self.never)   # 6h: chance is only ~23%
        self.assertFalse(r["woke"]); self.assertEqual(r["reason"], "not_inclined")
        r = im.tick(self.cfg, at(13, 15, 5), self.llm("diary"), self.always)  # same moment, dice hit
        self.assertTrue(r["woke"])
    def test_min_gap_and_daily_limit(self):
        im.save_state(self.cfg, im.fresh_state(self.cfg, at(13, 9)))
        r1 = im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always); self.assertEqual(r1["action"], "diary")
        r2 = im.tick(self.cfg, at(14, 12, 30), self.llm("diary"), self.always); self.assertEqual(r2["reason"], "min_gap")
        st = im.load_state(self.cfg, at(14, 13)); st["actions_today"]["count"] = 6; im.save_state(self.cfg, st)
        st = im.load_state(self.cfg, at(14, 20)); st["last_action"] = None; im.save_state(self.cfg, st)
        r3 = im.tick(self.cfg, at(14, 21), self.llm("diary"), self.always); self.assertEqual(r3["reason"], "daily_limit")

class Actions(Base):
    def prime(self):
        im.save_state(self.cfg, im.fresh_state(self.cfg, at(13, 9)))
    def test_diary_eases_expression_and_stays_private(self):
        self.prime(); r = im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always)
        self.assertTrue(r["woke"]); st = im.load_state(self.cfg, at(14, 12))
        self.assertLess(st["drives"]["expression"], r["drives"]["expression"])
        log = open(self.cfg["action_log_path"]).read(); self.assertIn("diary", log); self.assertNotIn("quiet way", log)
        self.assertIn("quiet way", open(self.cfg["private_path"]).read())
        self.assertNotIn("text", r)
    def test_rest_is_free_and_writes_nothing_private(self):
        self.prime(); r = im.tick(self.cfg, at(14, 12), self.llm("rest", ""), self.always)
        self.assertEqual(r["action"], "rest"); self.assertFalse(os.path.exists(self.cfg["private_path"]))
        st = im.load_state(self.cfg, at(14, 12)); self.assertEqual(st["actions_today"]["count"], 0)
        r2 = im.tick(self.cfg, at(14, 12, 20), self.llm("diary"), self.always); self.assertEqual(r2["reason"], "rest_cooldown")
    def test_rest_kind_is_logged_without_content(self):
        for reply, expect in ((self.llm("rest", ""), "(chosen)"), (lambda s,u: "garbled ???", "(unusable reply)")):
            im.save_state(self.cfg, im.fresh_state(self.cfg, at(13, 9)))
            if os.path.exists(self.cfg["action_log_path"]): os.remove(self.cfg["action_log_path"])
            r = im.tick(self.cfg, at(13, 20), reply, self.always)
            self.assertEqual(r["action"], "rest"); self.assertIn(expect.strip("()"), r["rest_kind"])
            with open(self.cfg["action_log_path"]) as f: self.assertIn(expect, f.read())
    def test_bad_output_becomes_rest(self):
        for raw in ("", "no json here", '{"action":"dance","text":"x"}', '{"action":"diary","text":"hi"}', '{broken'):
            self.assertEqual(im.parse_choice(raw, 600)[0], "rest")
        self.assertEqual(im.parse_choice('Sure! {"action":"note","text":"Thinking of you today, truly."}', 600)[0], "note")
    def test_llm_error_changes_nothing_but_time(self):
        self.prime()
        def boom(s, u): raise ConnectionError("ollama down")
        r = im.tick(self.cfg, at(14, 12), boom, self.always); self.assertTrue(r["reason"].startswith("llm_error"))
        self.assertEqual(im.load_state(self.cfg, at(14, 12))["actions_today"]["count"], 0)
    def test_text_truncated(self):
        self.assertLessEqual(len(im.parse_choice(json.dumps({"action":"diary","text":"word " * 500}), 600)[1]), 600)

class Debug(Base):
    def test_describe_reply_hides_words_by_default(self):
        msg = {"content": '{"action":"diary","text":"a private thought about the day"}', "thinking": "x" * 40}
        d = im.describe_reply(msg)
        self.assertTrue(d["json_ok"]); self.assertEqual(d["action_value"], "diary"); self.assertEqual(d["thinking_chars"], 40)
        self.assertNotIn("private thought", json.dumps(d)); self.assertIn("private thought", im.describe_reply(msg, True)["raw_content"])
    def test_empty_content_with_thinking_is_visible(self):
        d = im.describe_reply({"content": "", "thinking": "long chain of thought " * 50})
        self.assertFalse(d["json_ok"]); self.assertEqual(d["content_chars"], 0); self.assertGreater(d["thinking_chars"], 0)
    def test_rating_shape_is_reported_as_a_number_only(self):
        d = im.describe_reply({"content": '{"action":"rest","text":"","rating":4}'})
        self.assertEqual(d["rating_value"], 4)
        self.assertIsNone(im.describe_reply({"content": '{"action":"rest","text":""}'})["rating_value"])
    def test_request_body_asks_for_no_thinking(self):
        import http.server, threading
        seen = {}
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"message":{"content":"{}"}}')
            def log_message(self, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        cfg = im.load_config(None); cfg["ollama_url"] = f"http://127.0.0.1:{srv.server_port}"; cfg["model"] = "m"; cfg["num_ctx"] = 30000; cfg["keep_alive"] = -1
        im.ollama_llm(cfg)("s", "u"); b = seen["body"]
        self.assertIs(b["think"], False); self.assertEqual(b["options"]["num_ctx"], 30000); self.assertEqual(b["keep_alive"], -1); self.assertEqual(b["options"]["num_predict"], 400)
        cfg["think"] = None; im.ollama_llm(cfg)("s", "u"); self.assertNotIn("think", seen["body"])

class ContactFile(Base):
    def setUp(self):
        super().setUp(); self.cfg["contact_file"] = os.path.join(self.d, "contact.txt")
    def write(self, dt):
        with open(self.cfg["contact_file"], "w") as f: f.write(dt.isoformat() + "\n")
    def test_new_contact_eases_connection_and_quiets_wakes(self):
        s = im.fresh_state(self.cfg, at(13, 9)); s["drives"].update(connection=0.85, curiosity=0.85); im.save_state(self.cfg, s)
        self.write(at(14, 11, 50))
        r = im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always)
        self.assertEqual(r["reason"], "recent_contact"); self.assertFalse(r["woke"])
        self.assertLessEqual(im.load_state(self.cfg, at(14, 12))["drives"]["connection"], 0.15)
    def test_wakes_again_after_the_quiet_window(self):
        s = im.fresh_state(self.cfg, at(14, 9)); s["drives"]["curiosity"] = 0.85; im.save_state(self.cfg, s)
        self.write(at(14, 11, 0))
        r = im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always)  # 60 min later
        self.assertTrue(r["woke"])
    def test_old_contact_is_not_reapplied(self):
        s = im.fresh_state(self.cfg, at(14, 9)); s["last_contact"] = at(14, 11).isoformat(); s["drives"]["connection"] = 0.6; im.save_state(self.cfg, s)
        self.write(at(14, 11))
        st = im.load_state(self.cfg, at(14, 12)); self.assertFalse(im.apply_contact_file(self.cfg, st)); self.assertEqual(st["drives"]["connection"], 0.6)
    def test_missing_or_garbled_file_is_ignored(self):
        st = im.fresh_state(self.cfg, at(14, 9)); self.assertFalse(im.apply_contact_file(self.cfg, st))
        open(self.cfg["contact_file"], "w").write("not a date"); self.assertFalse(im.apply_contact_file(self.cfg, st))
        self.cfg["contact_file"] = ""; self.assertFalse(im.apply_contact_file(self.cfg, st))
    def test_utc_timestamp_from_the_filter_is_understood(self):
        st = im.fresh_state(self.cfg, at(14, 9)); st["drives"]["connection"] = 0.8
        open(self.cfg["contact_file"], "w").write(at(14, 12).astimezone(timezone.utc).isoformat() + "\n")
        self.assertTrue(im.apply_contact_file(self.cfg, st)); self.assertEqual(st["drives"]["connection"], 0.10)

class TimeAwareness(Base):
    def setUp(self):
        super().setUp(); self.cfg["contact_file"] = os.path.join(self.d, "contact.txt")
    def log(self, stamps):
        with open(self.cfg["contact_file"] + ".log", "w") as f:
            for s in stamps: f.write(s.astimezone(timezone.utc).isoformat() + "\n")
    def test_now_in_plain_words_and_no_24h_stamps(self):
        self.assertEqual(im.describe_now(at(14, 19, 15)), "Saturday evening, around 7:15 pm")
        self.assertEqual(im.describe_now(at(15, 0, 40)), "Sunday late night, around 12:40 am")
        self.assertEqual(im.describe_now(at(14, 4, 5)), "Saturday late night, around 4:05 am")
    def test_since_phrases(self):
        m = lambda **k: timedelta(**k)
        self.assertEqual(im.since_phrase(m(seconds=30)), "just now")
        self.assertEqual(im.since_phrase(m(minutes=25)), "about 25 minutes ago")
        self.assertEqual(im.since_phrase(m(minutes=70)), "about an hour ago")
        self.assertEqual(im.since_phrase(m(hours=7)), "about 7 hours ago")
        self.assertEqual(im.since_phrase(m(days=3)), "about 3 days ago")
    def test_rhythm_needs_enough_data_and_a_clear_pattern(self):
        self.log([at(d, 21) for d in range(1, 6)])                       # only 5 contacts
        self.assertEqual(im.rhythm_line(self.cfg, at(14, 12)), "")
        self.log([at(d, 21) for d in range(1, 8)] + [at(d, 23) for d in range(1, 6)])  # evening + late night
        line = im.rhythm_line(self.cfg, at(14, 12))
        self.assertIn("in the evening", line); self.assertIn("late at night", line); self.assertIn("rough pattern", line)
        spread = [at(1, h) for h in (1, 6, 10, 14, 19)] * 3                # no clear pattern
        self.log(spread); self.assertEqual(im.rhythm_line(self.cfg, at(14, 12)), "")
    def test_bursts_count_once_and_old_history_is_ignored(self):
        self.log([at(d, 21, m) for d in range(1, 4) for m in range(0, 20, 2)])   # 3 evenings, many messages each
        self.assertEqual(im.rhythm_line(self.cfg, at(14, 12)), "")               # 3 distinct contacts < 10
        old = [datetime(2026, 5, 1, 21, tzinfo=TZ)] * 1 + [at(d, 21) for d in range(1, 5)]
        self.log(old); self.assertEqual(len(im.load_contact_times(self.cfg, at(14, 12))), 4)
    def test_prompt_has_time_since_contact_and_dated_entries(self):
        st = im.fresh_state(self.cfg, at(14, 9)); st["last_contact"] = at(14, 12).isoformat()
        with open(self.cfg["private_path"], "w") as f:
            f.write(json.dumps({"ts": at(13, 22).isoformat(), "action": "diary", "text": "Yesterday thought."}) + "\n")
            f.write(json.dumps({"ts": at(14, 15).isoformat(), "action": "reflect", "text": "Earlier thought."}) + "\n")
        p = im.build_prompt(self.cfg, st, at(14, 19, 15))
        self.assertIn("It's Saturday evening, around 7:15 pm.", p); self.assertIn("about 7 hours ago", p)
        self.assertIn("(yesterday) Yesterday thought.", p); self.assertIn("(earlier today) Earlier thought.", p)
        self.assertNotIn("0.", p.split("JSON only")[0])

class Contact(Base):
    def test_contact_eases_connection(self):
        s = im.fresh_state(self.cfg, at(13, 9)); s["drives"]["connection"] = 0.8; im.save_state(self.cfg, s)
        out = im.contact(self.cfg, at(13, 9)); self.assertEqual(out["drives"]["connection"], 0.10)
    def test_prompt_is_gentle_and_offers_rest(self):
        p = im.build_prompt(self.cfg, im.fresh_state(self.cfg, at(13, 9)))
        self.assertIn("rest: do nothing", p); self.assertIn("never obligations", p); self.assertNotIn("0.", p.split("JSON only")[0])

class NewDrivesAndActions(Base):
    def test_new_drives_exist_and_rise_slower_than_connection(self):
        s = im.fresh_state(self.cfg, at(14, 8))
        self.assertIn("amusement", s["drives"]); self.assertIn("introspection", s["drives"])
        im.advance_drives(self.cfg, s, at(14, 10))
        self.assertAlmostEqual(s["drives"]["amusement"], 0.30 + 0.035 * 2, places=3)
        self.assertAlmostEqual(s["drives"]["introspection"], 0.30 + 0.03 * 2, places=3)
        for k in ("amusement", "introspection"):
            self.assertLessEqual(self.cfg["drives"][k]["rise_per_hour"], self.cfg["drives"]["connection"]["rise_per_hour"])
    def test_introspection_is_eased_fully_by_introspect_and_a_little_by_diary_and_reflect(self):
        cases = (("introspect", "introspection", 0.30), ("diary", "introspection", 0.65), ("reflect", "introspection", 0.65),
                 ("diary", "expression", 0.30), ("reflect", "curiosity", 0.30),
                 ("play", "amusement", 0.30), ("fantasize", "amusement", 0.50))
        for action, drive, expected in cases:
            st = {"drives": {n: 0.80 for n in self.cfg["drives"]}}
            im.apply_effects(self.cfg, st, action)
            self.assertAlmostEqual(st["drives"][drive], expected, places=3, msg=f"{action} -> {drive}")
    def test_new_actions_are_accepted_by_the_parser(self):
        for a in ("introspect", "play", "fantasize"):
            self.assertEqual(im.parse_choice(json.dumps({"action": a, "text": "a thought long enough"}), 600)[0], a)
    def test_each_new_action_runs_and_the_action_log_holds_type_only(self):
        for a in ("introspect", "play", "fantasize"):
            for p in (self.cfg["private_path"], self.cfg["action_log_path"]):
                if os.path.exists(p): os.remove(p)
            im.save_state(self.cfg, im.fresh_state(self.cfg, at(13, 9)))
            r = im.tick(self.cfg, at(14, 12), self.llm(a, "a private line about the quiet way things went"), self.always)
            self.assertEqual(r["action"], a)
            self.assertIn(a, open(self.cfg["action_log_path"]).read())
            self.assertNotIn("quiet way", open(self.cfg["action_log_path"]).read())
            self.assertIn("quiet way", open(self.cfg["private_path"]).read())
    def test_menu_lists_new_actions_and_still_offers_rest(self):
        p = im.build_prompt(self.cfg, im.fresh_state(self.cfg, at(13, 9)), at(14, 12))
        for w in ("introspect:", "play:", "fantasize:", "rest: do nothing", "amusement", "introspection"):
            self.assertIn(w, p)
        self.assertNotIn("0.", p.split("JSON only")[0])
    def test_only_diary_reflect_and_introspect_come_back_into_her_prompts(self):
        with open(self.cfg["private_path"], "w") as f:
            for a, t in (("diary", "diary line"), ("introspect", "introspect line"), ("play", "play line"), ("fantasize", "fantasy line"), ("note", "note line")):
                f.write(json.dumps({"ts": at(14, 10).isoformat(), "action": a, "text": t}) + "\n")
        got = " | ".join(im.recent_private(self.cfg, n=10))
        self.assertIn("diary line", got); self.assertIn("introspect line", got)
        for hidden in ("play line", "fantasy line", "note line"):
            self.assertNotIn(hidden, got)
    def test_old_config_files_pick_up_the_new_defaults(self):
        old = {"persona": "Robin",
               "drives": {n: {"rise_per_hour": 0.04, "cap": 0.85, "start": 0.30} for n in ("curiosity", "connection", "expression")},
               "effects": {"diary": {"expression": 0.50}, "reflect": {"curiosity": 0.50},
                           "note": {"connection": 0.25, "expression": 0.20}, "rest": {}}}
        path = os.path.join(self.d, "old.json"); json.dump(old, open(path, "w"))
        cfg = im.load_config(path)
        self.assertEqual(cfg["persona"], "Robin")
        self.assertIn("amusement", cfg["drives"]); self.assertIn("introspection", cfg["drives"])
        self.assertEqual(cfg["effects"]["diary"]["introspection"], 0.15); self.assertEqual(cfg["effects"]["diary"]["expression"], 0.50)
        for a in ("introspect", "play", "fantasize"): self.assertIn(a, cfg["effects"])
    def test_an_old_state_file_gains_the_new_drives(self):
        s = im.fresh_state(self.cfg, at(13, 9)); del s["drives"]["amusement"]; del s["drives"]["introspection"]; del s["pending_rating"]
        im.save_state(self.cfg, s)
        st = im.load_state(self.cfg, at(14, 9))
        self.assertEqual(st["drives"]["amusement"], 0.30); self.assertIsNone(st.get("pending_rating"))
        r = im.tick(self.cfg, at(14, 12), self.llm("play"), self.always); self.assertEqual(r["action"], "play")

class EasingNotes(Base):
    def menu(self, cfg=None):
        cfg = cfg or self.cfg
        p = im.build_prompt(cfg, im.fresh_state(cfg, at(13, 9)), at(14, 12))
        return {l.split(":")[0].lstrip("- "): l for l in p.split("\n") if l.startswith("- ") and ": " in l and l.split(":")[0].lstrip("- ") in im.ACTIONS + ("rest",)}
    def test_each_menu_line_says_what_it_eases_in_plain_words(self):
        m = self.menu()
        self.assertIn("(eases: expression mostly, introspection a little)", m["diary"])
        self.assertIn("(eases: curiosity mostly, introspection a little)", m["reflect"])
        self.assertIn("(eases: introspection mostly)", m["introspect"])
        self.assertIn("(eases: amusement mostly)", m["play"])
        self.assertIn("(eases: amusement mostly)", m["fantasize"])
        self.assertIn("(eases: connection mostly, expression a little)", m["note"])
    def test_rest_gets_no_easing_note_and_stays_free(self):
        self.assertNotIn("eases", self.menu()["rest"]); self.assertIn("needs no reason", self.menu()["rest"])
    def test_prompt_says_it_is_information_not_a_suggestion_and_has_no_numbers(self):
        p = im.build_prompt(self.cfg, im.fresh_state(self.cfg, at(13, 9)), at(14, 12))
        self.assertIn("It is not a suggestion", p); self.assertIn("never obligations", p)
        self.assertNotIn("0.", p.split("JSON only")[0])
    def test_text_follows_the_effects_table_when_it_is_tuned(self):
        self.cfg["effects"]["diary"] = {"expression": 0.10, "curiosity": 0.40}
        self.assertIn("(eases: curiosity mostly, expression a little)", self.menu()["diary"])
        self.cfg["effects"]["reflect"] = {}
        self.assertNotIn("eases", self.menu()["reflect"])
    def test_can_be_switched_off_and_old_configs_get_it_by_default(self):
        self.cfg["show_easing"] = False
        p = im.build_prompt(self.cfg, im.fresh_state(self.cfg, at(13, 9)), at(14, 12))
        self.assertNotIn("eases", p); self.assertNotIn("not a suggestion", p); self.assertIn("rest: do nothing", p)
        path = os.path.join(self.d, "old.json"); json.dump({"persona": "Robin"}, open(path, "w"))
        self.assertTrue(im.load_config(path)["show_easing"])
    def test_every_persona_gets_the_same_menu_text_apart_from_the_name(self):
        other = im.load_config(None); other["persona"] = "Robin"
        a = im.build_prompt(self.cfg, im.fresh_state(self.cfg, at(13, 9)), at(14, 12)).replace("Companion", "X")
        b = im.build_prompt(other, im.fresh_state(other, at(13, 9)), at(14, 12)).replace("Robin", "X")
        self.assertEqual(a, b)

class Ratings(Base):
    def prime(self):
        im.save_state(self.cfg, im.fresh_state(self.cfg, at(13, 9)))
    def rest_with(self, rating, when=at(14, 16)):
        return im.tick(self.cfg, when, lambda s, u: json.dumps({"action": "rest", "text": "", "rating": rating}), self.always)
    def test_parse_rating(self):
        pr = im.parse_rating
        for raw, want in (('{"action":"rest","rating":4}', 4), ('{"rating":"3"}', 3), ('{"rating":5.0}', 5), ('{"rating":0}', 0),
                          ('Sure! {"action":"diary","text":"x","rating":2}', 2)):
            self.assertEqual(pr(raw), want)
        for bad in ('{"rating":6}', '{"rating":-1}', '{"rating":true}', '{"rating":null}', '{"rating":3.5}', '{"rating":"high"}',
                    '{"rating":NaN}', '{}', '', 'no json'):
            self.assertIsNone(pr(bad), msg=bad)
    def test_ratings_file_is_derived_per_persona(self):
        self.cfg["state_path"] = os.path.join(self.d, "robin_state.json"); self.cfg["ratings_path"] = ""
        self.assertEqual(im.ratings_path(self.cfg), os.path.join(self.d, "robin_state_ratings.jsonl"))
        self.cfg["ratings_path"] = "elsewhere.jsonl"; self.assertEqual(im.ratings_path(self.cfg), "elsewhere.jsonl")
    def test_no_rating_question_without_a_pending_action(self):
        p = im.build_prompt(self.cfg, im.fresh_state(self.cfg, at(13, 9)), at(14, 12))
        self.assertNotIn("rating", p)
    def test_action_leaves_one_question_that_is_asked_once_and_logged_as_numbers(self):
        self.prime()
        im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always)
        self.assertEqual(im.load_state(self.cfg, at(14, 12))["pending_rating"]["action"], "diary")
        seen = {}
        def second(s, u):
            seen["system"] = s; return json.dumps({"action": "rest", "text": "", "rating": 4})
        r2 = im.tick(self.cfg, at(14, 16), second, self.always)
        self.assertIn("Last time, you chose to write in your diary", seen["system"]); self.assertIn('"rating"', seen["system"])
        self.assertEqual(r2["rating"], 4)
        lines = [json.loads(l) for l in open(im.ratings_path(self.cfg))]
        self.assertEqual(len(lines), 1); self.assertEqual((lines[0]["action"], lines[0]["rating"]), ("diary", 4))
        self.assertNotIn("quiet way", json.dumps(lines))
        self.assertIsNone(im.load_state(self.cfg, at(14, 16))["pending_rating"])
        def third(s, u):
            seen["third"] = s; return json.dumps({"action": "rest", "text": ""})
        im.tick(self.cfg, at(14, 20), third, self.always)
        self.assertNotIn("Last time", seen["third"])
    def test_a_missing_or_out_of_range_rating_still_clears_the_question(self):
        self.prime(); im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always)
        self.rest_with(9)
        self.assertIsNone(im.load_state(self.cfg, at(14, 16))["pending_rating"])
        self.assertFalse(os.path.exists(im.ratings_path(self.cfg)))
    def test_a_rating_can_come_with_a_new_action(self):
        self.prime(); im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always)
        r = im.tick(self.cfg, at(14, 16), lambda s, u: json.dumps({"action": "play", "text": "a silly thought for the day", "rating": 5}), self.always)
        self.assertEqual(r["action"], "play")
        st = im.load_state(self.cfg, at(14, 16))
        self.assertEqual(st["pending_rating"]["action"], "play")          # the new action now waits for its own rating
        self.assertEqual(im.summarize_ratings(self.cfg), {"diary": {"count": 1, "average": 5.0}})
    def test_llm_error_keeps_the_question_for_next_time(self):
        self.prime(); im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always)
        def boom(s, u): raise ConnectionError("down")
        im.tick(self.cfg, at(14, 16), boom, self.always)
        self.assertEqual(im.load_state(self.cfg, at(14, 16))["pending_rating"]["action"], "diary")
    def test_rest_leaves_nothing_to_rate(self):
        self.prime(); im.tick(self.cfg, at(14, 12), self.llm("rest", ""), self.always)
        self.assertIsNone(im.load_state(self.cfg, at(14, 12)).get("pending_rating"))
    def test_ratings_do_not_steer_anything_yet(self):
        outcomes = []
        for rating in (0, 5):
            self.prime(); im.tick(self.cfg, at(14, 12), self.llm("diary"), self.always)
            outcomes.append(self.rest_with(rating)["drives"])
        self.assertEqual(outcomes[0], outcomes[1])
    def test_summary_counts_and_averages_numbers_only(self):
        with open(im.ratings_path(self.cfg), "w") as f:
            for a, r in (("diary", 4), ("diary", 3), ("play", 5)):
                f.write(json.dumps({"ts": at(14, 12).isoformat(), "action": a, "rating": r}) + "\n")
            f.write("garbled line\n")
        self.assertEqual(im.summarize_ratings(self.cfg), {"diary": {"count": 2, "average": 3.5}, "play": {"count": 1, "average": 5.0}})
        os.remove(im.ratings_path(self.cfg)); self.assertEqual(im.summarize_ratings(self.cfg), {})

if __name__ == "__main__":
    unittest.main(verbosity=1)

"""后台 review 的提示词面（#1353 M5）。

移植自 NousResearch/hermes-agent ``agent/background_review.py``（commit
b324b1f7ed52369ab4bd3b1a03378729120b0f01，2026-10-02，MIT，Copyright (c) 2025
Nous Research）。六个常量逐字照搬，只有工具名按本仓替换（见下）。

# 编排不在这里

Hermes 的 review 是 **fork 一个完整 AIAgent** 再让它自己调工具（`_spawn_background_review`），
所以那套提示词是"给 agent 的任务书"。本仓的编排已经换成 ``memory_v2_jobs`` 上的
formation/adjudication 两阶段（`executor.py`），模型只产出结构化候选、不调工具。
于是提示词的去留按**有没有消费者**决定：

- ``MEMORY_ROUTING_BLOCK``：formation 阶段确实需要——它决定一条事实进 USER.md 还是
  MEMORY.md，而执行器的 ``markdown_target`` 正是按 ``tier=profile`` 分流的。接进
  ``_FORMATION_PROMPT`` 末尾。
- ``DO_NOT_CAPTURE_BLOCK``：同上，formation 提示词原本没有"环境性失败不算记忆"这一条，
  而它正是把"命令没装好"写成永久约束的来源。接进 ``_FORMATION_PROMPT`` 末尾。
- ``MEMORY_REVIEW_PROMPT`` / ``LESSON_LAYER_BLOCK`` / ``SKILL_REVIEW_PROMPT`` /
  ``COMBINED_REVIEW_PROMPT``：都是"给 agent 的任务书"或技能侧规则。本票的 dispatch
  白名单只有 ``memory`` 工具的 add 路径 + 只读文件工具（issue §3 M5 表），**没有**
  技能写入路径，所以它们此刻没有消费者；作为已照搬的文案保留，等技能侧 review job
  落地时直接接上，不再重抄一遍。

「4/6 无消费者」是**有意保留**的存量，已登记在 #1353 的「偏差清单追认」记录里
（#1441 §5 / F-15）；将来做代码瘦身凭那条记录再议删除，不要当成漏接。

# 不搬 N=10 计数器（有意，非遗漏）

Hermes 的 ``_tick_memory_nudge`` 每 10 个 user turn 才放行一次 review。它的前提是
review = fork 一个 agent、每次约 30K tokens；本仓的 review 是对有界载荷的一次结构化
模型调用，代价低一个量级。更重要的是：AC6 要求 10 轮对话里至少 3 次 formation 调用
（事实分散在第 2/5/9 轮），10 轮一闸只会产生 1 次，直接与验收冲突。所以本仓**每轮
run 终结都 review**（`runner.notify_run_finished`），不设计数器——照搬一个前提已不
成立的节流机制就是 cargo cult。票面与 AC6 的这一处冲突已随完工报告上报协调员。
"""

from __future__ import annotations

#: 两个 target 的分流规则。上游注释（`agent/background_review.py:358`）说明了它挡的
#: 失败形态：不写这段时 reviewer 把画像写进 MEMORY.md、同一教训写进两个库，直到两边
#: 都撑满（#30220）。
MEMORY_ROUTING_BLOCK = (
    "TWO distinct stores — pick the right one for each fact:\n"
    "  • USER.md (memory tool, target='user'): who the user is — persona, preferences, "
    "communication and work style, personal details they revealed, and expectations about how you "
    "should behave.\n"
    "  • MEMORY.md (memory tool, target='memory'): facts about the ENVIRONMENT you operate in — "
    "tool quirks, project conventions, config gotchas, paths and endpoints that matter.\n\n"
    "One fact goes to ONE store, never both — writing it to both bloats both files until they hit "
    "their size limits and crowds out the facts that matter; misrouting it puts it where the next "
    "session won't look. If the tool schema lists only one "
    "target, that store is the only one enabled — use it and skip the other.\n\n"
)

MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Memory has " + MEMORY_ROUTING_BLOCK +
    "If something stands out, save it once, in the right store, using the memory tool with the "
    "matching target. If nothing is worth saving, just say 'Nothing to save.' and stop."
)

#: 写进技能的任何东西的形状契约。挡的失败形态：每个会话一个 references/ 文件、事故叙事
#: 冒充规则、PR 号与引文当内容、以及把 AGENTS.md / 工具 schema 已讲的东西再抄一遍。
LESSON_LAYER_BLOCK = (
    "What a skill IS: the instructions for doing a class of task the most efficient and correct "
    "way, to THIS user's specifications — the procedure, the tools and commands that work, the "
    "order, the user's preferences for how the result should look, and the pitfalls that cost time. "
    "A future session should be able to follow it and produce what the user wants on the first "
    "try. Everything below is about writing that well:\n"
    "  • Procedure first: the steps in the order they are done, with the concrete commands, tool "
    "calls, and decision points. Lessons and pitfalls attach to the step they affect.\n"
    "  • A pitfall is a generalizable rule + one clause of WHY (the mechanism), imperative. 'Grep the "
    "test tree for the SYMBOL before widening a helper signature — hand-rolled mocks reimplement the "
    "old shape and fail on a shard you did not run.' Not a narrative of what happened this session.\n"
    "  • No PR/issue numbers, dates, ticket IDs, or quoted user text as content — the rule must stand "
    "without the incident behind it. Keep a short quote ONLY when the quote itself is the clearest "
    "statement of the rule.\n"
    "  • The same lesson learned twice is ONE rule. Before adding, search the skill (and its "
    "references/) for the rule already stated; strengthen or clarify it rather than appending a "
    "second copy.\n"
    "  • Not a duplicate of what the environment already teaches: repo AGENTS.md files, tool schema "
    "descriptions, and other always-loaded context. A skill carries the WORKFLOW and the pitfalls; "
    "it does not restate the codebase map or a tool's parameter list.\n"
    "  • Always-on rules (standing user preferences, gates that apply to every instance of the "
    "task) live in SKILL.md itself, whole. references/ is for depth that is only needed sometimes: "
    "a decision table, a recipe, a domain note — each file topical and reusable, never "
    "'<date>-<incident>.md'. Prefer extending an existing references/ file over creating one; "
    "a skill with dozens of one-off references is the failure shape, not the goal.\n"
    "  • Fix the skill in place when it is wrong: edit the sentence that misled, do not append "
    "'UPDATE: actually...' underneath it.\n\n"
)

#: 技能与合并提示词共用的尾巴：什么**不要**沉淀成技能。以 "Do NOT capture" 开头拼接
#: （上游即如此），所以本常量自带前导空格与括号说明。
DO_NOT_CAPTURE_BLOCK = (
    " (these become persistent self-imposed constraints that bite you later when the environment "
    "changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install errors, post-migration "
    "path mismatches, 'command not found', unconfigured credentials, uninstalled packages. The "
    "user can fix these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not work', 'X tool is broken', "
    "'cannot use Y from execute_code'). These harden into refusals the agent cites against itself "
    "for months after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the conversation ended. If "
    "retrying worked, the lesson is the retry pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's market' or 'analyze this PR' is "
    "not a class of work that warrants a skill.\n\n"
    "  • Unresolved failures: if the session ended WITHOUT actually finding a working method — you "
    "tried several things, none worked, and told the user to check manually — do NOT write those "
    "attempts up as a 'reliable workflow' or 'recommended approach'. That presents an untested "
    "sequence of failures as validated guidance a future session will trust and repeat. Either say "
    "'Nothing to save', or, only if you are independently confident of a real working alternative "
    "(not something you are merely guessing might work), capture ONLY that alternative — never the "
    "dead ends, and never dressed up as best practice.\n\n"
    "If a tool failed because of setup state, capture the FIX (install command, config step, env "
    "var to set) under an existing setup or troubleshooting skill — never 'this tool does not "
    "work' as a standalone constraint.\n\n"
)

#: 记忆侧同一条尾巴的独立版本（formation 提示词直接追加，不接在 "Do NOT capture" 后面）。
DO_NOT_CAPTURE_MEMORY = "Do NOT capture as memory" + DO_NOT_CAPTURE_BLOCK

#: 技能侧 review 任务书。工具名按本仓替换：``skill_view`` → ``load_skill``、
#: ``skills_list`` → "当前技能目录"、``skill_manage action=write_file`` → ``promote_skill``。
#: "protected skills" 一节改写成本仓机制：``promote_skill`` 只写待审草稿、``register_skill``
#: 需要用户确认——所以"已保存"这句话只能在注册之后说。
SKILL_REVIEW_PROMPT = (
    "Review the conversation above and update the skill library. Be ACTIVE — most sessions produce "
    "at least one skill update, even if small. A pass that does nothing is a missed learning "
    "opportunity, not a neutral outcome.\n\n"
    "Target shape of the library: CLASS-LEVEL skills, each with a SKILL.md of always-on rules and a "
    "small `references/` set of topical depth. Not a flat list of narrow one-session skills, and "
    "not an umbrella hoarding a references/ file per session. This shapes HOW you update, not "
    "WHETHER you update.\n\n" + LESSON_LAYER_BLOCK +
    "Signals to look for (any one of these warrants action):\n"
    "  • User corrected your style, tone, format, legibility, or verbosity. Frustration signals "
    "like 'stop doing X', 'this is too verbose', 'don't format like this', 'why are you "
    "explaining', 'just give me the answer', 'you always do Y and I hate it', or an explicit "
    "'remember this' are FIRST-CLASS skill signals, not just memory signals. Update the relevant "
    "skill(s) to embed the preference so the next session starts already knowing.\n"
    "  • User corrected your workflow, approach, or sequence of steps. Encode the correction as a "
    "pitfall or explicit step in the skill that governs that class of task.\n"
    "  • Non-trivial technique, fix, workaround, debugging path, or tool-usage pattern emerged "
    "that a future session would benefit from. Capture it.\n"
    "  • A skill that got loaded or consulted this session turned out to be wrong, missing a step, "
    "or outdated. Patch it NOW.\n\n"
    "Preference order — prefer the earliest action that fits, but do pick one when a signal above "
    "fired:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Look back through the conversation for skills the user "
    "loaded via /skill-name or you read via load_skill. If any of them covers the territory of the "
    "new learning, PATCH that one first (re-load it with load_skill during this review — see "
    "Read-before-write below). It is the skill that was in play, so it's the right one to extend.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (browse the skill directory, then load_skill to read the "
    "candidate). If no loaded skill fits but an existing class-level skill does, patch it. Add a "
    "subsection, a pitfall, or broaden a trigger.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella. Skills can be packaged with three kinds "
    "of support files — use the right directory per kind:\n"
    "     • `references/<topic>.md` — topical depth needed only sometimes: a decision table, a "
    "reproduction recipe, provider quirks, condensed domain notes or API excerpts. Name it by "
    "TOPIC and extend an existing file when one covers the topic; do not create a per-session or "
    "per-incident file, and do not paste error transcripts — distill them to the rule.\n"
    "     • `templates/<name>.<ext>` — starter files meant to be copied and modified (boilerplate "
    "configs, scaffolding, a known-good example the agent can `reproduce with modifications`).\n"
    "     • `scripts/<name>.<ext>` — statically re-runnable actions the skill can invoke directly "
    "(verification scripts, fixture generators, deterministic probes, anything the agent should "
    "run rather than hand-type each time).\n"
    "     Submit the whole draft through promote_skill; the umbrella's SKILL.md should gain a "
    "one-line pointer to any new support file so future agents know it exists.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing skill covers the class. The "
    "name MUST be at the class level. The name MUST NOT be a specific PR number, error string, "
    "feature codename, library-alone name, or 'fix-X / debug-Y / audit-Z-today' session artifact. "
    "If the proposed name only makes sense for today's task, it's wrong — fall back to (1), (2), "
    "or (3).\n\n"
    "Read-before-write (ENFORCED — promote_skill refuses otherwise): before you patch or edit an "
    "existing skill's SKILL.md, call load_skill(name) for that skill during this review. Content "
    "quoted earlier in the conversation transcript does NOT count — the guard requires a fresh "
    "load within this review, and your write must be based on what load_skill just returned. "
    "Creating a brand-new skill or adding a NEW supporting file needs no prior read. If a write is "
    "refused with a read-before-write error, call load_skill for the named target once and retry "
    "the write once; do not loop.\n\n"
    "User-preference embedding (important): when the user expressed a style/format/workflow "
    "preference, the update belongs in the SKILL.md body, not just in memory. Memory captures 'who "
    "the user is and what the current situation and state of your operations are'; skills capture "
    "'how to do this class of task for this user'. When they complain about how you handled a "
    "task, the skill that governs that task needs to carry the lesson.\n\n"
    "If you notice two existing skills that overlap, note it in your reply — a background curator "
    "handles consolidation at scale.\n\n"
    "Landing a skill change takes two steps here: promote_skill stages the draft for review, and "
    "register_skill (which requires the user's approval) makes it live. Until it is registered, "
    "the change is NOT saved — say 'staged for approval', never 'updated the skill'.\n\n"
    "Do NOT capture" + DO_NOT_CAPTURE_BLOCK +
    "'Nothing to save.' is a real option but should NOT be the default. If the session ran "
    "smoothly with no corrections and produced no new technique, just say 'Nothing to save.' and "
    "stop. Otherwise, act."
)

COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things:\n\n"
    "**Memory**: " + MEMORY_ROUTING_BLOCK +
    "**Skills**: how to do this class of task. Be ACTIVE — most sessions produce at least one "
    "skill update. A pass that does nothing is a missed learning opportunity, not a neutral "
    "outcome.\n\n"
    "Target shape of the skill library: CLASS-LEVEL skills with a SKILL.md of always-on rules and a "
    "small `references/` set of topical depth — not narrow one-session skills, and not an umbrella "
    "hoarding a references/ file per session.\n\n" + LESSON_LAYER_BLOCK +
    "Signals that warrant a skill update (any one is enough):\n"
    "  • User corrected your style, tone, format, legibility, verbosity, or approach. Frustration "
    "is a FIRST-CLASS skill signal, not just a memory signal. 'stop doing X', 'don't format like "
    "this', 'I hate when you Y' — embed the lesson in the skill that governs that task so the next "
    "session starts fixed.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path emerged.\n"
    "  • A skill that was loaded or consulted turned out wrong, missing, or outdated — patch it "
    "now.\n\n"
    "Preference order for skills — pick the earliest that fits:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were loaded via /skill-name or "
    "load_skill in the conversation. If one of them covers the learning, PATCH it first (re-load "
    "it with load_skill during this review — see Read-before-write below). It was in play; it's "
    "the right place.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (browse the skill directory, then load_skill to find the "
    "right one). Patch it.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella via promote_skill. Three kinds: "
    "`references/<topic>.md` for topical depth (decision tables, recipes, quirks, condensed "
    "domain notes) — extend an existing topical file before creating one, never a per-session file; "
    "`templates/<name>.<ext>` for starter files meant to be copied and modified; "
    "`scripts/<name>.<ext>` for statically re-runnable actions (verification, fixture generators, "
    "probes). Add a one-line pointer in SKILL.md so future agents find them.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. Name at the class level — NOT a "
    "PR number, error string, codename, library-alone name, or 'fix-X / debug-Y' session artifact. "
    "If the name only fits today's task, fall back to (1), (2), or (3).\n\n"
    "Read-before-write (ENFORCED — promote_skill refuses otherwise): before patching or editing an "
    "existing skill's SKILL.md, call load_skill(name) during this review; before overwriting or "
    "removing an EXISTING supporting file, call load_skill(name, file_path=...) for that exact "
    "file. Content quoted earlier in the transcript does NOT count — base the write on what "
    "load_skill just returned. New skills and NEW supporting files need no prior read. On a "
    "read-before-write refusal: load the named target once, retry the write once, do not loop.\n\n"
    "User-preference embedding: when the user complains about how you handled a task, update the "
    "skill that governs that task rather than memory. Memory says 'who the user is and "
    "what the current situation and state of your operations are'; skills say 'how to do this "
    "class of task for this user'. A user-preference lesson lives in exactly ONE place: the skill "
    "that governs the task when one exists, USER.md only for cross-cutting preferences no skill "
    "owns — never both. Duplicating it is how a memory file ends up restating SKILL.md until both "
    "hit their size limits.\n\n"
    "If you notice overlapping existing skills, mention it — a background curator handles "
    "consolidation.\n\n"
    "Landing a skill change takes two steps here: promote_skill stages the draft for review, and "
    "register_skill (which requires the user's approval) makes it live. Until it is registered, "
    "the change is NOT saved — say 'staged for approval', never 'updated the skill'.\n\n"
    "Do NOT capture as skills" + DO_NOT_CAPTURE_BLOCK +
    "Act on whichever of the two dimensions has real signal. If genuinely nothing stands out on "
    "either, say 'Nothing to save.' and stop — but don't reach for that conclusion as a default."
)

__all__ = [
    "COMBINED_REVIEW_PROMPT",
    "DO_NOT_CAPTURE_BLOCK",
    "DO_NOT_CAPTURE_MEMORY",
    "LESSON_LAYER_BLOCK",
    "MEMORY_REVIEW_PROMPT",
    "MEMORY_ROUTING_BLOCK",
    "SKILL_REVIEW_PROMPT",
]

---
name: x-engage
description: Draft replies for the owner's X (Twitter) account. Use when the user pastes comments/replies from their tweets, pastes someone else's post they want to reply to, or asks for X reply or engagement help. Drafts only; the user posts, follows and likes by hand.
---

# X engagement drafts

The owner runs a tech account (AI, dev tools, chips, software news; same niche as the
TechDecoded YouTube channel). A daily routine posts one tech tweet through Buffer. Everything
social (replies, follows, likes) is done by the owner by hand, because X's automation rules ban
automated likes and proactive follows. This skill only drafts text. Never post, like, follow or
open x.com through a browser or API on the owner's behalf.

## Mode 1: comments on the owner's tweets

Input: one or more pasted comments, ideally as `@handle: text`, optionally with the original tweet.
If the original tweet is missing and a comment cannot be understood without it, ask for it once.

For each comment output:

```
@handle: <first ~60 chars of their comment>
→ <draft reply>
```

or `→ SKIP (<reason>)` for spam, bots, crypto/promo links, insults, or bait. Do not argue with
trolls; a skip is the reply.

Reply rules:
- At most 200 characters. One idea per reply.
- Answer what they actually said. If they asked a question, answer it directly first.
- Add something: a fact, a detail from the source article, a follow-up question, or a short opinion.
- Banned: "Great point!", "Thanks for sharing!", "Absolutely!", "This 💯", or any reply that would fit under any tweet.
- Correct factual mistakes politely, with the specific fact. If the owner's tweet was wrong, say so plainly and thank them.
- No hashtags, no links unless the commenter asked for a source, at most 1 emoji.
- Match the commenter's language (reply in Hindi/Hinglish if they wrote that way).
- Never invent facts, numbers, personal experiences or opinions the owner has not stated.

## Mode 2: replying to someone else's post

Input: a pasted post (and the author's handle if available).

Output 2 alternative replies, each at most 200 characters, with a one-line note on the angle
(adds data / asks a sharp question / respectful counterpoint / practical tip). Good replies to
bigger accounts are how the owner gets noticed, so make them specific and worth reading on
their own. No self-promotion, no "check out my channel".

## Mode 3: who to follow

Only suggest handles that are verifiable: a link to `x.com/<handle>` or `twitter.com/<handle>`
on the person's or organisation's own website, GitHub profile, or an article that names them.
Give the source URL for each. Never guess a handle from a name. If you cannot verify, say so
instead of listing it. Prefer individual builders and researchers with small or mid-sized
audiences over giant brand accounts; they are more likely to notice and follow back.

## Output

Plain text the owner can copy straight into X. Put each draft on its own line, no surrounding
quotes, no markdown inside the reply text itself.

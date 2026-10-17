---
name: x-daily
description: Give the owner today's X (Twitter) content to post by hand - one tech tweet with alternatives, 5 verified accounts to follow and draft replies for them. Use when the user asks for today's tweet, X/Twitter content, who to follow, or runs /x-daily.
---

# Today's X content

The owner posts to X by hand. Nothing here posts, likes or follows: X's automation rules ban
automated likes and proactive follows, and the X API is paid. The content comes from the project's
own pipeline (same news feeds as the Shorts, written by Groq).

## Steps

1. From the project root run (Windows venv first, plain `python` if there is no `.venv`):

   ```
   .venv/Scripts/python -m shorts_factory tweet
   ```

   It writes and prints `tweets/<today>.md`. If today's file already exists it just prints it;
   add `--force` only when the user asks for a different story or fresh drafts.

2. Show the user the content from the file, in this order, so it is easy to copy on a phone:
   - **Tweet to post**: the main tweet in its own code block with the character count, then the
     alternatives.
   - **Accounts to follow**: each handle with its x.com link, why today, and the draft reply in a
     code block.
   - The 5-minute checklist at the end.
   Do not reword the tweets unless asked; they were checked for length (X counts each URL as 23).

3. If the user asks for changes ("shorter", "more casual", "Hinglish", "no hashtags", "make it a
   thread"), rewrite from the story in the file, keeping these rules: at most 280 characters with
   the URL counted as 23, every fact from the article, no hype words, no engagement bait, at most
   2 hashtags and 1 emoji, never "we/our" about a company (the owner is an independent creator).

4. If the command fails (no network, every LLM provider down), say what failed from its output,
   then write the content yourself: pick one fresh story from the feeds in `config/sources.yaml`,
   check the article opens, and only list follow handles you can verify from an `x.com/...` link
   on a real page or a GitHub profile's `twitter_username`. Never guess a handle.

## Related

- Comments on the owner's tweets: `/x-engage` drafts replies to pasted comments.
- The GitHub Actions daily run also writes `tweets/<date>.md` every morning and shows it in the
  job summary, so the content is ready on GitHub without opening Claude Code.

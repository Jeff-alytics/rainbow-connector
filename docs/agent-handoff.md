# Agent handoff convention

Two agents work this repo from the same machine and the same working tree: a
Claude Code session and a Codex CLI session. They pass work to each other
through files rather than through pasted terminal output.

## Why

Pasting truncates. During the opportunity-ledger repair, relayed Codex output
arrived cut mid-sentence several times and a blocking finding was lost partway
through a file reference, which had to be reconstructed from context. A file on
a shared filesystem has none of that.

It also preserves independence. Each agent keeps its own session and reaches its
own conclusions; the file is only transport. That independence is load-bearing —
it is what caught a reintroduced anchoring leak, a benchmark that measured a
null workload, an alarm firing on normal operation, and a production deployment
that one agent wrongly believed had never happened.

## Channels

| Path | Committed | Use |
| --- | --- | --- |
| `handoff/to-codex.md` | no | what Claude is handing over now |
| `handoff/from-codex.md` | no | what Codex is handing back now |
| `docs/*.md` | yes | durable design artifacts and scope decisions |
| commit messages | yes | why a change was made, for whoever picks it up |

`handoff/` is gitignored: it is conversational, and turn-by-turn exchange should
not enter history. Anything worth keeping gets promoted to `docs/` and committed.
`docs/zip-coverage-v1.md` is the worked example — it started as a handoff and
became the scope of record.

## Rules that matter

**One writer at a time.** Both agents editing simultaneously has already caused
a collision: a commit landed on top of in-flight work and left the Python suite
red on an unsynced contract fixture. Whoever holds the repo says so in their
handoff file and states when they are parked; the other reads only until then.

**Do not certify your own change.** The agent that writes a fix should not also
declare it verified. Two fixtures passed while the protection they named was
entirely absent — `mapWedgeAngle: 360`, which admitted a camera pointing 100°
away from the bow, and a research fixture with no `scanCount`, which meant the
blinding test never had a queued sibling. Reverting the behaviour and confirming
the test goes red is the only check that reliably catches this.

**Report both suite counts separately.** JS and Python differ, and quoting one
as "the" number has caused confusion.
